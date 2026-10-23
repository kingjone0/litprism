"""Human QA review must not launder blocked drafts or bypass current source checks."""
import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.context.dialogue import build_dialogue
from litprism.knowledge import create_source, get_source, review_source, search_knowledge


def start(env, text='BLOCKED_DRAFT [P1 p.99]'):
    _, kb, model, client = env
    pid = paper(kb)
    model.text = text
    cid = kb.add_conversation('Review', scope={'paper_ids': [pid]})
    answer = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    rid = answer['assistant']['qa_review_id']
    detail = client.get('/api/chat-reviews/'+rid).json()
    return pid, cid, rid, detail


def submit(client, rid, d, **overrides):
    req = dict(action='confirm', note='核对原文和条件，修正错误页码。', reviewed_text='人工修订 alphatopic [P1]',
               source_tags=['P1'], snapshot_hash=d['snapshot_hash'], review_token=d['review_token'])
    req.update(overrides)
    return client.post('/api/chat-reviews/'+rid+'/review', json=req)


def test_correct_invalid_page_confirm_reopen_revoke_without_promoting_draft(env):
    cfg, kb, model, client = env
    _, cid, rid, d = start(env)
    calls = len(model.calls)
    assert d['status'] == 'invalid_citation' and d['sources'][0]['page'] == 1
    assert d['sources'][0]['citable'] and 'BLOCKED_DRAFT' in d['draft']
    assert submit(client, rid, d, reviewed_text=d['draft']).status_code == 400
    response = submit(client, rid, d)
    assert response.status_code == 200, response.text
    sid = response.json()['knowledge_id']
    source = get_source(kb, sid)
    assert source['status'] == 'manual_confirmed' and 'BLOCKED_DRAFT' not in source['content']
    assert d['sources'][0]['chunk_id'] in source['content']
    reopened = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['qa_review_id'] == rid and reopened['status'] == 'invalid_citation'
    assert 'BLOCKED_DRAFT' not in reopened['content']
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    assert search_knowledge(kb, '人工修订', 10)
    d = client.get('/api/chat-reviews/'+rid).json()
    assert submit(client, rid, d, action='revoke').status_code == 200
    assert get_source(kb, sid)['status'] == 'revoked'
    assert not search_knowledge(kb, '人工修订', 10)
    assert len(model.calls) == calls


@pytest.mark.parametrize('change', ['delete', 'trust', 'material', 'text', 'page'])
def test_source_changes_after_open_block_confirmation_but_allow_rejection(env, change):
    _, kb, _, client = env
    pid, _, rid, d = start(env)
    if change == 'delete':
        kb.delete_paper(pid)
    elif change == 'trust':
        kb.update_paper(pid, verify_level='likely')
    elif change == 'material':
        kb.add_chunks(pid, [{'text': 'replacement source', 'page': 2}], level='fulltext')
    else:
        with kb.tx() as con:
            con.execute('UPDATE chunks SET '+change+'=? WHERE id=?',
                        ('altered source' if change == 'text' else 2, d['sources'][0]['chunk_id']))
    assert submit(client, rid, d).status_code == 409
    after = client.get('/api/chat-reviews/'+rid).json()
    assert not after['sources'][0]['citable']
    assert submit(client, rid, d, action='reject').status_code == 200
    assert kb._con.execute('SELECT COUNT(*) FROM evidence_sources').fetchone()[0] == 0


@pytest.mark.parametrize('override', [
    {'source_tags': []}, {'source_tags': ['P99']}, {'source_tags': [None]},
    {'source_tags': 'P1'}, {'source_tags': [{}]}, {'reviewed_text': '无原文标记'},
    {'reviewed_text': '其他来源 [P2]'}, {'note': ''}, {'reviewed_text': ''},
])
def test_unbound_or_malformed_confirmation_cannot_create_knowledge(env, override):
    _, kb, _, client = env
    _, _, rid, d = start(env)
    assert submit(client, rid, d, **override).status_code == 400
    assert kb._con.execute('SELECT COUNT(*) FROM evidence_sources').fetchone()[0] == 0


def test_stale_snapshot_duplicate_click_and_other_review_route_cannot_bypass(env):
    _, kb, _, client = env
    _, _, rid, d = start(env)
    assert submit(client, rid, d, snapshot_hash='wrong').status_code == 409
    assert client.post('/api/semantic-runs/'+rid+'/reply/review', json={
        'action': 'confirm', 'note': 'bypass', 'analysis_hash': d['snapshot_hash'],
        'reviewed_text': 'unbound text'}).status_code == 409
    assert submit(client, rid, d).status_code == 200
    assert submit(client, rid, d).status_code == 409
    latest = client.get('/api/chat-reviews/'+rid).json()
    assert submit(client, rid, latest).status_code == 409
    assert kb._con.execute('SELECT COUNT(*) FROM evidence_sources').fetchone()[0] == 1
    assert submit(client, rid, latest, action='reject').status_code == 200
    assert get_source(kb, latest['reviews'][-1]['knowledge_id'])['status'] == 'revoked'


def test_scope_change_does_not_rebind_frozen_reply_and_revoke_survives_source_loss(env):
    _, kb, _, client = env
    pid, cid, rid, d = start(env)
    other = paper(kb, title='Other')
    client.patch('/api/conversations/'+cid, json={'scope': {'paper_ids': [other]}})
    response = submit(client, rid, d)
    assert response.status_code == 200
    sid = response.json()['knowledge_id']
    kb.delete_paper(pid)
    latest = client.get('/api/chat-reviews/'+rid).json()
    assert submit(client, rid, latest, action='revoke').status_code == 200
    assert get_source(kb, sid)['status'] == 'revoked'


def test_answered_reply_review_and_candidate_sources_are_not_model_inventions(env):
    _, kb, _, client = env
    _, _, rid, d = start(env, '正常回答 [P1]')
    assert d['status'] == 'answered' and len(d['sources']) == 1
    assert d['sources'][0]['text'] == kb.get_chunks(d['sources'][0]['paper_id'])[0]['text']
    assert submit(client, rid, d).status_code == 200


@pytest.mark.parametrize('action', ['confirm', 'reject'])
def test_human_decision_excludes_original_answer_and_its_old_checkpoint(env, action):
    cfg, kb, _, client = env
    _, cid, rid, d = start(env, 'ORIGINAL_CLAIM [P1]')
    original = kb.get_messages(cid)[-1]
    assert any('ORIGINAL_CLAIM' in m['content'] for m in build_dialogue(kb, cfg, cid, None).history)
    with kb.tx() as con:
        refs = json.dumps([original['ref']])
        con.execute('INSERT INTO compaction_blocks (id,conversation_id,summary,direct_refs,effective_refs,active) VALUES (?,?,?,?,?,1)',
                    ('block-review', cid, 'OLD_CHECKPOINT_CLAIM', refs, refs))
        msg = kb.append_message(cid, 'user', 'OLD_CHECKPOINT_CLAIM', kind='compact-checkpoint')
        con.execute('UPDATE messages SET block_id=? WHERE id=?', ('block-review', msg['id']))
    assert submit(client, rid, d, action=action).status_code == 200
    context = build_dialogue(kb, cfg, cid, None)
    assert not any('ORIGINAL_CLAIM' in m['content'] or 'OLD_CHECKPOINT_CLAIM' in m['content'] for m in context.history)
    loaded = client.get('/api/conversations/'+cid+'/messages').json()['items']
    reply = next(m for m in loaded if m.get('qa_review_id') == rid)
    assert reply['status'] == 'human_'+action and 'ORIGINAL_CLAIM' not in reply['content']
    # Immutable audit data remains available for inspection.
    assert client.get('/api/chat-reviews/'+rid).json()['draft'] == 'ORIGINAL_CLAIM [P1]'


@pytest.mark.parametrize('kind', ['web', 'knowledge'])
def test_nonpaper_source_revalidation_and_true_origin(env, kind):
    _, kb, model, client = env
    source = create_source(kb, kind=kind, title='Origin', content='alphatopic source evidence.',
                           url='https://example.org/source' if kind == 'web' else '')
    review_source(kb, source['id'], action='confirm', note='checked', content_hash=source['content_hash'])
    model.text = '知识说明 [P1]'
    cid = kb.add_conversation('Web')
    result = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    rid = result['assistant']['qa_review_id']
    d = client.get('/api/chat-reviews/'+rid).json()
    assert d['sources'][0]['source_kind'] == kind and d['sources'][0]['citable']
    confirmed = submit(client, rid, d)
    assert confirmed.status_code == 200
    saved = get_source(kb, confirmed.json()['knowledge_id'])
    if kind == 'web':
        assert 'https://example.org/source' in saved['content']
    d = client.get('/api/chat-reviews/'+rid).json()
    assert submit(client, rid, d, action='revoke').status_code == 200
    d = client.get('/api/chat-reviews/'+rid).json()
    review_source(kb, source['id'], action='revoke', note='withdraw', content_hash=source['content_hash'])
    assert submit(client, rid, d).status_code == 409


def test_message_and_candidate_are_atomic(env, monkeypatch):
    from litprism import qa_reviews
    _, kb, _, client = env
    pid = paper(kb)
    cid = kb.add_conversation('Atomic', scope={'paper_ids': [pid]})
    def fail(*args, **kwargs):
        raise RuntimeError('snapshot persistence failed')
    monkeypatch.setattr(qa_reviews, 'save', fail)
    with pytest.raises(RuntimeError):
        client.post('/api/chat/'+cid, json={'question': 'alphatopic'})
    assert not any(m['role'] == 'assistant' for m in kb.get_messages(cid))
    assert kb._con.execute('SELECT COUNT(*) FROM semantic_runs').fetchone()[0] == 0


def test_backup_roundtrip_keeps_reply_audit_and_reversible_knowledge(env, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from litprism import backup
    from litprism.config import load_config
    from litprism.db import KnowledgeBase
    from litprism.qa_reviews import register_qa_reviews
    cfg, kb, _, client = env
    _, _, rid, d = start(env)
    sid = submit(client, rid, d).json()['knowledge_id']
    archive = tmp_path/'review.zip'
    backup.create_backup(kb, cfg, archive)
    # The temporary source root contains the usual pytest fixtures; restore to a sibling.
    result = backup.restore_backup(archive, tmp_path.parent/(tmp_path.name+'-restored'), current_root=cfg.data_path)
    restored = KnowledgeBase(load_config(result['config_path']))
    try:
        app = FastAPI()
        register_qa_reviews(app, restored)
        restored_client = TestClient(app)
        d = restored_client.get('/api/chat-reviews/'+rid).json()
        assert 'BLOCKED_DRAFT' in d['draft'] and d['reviews'][-1]['knowledge_id'] == sid
        assert submit(restored_client, rid, d, action='revoke').status_code == 200
        assert get_source(restored, sid)['status'] == 'revoked'
        assert get_source(kb, sid)['status'] == 'manual_confirmed'
    finally:
        restored.close()
