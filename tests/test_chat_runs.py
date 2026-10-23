"""Durable QA receipts, exact reuse and unknown-outcome handling with isolated APIs."""
import json
import time

import pytest
from test_workflow_guards import env, paper  # noqa: F401


def wait(client, rid):
    for _ in range(300):
        data = client.get('/api/chat-runs/'+rid).json()
        if not data['active']:
            return data
        time.sleep(.01)
    raise AssertionError('task did not finish')


def start(env, key='request_0001'):
    _, kb, _, client = env
    pid = paper(kb)
    cid = kb.add_conversation('Durable', scope={'paper_ids': [pid]})
    req = {'question': 'alphatopic', 'request_id': key}
    response = client.post(f'/api/conversations/{cid}/chat-runs', json=req)
    assert response.status_code == 200, response.text
    rid = response.json()['run_id']
    return pid, cid, rid, req


def test_success_idempotent_receipt_reopen_and_source_recheck(env):
    _, kb, model, client = env
    pid, cid, rid, req = start(env)
    data = wait(client, rid)
    assert data['status'] == 'completed', data
    calls = len(model.calls)
    assert client.post(f'/api/conversations/{cid}/chat-runs', json=req).json()['run_id'] == rid
    assert client.post('/api/chat-runs/'+rid+'/resume', json={}).status_code == 200
    assert len(model.calls) == calls
    assert len(kb.get_messages(cid)) == 2
    result = client.get('/api/chat-runs/'+rid+'/result').json()
    assert result['assistant']['qa_review_id']
    assert 'response' not in str(data['calls']) and 'request_hash' not in str(data['calls'])
    kb.delete_paper(pid)
    result = client.get('/api/chat-runs/'+rid+'/result').json()
    assert result['status'] == 'invalid_citation' and not result['sources']
    assert client.post(f'/api/conversations/{cid}/chat-runs', json={**req, 'question': 'different'}).status_code == 409


def test_failed_call_requires_explicit_retry_and_retains_unknown_usage(env):
    _, kb, model, client = env
    original = model.chat_json
    def failed(messages, task='', **kwargs):
        if task == 'qa':
            raise TimeoutError('secret body should not persist')
        return original(messages, task=task, **kwargs)
    model.chat_json = failed
    _, cid, rid, _ = start(env)
    data = wait(client, rid)
    assert data['status'] == 'failed', data
    assert data['retry_required'] and data['usage']['unknown_usage_calls'] >= 1
    assert len(kb.get_messages(cid)) == 1
    assert 'secret body' not in json.dumps(data)
    assert client.post('/api/chat-runs/'+rid+'/resume', json={}).status_code == 409
    model.chat_json = original
    assert client.post('/api/chat-runs/'+rid+'/resume', json={'retry_failed': True}).status_code == 200
    data = wait(client, rid)
    assert data['status'] == 'completed', data
    assert len(kb.get_messages(cid)) == 2
    assert [c['status'] for c in data['calls'] if c['task'] == 'qa'] == ['failed','succeeded']
    assert data['usage']['failed_calls'] == 1


def test_saved_response_survives_publication_failure_without_another_model_call(env, monkeypatch):
    from litprism import qa_reviews
    _, kb, model, client = env
    original = qa_reviews.save
    def fail(*args, **kwargs):
        raise RuntimeError('interrupted before publication')
    monkeypatch.setattr(qa_reviews, 'save', fail)
    _, cid, rid, _ = start(env)
    data = wait(client, rid)
    assert data['status'] == 'failed' and not data['retry_required']
    calls = len(model.calls)
    monkeypatch.setattr(qa_reviews, 'save', original)
    client.post('/api/chat-runs/'+rid+'/resume', json={})
    data = wait(client, rid)
    assert data['status'] == 'completed', data
    assert len(model.calls) == calls
    assert len(kb.get_messages(cid)) == 2
    assert data['usage']['calls'] == calls


@pytest.mark.parametrize('change', ['source', 'scope', 'history', 'model', 'memory'])
def test_changed_inputs_block_resume_without_sending(env, monkeypatch, change):
    from litprism import qa_reviews
    _, kb, model, client = env
    original = qa_reviews.save
    monkeypatch.setattr(qa_reviews, 'save', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop')))
    pid, cid, rid, _ = start(env)
    wait(client, rid)
    monkeypatch.setattr(qa_reviews, 'save', original)
    calls = len(model.calls)
    if change == 'source':
        kb.add_chunks(pid, [{'text':'replacement','page':2}], level='fulltext')
    elif change == 'scope':
        client.patch('/api/conversations/'+cid, json={'scope':{}})
    elif change == 'history':
        kb.append_message(cid,'user','new question')
    elif change == 'model':
        model.name = 'different-model'
    else:
        with kb.tx() as con:
            con.execute("INSERT INTO memories(id,kind,value,source) VALUES ('m-new','style_preference','short','explicit')")
    client.post('/api/chat-runs/'+rid+'/resume', json={})
    data = wait(client, rid)
    assert data['status'] == 'stale', data
    assert len(model.calls) == calls


def test_restart_keeps_completed_result_without_credentials(env, monkeypatch):
    from fastapi.testclient import TestClient
    from litprism.web import server
    cfg, _, model, client = env
    _, cid, rid, req = start(env)
    assert wait(client, rid)['status'] == 'completed'
    calls = len(model.calls)
    monkeypatch.setattr(server, 'build_llm', lambda _: (_ for _ in ()).throw(ValueError('no credentials')))
    reopened = TestClient(server.create_app(cfg))
    assert reopened.get('/api/chat-runs/'+rid+'/result').status_code == 200
    assert reopened.post(f'/api/conversations/{cid}/chat-runs', json=req).json()['run_id'] == rid
    assert len(model.calls) == calls


def test_restart_inflight_call_stays_unknown_until_explicit_retry(env):
    from fastapi.testclient import TestClient
    from litprism.web import server
    from litprism.chat_runs import ChatRuns, dump
    cfg, kb, model, client = env
    pid = paper(kb)
    cid = kb.add_conversation('Crash', scope={'paper_ids':[pid]})
    store = ChatRuns(kb)
    rid,_ = store.create(cid, {'question':'alphatopic','request_id':'crash-1234'}, cfg, model)
    # Crash after journal intent, before a response receipt. No provider call in this fixture.
    with kb.tx() as con:
        con.execute("UPDATE chat_runs SET status='running' WHERE id=?", (rid,))
        con.execute('INSERT INTO chat_run_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (rid,1,1,'qa','unknown-hash',dump([]),'started',None,None,None,None,'then','then'))
    reopened = TestClient(server.create_app(cfg))
    data = reopened.get('/api/chat-runs/'+rid).json()
    assert data['status'] == 'interrupted' and data['retry_required']
    assert data['usage']['unknown_usage_calls'] == 1 and not model.calls
    assert store.row(rid)['status'] == 'running'  # GET only projects interruption, never mutates the library.
    assert reopened.post('/api/chat-runs/'+rid+'/resume',json={}).status_code == 409
    assert not model.calls
    assert reopened.post('/api/chat-runs/'+rid+'/stop',json={}).json()['status'] == 'cancelled'
    assert store.calls(rid)[0]['status'] == 'started'  # Stopping does not erase unknown cost.


def test_unknown_inflight_exact_request_can_retry_after_restart(env, monkeypatch):
    from fastapi.testclient import TestClient
    from litprism.web import server
    from litprism import qa_reviews
    cfg, kb, model, client = env
    original = qa_reviews.save
    monkeypatch.setattr(qa_reviews, 'save', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop before publish')))
    _, cid, rid, _ = start(env)
    wait(client, rid)
    monkeypatch.setattr(qa_reviews, 'save', original)
    # Simulate losing the response receipt after the exact journaled send.
    with kb.tx() as con:
        con.execute("UPDATE chat_run_calls SET status='started',response=NULL,usage=NULL WHERE run_id=? AND task='qa'", (rid,))
        con.execute("UPDATE chat_runs SET status='running' WHERE id=?", (rid,))
    before = len(model.calls)
    reopened = TestClient(server.create_app(cfg))
    assert reopened.get('/api/chat-runs/'+rid).json()['status'] == 'interrupted'
    assert len(model.calls) == before
    reopened.post('/api/chat-runs/'+rid+'/resume', json={'retry_failed':True})
    data = wait(reopened, rid)
    assert data['status'] == 'completed', data
    assert len(model.calls) == before+1
    assert data['usage']['unknown_usage_calls'] >= 1
    assert [c['status'] for c in data['calls'] if c['task']=='qa'] == ['started','succeeded']
    assert len(kb.get_messages(cid)) == 2


def test_cancel_after_response_keeps_receipt_and_reuses_without_charge(env):
    _, kb, model, client = env
    original = model.chat_json
    def cancel_after_response(messages, task='', **kwargs):
        result = original(messages, task=task, **kwargs)
        if task == 'qa':
            for job in client.app.state.tasks.items.values():
                if job['status'] == 'running':
                    job['cancel'].set()
        return result
    model.chat_json = cancel_after_response
    _, cid, rid, _ = start(env)
    data = wait(client, rid)
    assert data['status'] == 'cancelled' and not data['retry_required'], data
    assert len(kb.get_messages(cid)) == 1
    before = len(model.calls)
    model.chat_json = original
    client.post('/api/chat-runs/'+rid+'/resume', json={})
    assert wait(client, rid)['status'] == 'completed'
    assert len(model.calls) == before and len(kb.get_messages(cid)) == 2


def test_postpublication_error_cannot_downgrade_persisted_result(env):
    from litprism.chat_runs import ChatRuns
    cfg, kb, model, _ = env
    pid = paper(kb)
    cid = kb.add_conversation('Publish',scope={'paper_ids':[pid]})
    store = ChatRuns(kb)
    rid,_ = store.create(cid, {'question':'alpha','request_id':'published-123'}, cfg, model)
    def process(*args, execution=None):
        with kb.tx():
            execution['publish']({'saved':True})
        raise RuntimeError('postpublication failure')
    assert store.execute(rid,cfg,model,process)['status'] == 'completed'
    assert json.loads(store.row(rid)['result']) == {'saved':True}


def test_chat_run_backup_restores_receipts_without_model_calls(env, tmp_path):
    from litprism import backup
    from litprism.config import load_config
    from litprism.db import KnowledgeBase
    from litprism.chat_runs import ChatRuns
    cfg,kb,_,client = env
    _,_,rid,_ = start(env)
    assert wait(client,rid)['status'] == 'completed'
    archive = tmp_path/'chat-run.zip'
    backup.create_backup(kb,cfg,archive)
    result = backup.restore_backup(archive,tmp_path.parent/(tmp_path.name+'-restored'),current_root=cfg.data_path)
    restored = KnowledgeBase(load_config(result['config_path']))
    try:
        store = ChatRuns(restored)
        assert store.detail(rid)['status'] == 'completed'
        assert store.calls(rid) == ChatRuns(kb).calls(rid)
        assert store.row(rid)['result'] == ChatRuns(kb).row(rid)['result']
    finally:
        restored.close()


@pytest.mark.parametrize('field,value', [('question',{}),('question',None),('top_k',True),('web_search','yes')])
def test_malformed_duplicate_receipt_is_rejected(env, field, value):
    _,_,_,client = env
    _,cid,rid,req = start(env)
    wait(client,rid)
    assert client.post(f'/api/conversations/{cid}/chat-runs',json={**req,field:value}).status_code == 400


def test_status_completion_during_read_keeps_polling_for_final_snapshot(env, monkeypatch):
    import threading
    from litprism.chat_runs import ChatRuns
    _, kb, _, client = env
    release, started = threading.Event(), threading.Event()
    def execute(store, rid, *args, **kwargs):
        started.set()
        assert release.wait(5)
        store.update(rid, 'stale', 'changed inputs')
        return {}
    monkeypatch.setattr(ChatRuns, 'execute', execute)
    _, _, rid, _ = start(env)
    assert started.wait(2)
    store = client.app.state.chat_runs
    original_detail = store.detail
    def racing_detail(key):
        snapshot = original_detail(key)
        release.set()
        deadline = time.monotonic()+3
        while any(t['status'] in ('queued','running') for t in client.app.state.tasks.items.values()):
            assert time.monotonic() < deadline
            time.sleep(.001)
        return snapshot
    monkeypatch.setattr(store, 'detail', racing_detail)
    first = client.get('/api/chat-runs/'+rid).json()
    assert first['status'] == 'queued' and first['active']
    monkeypatch.setattr(store, 'detail', original_detail)
    final = client.get('/api/chat-runs/'+rid).json()
    assert final['status'] == 'stale' and not final['active']
