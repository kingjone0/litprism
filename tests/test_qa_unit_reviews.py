"""Independent human units cannot promote siblings or bypass provenance/recovery."""
import json

import pytest
from test_workflow_guards import env  # noqa: F401

from litprism.context.dialogue import build_dialogue
from litprism.knowledge import get_source, search_knowledge


def start(env):
    cfg, kb, model, client = env
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    cfg.qa.evidence_anchor_trial = True
    pid = kb.add_paper(title='Unit fixture', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'unitfixture. The encoder uses attention. Decoder attends to all input positions.', 'page': 1},
                        {'text': 'Section 4 reports eleven tasks. Section 5.3 separately studies NER.', 'page': 2}])
    def response(messages, task='', **kwargs):
        model.calls.append((task, messages))
        catalog = json.loads(messages[-1]['content'].split('【试验定位锚点（程序生成，不是语义结论）】\n')[1])
        def u(tag, text):
            a = next(a for a in catalog if a['source'] == tag)
            return {'text': text, 'subject': '方法', 'basis': 'direct', 'sources': [tag],
                    'quotes': [{'source': tag, 'anchor_id': a['anchor_id']}]}
        return {'answer_units': [u('P1', '编码器使用注意力。[P1]'), u('P2', 'DRAFT_ONLY_UNREVIEWED_MARKER [P2]')]}
    model.chat_json = response
    cid = kb.add_conversation('Unit review', scope={'paper_ids': [pid]})
    answer = client.post('/api/chat/'+cid, json={'question': 'unitfixture'}).json()
    rid = answer['assistant']['qa_review_id']
    d = client.get('/api/chat-reviews/'+rid).json()
    return pid, cid, rid, d


def detail(client, rid, uid):
    return client.get('/api/chat-reviews/'+rid+'/units/'+uid).json()


def submit(client, rid, uid, d, **changes):
    context = client.get('/api/chat-reviews/'+rid+'/units/'+uid+'/sources/P1').json()
    anchor = next(a for a in context.get('anchors', []) if 'The encoder uses' in a['quote']) if context.get('anchors') else None
    req = {'action': 'confirm', 'note': '逐项核对实际原文及对象。', 'reviewed_text': 'encoderknowledge 注意力。[P1]',
           'evidence': [{'source': 'P1', 'anchor_id': anchor['id'] if anchor else 'qa2_stale'}],
           'snapshot_hash': d['snapshot_hash'], 'review_token': d['review_token']}
    req.update(changes)
    return client.post('/api/chat-reviews/'+rid+'/units/'+uid+'/review', json=req)


def test_confirm_one_unit_never_promotes_sibling_or_parent_history_and_retrieves_only_human_text(env):
    cfg, kb, model, client = env
    _, cid, rid, parent = start(env)
    uid, other = [u['id'] for u in parent['units']]
    frozen = kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0]
    d = detail(client, rid, uid)
    result = submit(client, rid, uid, d)
    assert result.status_code == 200, result.text
    sid = result.json()['knowledge_id']
    parent = client.get('/api/chat-reviews/'+rid).json()
    assert parent['review_mode'] == 'units' and not parent['reviews'] and parent['review_token'] == ''
    assert [u['decision'] for u in parent['units']] == ['confirm', 'unreviewed']
    assert detail(client, rid, other)['review_token'] == ''
    assert kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0] == frozen
    knowledge = get_source(kb, sid)
    assert knowledge['citable'] and 'DRAFT_ONLY_UNREVIEWED_MARKER' not in knowledge['content']
    assert len(knowledge['dependency_status']['refs']) == 1
    assert knowledge['dependency_status']['refs'][0]['chunk_id'] == parent['sources'][0]['chunk_id']
    hits = search_knowledge(kb, 'encoderknowledge', 10)
    assert hits and all('DRAFT_ONLY_UNREVIEWED_MARKER' not in h['text'] for h in hits)
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    message = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert message['status'] == 'human_partial'
    assert '编码器使用注意力' in message['content'] and 'DRAFT_ONLY_UNREVIEWED_MARKER' in message['content']
    assert [u['decision'] for u in message['unit_review']['units']] == ['confirm', 'unreviewed']
    assert message['unit_review']['units'][0]['reviewed_text'] == 'encoderknowledge 注意力。[P1]'
    assert message['unit_review']['units'][0]['knowledge_citable']
    assert len(message['source_links']) == 2
    assert len(model.calls) == 1


def test_revision_replaces_one_knowledge_version_and_keeps_previous_audit_immutable(env):
    _, kb, _, client = env
    _, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    first = submit(client, rid, uid, detail(client, rid, uid)).json()['knowledge_id']
    d = detail(client, rid, uid)
    first_record = d['reviews'][0]
    audit = kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', ('qud_'+first_record['id'],)).fetchone()[0]
    revised = submit(client, rid, uid, d, action='revise', reviewed_text='newversionknowledge 使用注意力。[P1]')
    assert revised.status_code == 200, revised.text
    second = revised.json()['knowledge_id']
    assert second != first and get_source(kb, first)['status'] == 'revoked' and get_source(kb, second)['citable']
    assert not search_knowledge(kb, 'encoderknowledge', 10)
    assert search_knowledge(kb, 'newversionknowledge', 10)
    assert kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', ('qud_'+first_record['id'],)).fetchone()[0] == audit
    assert submit(client, rid, uid, d, action='revise').status_code == 409
    d = detail(client, rid, uid)
    assert submit(client, rid, uid, d, action='revoke').status_code == 200
    assert not search_knowledge(kb, 'newversionknowledge', 10)
    assert detail(client, rid, uid)['suggested_text'].startswith('newversionknowledge')


def test_partial_review_keeps_visible_original_but_source_failure_still_blocks_it(env):
    cfg, kb, model, client = env
    pid, cid, rid, parent = start(env)
    uid = parent['units'][0]['id']
    fresh = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert fresh['unit_review']['draft'] == parent['draft']
    for unit in fresh['unit_review']['units']:
        position = unit['answer_span']
        assert parent['draft'][position['start']:position['end']] == unit['text']
    assert submit(client, rid, uid, detail(client, rid, uid), action='reject').status_code == 200
    visible = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert visible['status'] == 'human_partial' and visible['unit_review']['units'][0]['decision'] == 'reject'
    assert 'DRAFT_ONLY_UNREVIEWED_MARKER' in visible['content']
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    kb.update_paper(pid, verify_level='unchecked')
    blocked = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert blocked['status'] == 'invalid_citation' and not blocked.get('unit_review')
    assert 'DRAFT_ONLY_UNREVIEWED_MARKER' not in blocked['content']
    assert 'DRAFT_ONLY_UNREVIEWED_MARKER' not in json.dumps(blocked['segments'])
    assert len(model.calls) == 1


def test_all_confirmed_units_complete_the_display_without_promoting_parent_history(env):
    cfg, kb, model, client = env
    _, cid, rid, parent = start(env)
    first, second = [u['id'] for u in parent['units']]
    assert submit(client, rid, first, detail(client, rid, first)).status_code == 200
    pending = client.get('/api/chat-reviews/'+rid).json()['review_summary']
    assert pending['state'] == 'partial' and pending['pending'] == 1 and not pending['complete']
    d = detail(client, rid, second)
    evidence = [{'source':q['source'], 'anchor_id':q['anchor_id']} for q in d['original_evidence']]
    assert submit(client, rid, second, d, reviewed_text='第二条经人工核对。[P2]', evidence=evidence).status_code == 200
    completed = client.get('/api/chat-reviews/'+rid).json()
    summary = completed['review_summary']
    assert summary['state'] == 'all_confirmed' and summary['confirmed'] == 2 and summary['complete']
    assert '全部 2 条已确认入库' == summary['label'] and summary['whole_answer_covered']
    assert completed['reviews'] == [] and completed['review_mode'] == 'units'
    reopened = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['unit_review']['summary'] == summary
    assert not reopened['audit_warnings'] and len(reopened['audit_notices']) == 2
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    assert submit(client, rid, first, detail(client, rid, first), action='revoke').status_code == 200
    after = client.get('/api/chat-reviews/'+rid).json()['review_summary']
    assert after['state'] == 'all_reviewed' and after['confirmed'] == 1 and after['revoked'] == 1 and after['complete']
    assert len(model.calls) == 1


def test_completion_reports_rejected_and_invalidated_knowledge_separately(env):
    _, kb, _, client = env
    pid, _, rid, parent = start(env)
    first, second = [u['id'] for u in parent['units']]
    assert submit(client, rid, first, detail(client, rid, first)).status_code == 200
    assert submit(client, rid, second, detail(client, rid, second), action='reject').status_code == 200
    done = client.get('/api/chat-reviews/'+rid).json()['review_summary']
    assert done['state'] == 'all_reviewed' and done['rejected'] == 1 and done['confirmed'] == 1
    kb.update_paper(pid, verify_level='unchecked')
    invalid = client.get('/api/chat-reviews/'+rid).json()['review_summary']
    assert invalid['confirmed'] == 0 and invalid['invalidated'] == 1 and '依据已失效' in invalid['label']


def test_all_mapped_units_confirmed_cannot_claim_coverage_of_unmapped_prose():
    from litprism.qa_answer import span
    from litprism.qa_unit_reviews import completion
    answer = 'First [P1]\n\nUnmapped prose [P2]'
    first = {'answer_span':span(answer, 0, 10), 'decision':'confirm', 'knowledge_citable':True}
    summary = completion({'draft':answer}, [first])
    assert summary['confirmed'] == 1 and summary['processed'] == 1
    assert not summary['whole_answer_covered'] and not summary['complete']
    assert '另有未对应正文' in summary['label']


@pytest.mark.parametrize('read', ['list', 'resolve', 'messages'])
def test_conversation_refresh_reads_wait_for_review_transaction(env, read):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    _, kb, _, _ = env
    cid = kb.add_conversation('Before review')
    kb.append_message(cid, 'assistant', 'before')
    readers = {'list': lambda: kb.list_conversations(), 'resolve': lambda: kb.resolve_conversation(cid),
               'messages': lambda: kb.get_messages(cid)}
    started = threading.Event()
    def refresh():
        started.set()
        return readers[read]()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with kb.tx() as con:
            con.execute('UPDATE conversations SET title=? WHERE id=?', ('After review', cid))
            con.execute('UPDATE messages SET content=? WHERE conversation_id=?', ('after', cid))
            future = pool.submit(refresh)
            assert started.wait(2)
            # The reader cannot see uncommitted review state on this connection.
            with pytest.raises(TimeoutError):
                future.result(timeout=.1)
        result = future.result(timeout=2)
    assert (result[0]['content'] if read == 'messages' else result[0]['title'] if read == 'list' else result['title']) == (
        'after' if read == 'messages' else 'After review')


def test_tokens_are_independent_for_each_unit_and_stale_unit_token_cannot_duplicate(env):
    _, kb, _, client = env
    _, _, rid, parent = start(env)
    a, b = [u['id'] for u in parent['units']]
    da, db = detail(client, rid, a), detail(client, rid, b)
    assert submit(client, rid, a, da).status_code == 200
    assert submit(client, rid, a, da).status_code == 409
    assert submit(client, rid, b, db, action='reject').status_code == 200
    assert [u['decision'] for u in client.get('/api/chat-reviews/'+rid).json()['units']] == ['confirm', 'reject']
    assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['delete', 'trust', 'material', 'text', 'page'])
def test_changed_source_blocks_confirmation_but_rejection_still_works(env, change):
    _, kb, _, client = env
    pid, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    d = detail(client, rid, uid)
    source = client.get('/api/chat-reviews/'+rid+'/units/'+uid+'/sources/P1').json()
    chosen = next(a for a in source['anchors'] if 'The encoder uses' in a['quote'])
    if change == 'delete':kb.delete_paper(pid)
    elif change == 'trust':kb.update_paper(pid, verify_level='unchecked')
    elif change == 'material':kb.add_chunks(pid, [{'text': 'new material', 'page': 3}], level='fulltext')
    else:
        with kb.tx() as con:
            con.execute('UPDATE chunks SET '+change+'=? WHERE id=?', ('changed' if change == 'text' else 9, parent['sources'][0]['chunk_id']))
    assert submit(client, rid, uid, d, evidence=[{'source': 'P1', 'anchor_id': chosen['id']}]).status_code == 409
    assert submit(client, rid, uid, d, action='reject').status_code == 200
    assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0] == 0


@pytest.mark.parametrize('bad', [[], 'P1', [None], [{'source': 'P99', 'anchor_id': 'qa2_bad'}],
                                 [{'source': 'P1', 'anchor_id': 'qa_old'}], [{'source': 'P1', 'anchor_id': 'qa2_bad'}]])
def test_bad_evidence_cannot_create_knowledge(env, bad):
    _, kb, _, client = env
    _, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    assert submit(client, rid, uid, detail(client, rid, uid), evidence=bad).status_code in (400, 409)
    assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0] == 0


def test_supplemental_sentence_is_explicit_new_selection_not_original_model_evidence(env):
    _, _, _, client = env
    _, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    d = detail(client, rid, uid)
    context = client.get('/api/chat-reviews/'+rid+'/units/'+uid+'/sources/P1').json()
    assert d['original_evidence'][0]['anchor_id'] in {a['id'] for a in context['anchors']}
    later = next(a for a in context['anchors'] if 'Decoder attends' in a['quote'])
    evidence = [{'source': 'P1', 'anchor_id': later['id'], 'quote': 'forged', 'page': 999}]
    assert submit(client, rid, uid, d, reviewed_text='人工补充 decoderknowledge 可关注全部输入。[P1]', evidence=evidence).status_code == 200
    after = detail(client, rid, uid)
    assert after['original_evidence'] == d['original_evidence']
    chosen = after['reviews'][0]['evidence_snapshot'][0]
    assert chosen['quote'] == 'Decoder attends to all input positions.' and chosen['page'] == 1


def test_whole_and_unit_modes_cannot_create_overlapping_knowledge(env):
    from test_qa_reviews import submit as whole_submit
    _, _, _, client = env
    _, _, rid, d = start(env)
    uid = d['units'][0]['id']
    assert submit(client, rid, uid, detail(client, rid, uid)).status_code == 200
    assert whole_submit(client, rid, d).status_code == 409
    _, _, rid, d = start(env)
    assert whole_submit(client, rid, d).status_code == 200
    assert submit(client, rid, d['units'][0]['id'], detail(client, rid, d['units'][0]['id'])).status_code == 409


def test_old_or_forged_binding_has_no_unit_and_generic_route_cannot_bypass_decision(env):
    _, kb, _, client = env
    _, _, rid, d = start(env)
    uid = d['units'][0]['id']
    assert submit(client, rid, uid, detail(client, rid, uid)).status_code == 200
    after = detail(client, rid, uid)
    decision_id = 'qud_'+after['reviews'][0]['id']
    assert client.post('/api/semantic-runs/'+decision_id+'/claim-1/review', json={
        'action': 'confirm', 'note': '不能绕过单元来源校验', 'reviewed_text': 'bypass'}).status_code == 409
    with kb.tx() as con:
        payload = json.loads(con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0])
        for item in payload['evidence_audit']['items']:item.pop('answer_binding', None)
        con.execute('UPDATE semantic_runs SET payload=? WHERE id=?', (json.dumps(payload), rid))
    assert client.get('/api/chat-reviews/'+rid).json()['units'] == []
    assert client.get('/api/chat-reviews/'+rid+'/units/'+uid).status_code == 404


def test_failed_revision_rolls_back_revocation_and_new_audit(env, monkeypatch):
    from litprism import qa_unit_reviews
    _, kb, _, client = env
    _, _, rid, d = start(env)
    uid = d['units'][0]['id']
    sid = submit(client, rid, uid, detail(client, rid, uid)).json()['knowledge_id']
    before = kb._con.execute('SELECT count(*) FROM semantic_reviews').fetchone()[0]
    def fail(*args, **kwargs):raise RuntimeError('controlled transaction rollback')
    monkeypatch.setattr(qa_unit_reviews, 'create_source', fail)
    with pytest.raises(RuntimeError):submit(client, rid, uid, detail(client, rid, uid), action='revise')
    assert get_source(kb, sid)['status'] == 'manual_confirmed'
    assert kb._con.execute('SELECT count(*) FROM semantic_reviews').fetchone()[0] == before


def test_revision_and_upstream_withdrawal_invalidate_transitive_knowledge(env):
    from litprism.knowledge import create_source, source_chunk, review_source
    _, kb, _, client = env
    pid, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    first = submit(client, rid, uid, detail(client, rid, uid)).json()['knowledge_id']
    original = get_source(kb, first)
    child = create_source(kb, kind='knowledge', title='dependentchild', content='dependentchild',
        dependencies=[source_chunk(kb, original['chunks'][0]['id'])])
    review_source(kb, child['id'], action='confirm', note='测试依赖', content_hash=child['content_hash'])
    assert get_source(kb, child['id'])['citable']
    second = submit(client, rid, uid, detail(client, rid, uid), action='revise').json()['knowledge_id']
    assert not get_source(kb, child['id'])['citable']
    assert get_source(kb, child['id'])['effective_status'] == 'needs_review'
    assert not search_knowledge(kb, 'dependentchild', 10)
    kb.update_paper(pid, verify_level='unchecked')
    assert not get_source(kb, second)['citable']
    assert submit(client, rid, uid, detail(client, rid, uid), action='revoke').status_code == 200


def test_backup_restore_retains_unit_decisions_exact_evidence_and_revoke(env, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from litprism import backup
    from litprism.config import load_config
    from litprism.db import KnowledgeBase
    from litprism.qa_reviews import register_qa_reviews
    cfg, kb, _, client = env
    _, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    submit(client, rid, uid, detail(client, rid, uid))
    sid = submit(client, rid, uid, detail(client, rid, uid), action='revise').json()['knowledge_id']
    before = detail(client, rid, uid)
    archive = tmp_path/'units.zip'
    backup.create_backup(kb, cfg, archive)
    output = backup.restore_backup(archive, tmp_path.parent/(tmp_path.name+'-units-restored'), current_root=cfg.data_path)
    restored = KnowledgeBase(load_config(output['config_path']))
    try:
        app = FastAPI()
        register_qa_reviews(app, restored)
        c = TestClient(app)
        after = detail(c, rid, uid)
        assert after['original_evidence'] == before['original_evidence']
        assert after['reviews'] == before['reviews'] and after['knowledge_id'] == sid
        assert after['review_mode'] == 'units'
        assert submit(c, rid, uid, after, action='revoke').status_code == 200
        assert get_source(restored, sid)['status'] == 'revoked'
    finally:
        restored.close()


@pytest.mark.parametrize('like', [False, True])
def test_search_fallback_preserves_material_identity_used_by_anchor_context(env, like):
    from litprism.qa_retrieval import as_excerpts
    _, kb, _, _ = env
    pid, _, _, _ = start(env)
    hits = kb._search_like(['unitfixture'], [pid], 10) if like else kb.search_chunks('unitfixture', paper_ids=[pid])
    assert hits and hits[0]['material_id'] == kb.get_paper(pid)['current_material_id']
    assert as_excerpts(hits)[0]['material_id'] == hits[0]['material_id']


def test_generic_source_confirmation_cannot_reactivate_superseded_unit_knowledge(env):
    _, kb, _, client = env
    _, _, rid, parent = start(env)
    uid = parent['units'][0]['id']
    old_id = submit(client, rid, uid, detail(client, rid, uid)).json()['knowledge_id']
    new_id = submit(client, rid, uid, detail(client, rid, uid), action='revise').json()['knowledge_id']
    old = get_source(kb, old_id)
    response = client.post('/api/evidence/'+old_id+'/review', json={
        'action': 'confirm', 'note': '不能恢复被修订的旧单元知识', 'content_hash': old['content_hash']})
    assert response.status_code == 409
    assert get_source(kb, old_id)['status'] == 'revoked' and get_source(kb, new_id)['citable']
