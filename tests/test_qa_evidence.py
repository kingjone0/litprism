import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.agents import QAAgent
from litprism.qa_evidence import manifest, material_risks


def test_manifest_uses_actual_current_ids_not_a_model_claim(env):
    _, kb, _, _ = env
    pid = paper(kb)
    kb.update_paper(pid, material_level='fulltext')
    chunks = kb.get_chunks(pid)
    excerpts = [dict(c, chunk_id=c['id'], tag=f'P{i+1}') for i, c in enumerate(chunks)]
    result = manifest(kb, excerpts)
    assert result['papers'][0]['complete_current_material']
    old = excerpts
    kb.add_chunks(pid, [{'text': 'new current evidence', 'page': 2}], level='fulltext')
    assert not manifest(kb, old)['papers'][0]['complete_current_material']


@pytest.mark.parametrize('answer,blocked', [
    ('所给片段多为各论文首页/摘要。', True),
    ('本轮只包含摘要。', True),
    ('本回答仅引用摘要段落概括研究问题。[P1 p.1]', False),
    ('摘要提出了研究问题。[P1 p.1]', False),
    ('表格数值被标记为“表格待核对”。[P1 p.1]', True),
    ('表格没有标记为“表格待核对”。[P1 p.1]', False),
])
def test_actual_input_and_parse_markers_override_generated_meta_claims(answer, blocked):
    delivery = {'papers': [{'material_level': 'fulltext', 'complete_current_material': True}]}
    assert bool(material_risks(answer, [{'tag': 'P1', 'text': 'actual source text'}], delivery)) == blocked


def test_real_table_warning_and_partial_or_abstract_input_are_not_overridden():
    source = [{'tag': 'P1', 'text': '【表格待核对】行列未可靠恢复。'}]
    assert not material_risks('表格被标记为“表格待核对”。[P1 p.1]', source, {})
    assert not material_risks('本轮只包含摘要。', source, {
        'papers': [{'material_level': 'abstract', 'complete_current_material': True}]})
    assert not material_risks('本轮只包含摘要。', source, {
        'papers': [{'material_level': 'fulltext', 'complete_current_material': False}]})


def test_manifest_and_focus_instructions_share_actual_request_budget(env):
    cfg, kb, model, _ = env
    pid = paper(kb)
    kb.update_paper(pid, material_level='fulltext')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    result = QAAgent(kb, cfg, model).ask('alpha', paper_ids=[pid])
    request = model.calls[-1][1]
    assert '程序送达记录' in request[-1]['content']
    assert '不附加无关实验' in request[0]['content']
    meta = result.retrieval['evidence_manifest']
    assert meta['papers'][0]['complete_current_material']
    assert json.dumps(meta, ensure_ascii=False) in request[-1]['content']


def test_scope_misstatement_is_persisted_as_review_and_not_history(env):
    cfg, kb, model, client = env
    pid = paper(kb)
    kb.update_paper(pid, material_level='fulltext')
    model.text = '本轮所给片段只有摘要。[P1 p.1]'
    cid = kb.add_conversation('Input audit', scope={'paper_ids': [pid]})
    result = client.post('/api/chat/'+cid, json={'question': 'alpha'}).json()
    assert result['status'] == 'needs_review'
    assert '只有摘要' not in result['assistant']['content']
    assert result['assistant']['source_links']
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['status'] == 'needs_review' and reopened['guard_issues']
    from litprism.context.dialogue import build_dialogue
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb,cfg,cid,None).history)


def test_year_condition_failure_keeps_sources_and_audit_without_extra_calls(env):
    _, kb, model, client = env
    pid = paper(kb, text='alphatopic. Accuracy is 67% using the 2024 index for 2024 leaders.')
    model.text = '2024索引对2020年领导人得67%。[P1 p.1]'
    cid = kb.add_conversation('Conditions', scope={'paper_ids': [pid]})
    result = client.post('/api/chat/'+cid, json={'question': 'alpha'}).json()
    assert result['status'] == 'needs_review' and result['assistant']['citation_check']['passed']
    assert '2020' not in result['assistant']['content']
    assert result['assistant']['source_links']
    assert len([c for c in model.calls if c[0] == 'qa']) == 1
    stored = kb.get_messages(cid)[-1]
    assert '2020' in json.loads(stored['meta'])['blocked_answer']
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['status'] == 'needs_review' and reopened['source_links']


def test_delivery_manifest_is_frozen_with_the_actual_request(env):
    cfg, kb, model, _ = env
    pid = paper(kb)
    kb.update_paper(pid, material_level='fulltext')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    original = model.chat_json
    def mutate(messages, **kwargs):
        if kwargs.get('task') == 'qa':
            kb.add_chunks(pid, [{'text': 'new current source', 'page': 2}], level='fulltext')
        return original(messages, **kwargs)
    model.chat_json = mutate
    result = QAAgent(kb, cfg, model).ask('alpha', paper_ids=[pid])
    sent = model.calls[-1][1][-1]['content']
    actual = result.retrieval['evidence_manifest']
    assert actual['papers'][0]['complete_current_material']
    assert json.dumps(actual, ensure_ascii=False) in sent


def test_tag_only_citations_keep_database_pages_and_wrong_explicit_page_still_blocks(env):
    _, kb, model, client = env
    pid = paper(kb, page=18)
    model.text = '原文报告的知识载体。[P1]'
    cid = kb.add_conversation('Stable references', scope={'paper_ids':[pid]})
    answer = client.post('/api/chat/'+cid, json={'question':'alpha'}).json()
    assert answer['status'] == 'answered'
    ref = answer['assistant']['source_links'][0]['ref']
    source = client.get('/api/evidence/'+ref).json()
    assert source['page'] == 18
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['source_links'][0]['ref'] == ref
    model.text = '显式错页不能静默纠正。[P1 p.19]'
    rejected = client.post('/api/chat/'+cid, json={'question':'alpha'}).json()
    assert rejected['status'] == 'invalid_citation' and not rejected['assistant']['source_links']
