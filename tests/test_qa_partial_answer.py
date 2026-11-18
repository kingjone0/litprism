"""Local availability is distinct from model refusal and semantic approval."""
from copy import deepcopy

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.agents import QAAgent


def mixed():
    return {'answer_units': [
        {'text': 'alphatopic 方法使用注意力。[P1]', 'subject': 'alphatopic 方法',
         'basis': 'direct', 'sources': ['P1'],
         'quotes': [{'source': 'P1', 'quote': 'alphatopic method uses attention.'}]},
        {'text': '尚未定位该方法的GPU训练时间。', 'subject': '该方法的GPU训练时间',
         'basis': 'not_located', 'sources': [], 'quotes': []}],
        'insufficient_evidence': True, 'confidence': 'medium',
        'open_questions': ['该方法的GPU训练时间仍未知。']}


@pytest.mark.parametrize('kind,expected', [
    ('mixed', 'answered'), ('inference', 'answered'), ('gaps', 'refused'),
    ('bad_quote', 'needs_review'), ('bad_source', 'invalid_citation'), ('legacy', 'refused'),
    ('use_reversal', 'needs_review')])
def test_partial_availability_never_bypasses_source_checks(env, kind, expected):
    cfg, kb, model, _ = env
    source = 'alphatopic method uses attention.'
    if kind == 'use_reversal':
        source += ' We did not use TriviaQA data.'
    pid = paper(kb, text=source)
    value = mixed()
    if kind == 'inference':
        value['answer_units'][0].update(basis='inference', text='据原文推断该方法使用注意力。[P1]')
    elif kind == 'gaps':
        value['answer_units'].pop(0)
    elif kind == 'bad_quote':
        value['answer_units'][0]['quotes'][0]['quote'] = 'Invented source sentence.'
    elif kind == 'bad_source':
        value['answer_units'][0].update(text='方法使用注意力。[P99]', sources=['P99'])
        value['answer_units'][0]['quotes'][0]['source'] = 'P99'
    elif kind == 'legacy':
        value = {'answer': 'alphatopic 方法使用注意力。[P1] 尚未定位GPU时间。',
                 'insufficient_evidence': True}
    elif kind == 'use_reversal':
        value['answer_units'][0]['text'] = 'alphatopic 方法使用TriviaQA数据。[P1]'
    before = deepcopy(value)
    model.chat_json = lambda *a, **k: deepcopy(value)
    answer = QAAgent(kb, cfg, model).ask('alphatopic 方法和GPU训练时间', paper_ids=[pid])
    assert answer.status == expected
    assert value == before
    assert answer.evidence_audit['semantic_status'] == 'unreviewed'
    if expected == 'answered':
        assert not answer.insufficient_evidence
        assert answer.open_questions == value['open_questions']
        assert answer.evidence_audit['answer_availability']['state'] == 'partial'
        assert answer.evidence_audit['answer_availability']['model_insufficient_evidence'] is True
    else:
        assert 'answer_availability' not in answer.evidence_audit


@pytest.mark.parametrize('basis', ['metadata', 'direct'])
def test_metadata_even_if_mislabeled_direct_is_not_a_substantive_answer(basis):
    from litprism.qa_answer import compile_answer, partial_availability
    from litprism.qa_claims import audit_map
    value = mixed()
    source = {'tag': 'P1', 'source_kind': 'web', 'text': 'alphatopic evidence.',
              'acquired_at': '2026-10-29T03:04:05+00:00'}
    value['answer_units'][0].update(text='来源获取时间已记录。[P1]', basis=basis,
        quotes=[{'source': 'P1', 'quote': '获取时间：2026-10-29T03:04:05+00:00'}])
    answer, mapping, bindings, protocol = compile_answer(value)
    audit = audit_map(answer, mapping, [source], answer_bindings=bindings, answer_protocol=protocol)
    assert audit['state'] == 'recorded'
    assert audit['items'][0]['quotes'][0]['kind'] == 'program_metadata'
    assert partial_availability(audit) is None


@pytest.mark.parametrize('durable', [False, True])
def test_mixed_reply_reopens_and_reaches_existing_review_without_mutating_raw_response(env, durable):
    import json
    from test_chat_runs import wait
    from litprism.context.dialogue import build_dialogue
    cfg, kb, model, client = env
    pid = paper(kb, text='alphatopic method uses attention.')
    value = mixed()
    model.chat_json = lambda *a, **k: deepcopy(value)
    cid = kb.add_conversation('部分可答', scope={'paper_ids': [pid], 'mode': 'strict'})
    question = {'question': 'alphatopic 方法和GPU训练时间'}
    if durable:
        rid = client.post('/api/conversations/'+cid+'/chat-runs', json={
            **question, 'request_id': 'partial-answer-case'}).json()['run_id']
        assert wait(client, rid)['status'] == 'completed'
        result = client.get('/api/chat-runs/'+rid+'/result').json()
        saved = json.loads(kb._con.execute(
            "SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa'", (rid,)).fetchone()[0])
        assert saved['value'] == value
    else:
        result = client.post('/api/chat/'+cid, json=question).json()
    assert result['status'] == 'answered' and not result['insufficient_evidence']
    reply = result['assistant']
    assert '尚未定位该方法的GPU训练时间。' in reply['content']
    assert reply['unit_review'] is not None
    reopened = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['status'] == 'answered' and reopened['source_links']
    review = client.get('/api/chat-reviews/'+reply['qa_review_id']).json()
    assert review['evidence_audit']['answer_availability']['semantic_status'] == 'unreviewed'
    original_draft = review['draft']
    assert any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    kb.update_paper(pid, verify_level='unchecked')
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    invalid = client.get('/api/chat-reviews/'+reply['qa_review_id']).json()
    assert invalid['draft'] == original_draft and not invalid['sources'][0]['citable']
