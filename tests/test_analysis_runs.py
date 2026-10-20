"""Budget/resume tests use a deterministic offline client, never a paid API."""
import json
import sqlite3
import threading
import time

import pytest

from litprism.analysis_runs import AnalysisRuns, JournalClient, RunConflict, _run_lock
from litprism.config import Config
from litprism.db import KnowledgeBase


class Client:
    name = 'offline:durable-analysis'
    supports_semantic_review = True

    def __init__(self, cfg):
        self.cfg, self.calls = cfg.llm, []

    def chat_json(self, messages, *, task=None):
        self.calls.append(task)
        self.last_usage = {'prompt_tokens': 10, 'completion_tokens': 5}
        if task == 'extract':
            return {'key_conclusions': '模型A为71.8 [P1 p.1]。模型B为70.0 [P1 p.1]。'}
        if task == 'concepts':
            return {'concepts': []}
        if task == 'semantic_cross_review':
            return {'differences': []}
        request = json.loads(messages[-1]['content'])
        return {'reviews': [{'id': c['id'], 'verdict': 'supported', 'reason': 'offline fixture', 'evidence': ['P1']}
                            for c in request['claims']]}


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Durable study', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'A study describes model A with score 71.8 and model B with score 70.0. '*35,
                         'page': 1, 'section': '结果'}])
    yield cfg, kb, pid
    kb.close()


def test_stage_budgets_no_longer_stop_a_run(env):
    """阶段调用预算已取消：给很小的预算也照常跑完，不再 paused_budget。"""
    cfg, kb, pid = env
    kb.save_analysis(pid, {'key_conclusions': 'previous accepted analysis'})
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid, {'semantic_review': 1})['id']
    done = store.execute(rid, cfg, client)
    assert done['status'] == 'completed'
    assert client.calls[:2] == ['extract', 'concepts']
    assert 'semantic_review' in client.calls and client.calls[-1] == 'semantic_cross_review'
    payload = kb.get_analysis(pid)['payload']
    assert payload['_analysis_run_id'] == rid
    assert payload['_semantic_review']['usage']['calls'] == 3


def test_resume_after_database_reopen(env):
    cfg, kb, pid = env
    rid = AnalysisRuns(kb).create(cfg, Client(cfg), pid, {'extract': 0})['id']
    path = kb.db_path
    kb.close()
    reopened = KnowledgeBase(cfg, db_path=path)
    try:
        done = AnalysisRuns(reopened).execute(rid, cfg, Client(cfg))
        assert done['status'] == 'completed'
        assert done['spent']['extract'] >= 1
    finally:
        reopened.close()


@pytest.mark.parametrize('change', ['source', 'model', 'endpoint', 'temperature', 'context', 'analysis'])
def test_incompatible_resume_stops_before_any_model_call(env, change):
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid)['id']
    if change == 'source':
        kb.add_chunks(pid, [{'text': 'new source', 'page': 2}])
    elif change == 'analysis':
        kb.save_analysis(pid, {'research_question': 'new analysis'})
    elif change == 'context':
        cfg.context.window_tokens += 1000
    else:
        name = {'model': 'model', 'endpoint': 'base_url', 'temperature': 'temperature'}[change]
        setattr(client.cfg, name, .9 if change == 'temperature' else 'changed')
    with pytest.raises(RunConflict):
        store.execute(rid, cfg, client)
    assert not client.calls and not store.calls(rid)
    assert store.detail(rid)['status'] == 'stale'


def test_interrupted_request_needs_explicit_retry_and_counts_unknown_cost(env):
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid)['id']
    original = client.chat_json
    def fail(*args, **kwargs):
        raise TimeoutError('provider error may contain secrets')
    client.chat_json = fail
    assert store.execute(rid, cfg, client)['status'] == 'failed'
    with kb.tx() as con:
        con.execute("UPDATE analysis_run_calls SET status='started', error_type=NULL WHERE run_id=?", (rid,))
    client.chat_json = original
    blocked = store.execute(rid, cfg, client)
    assert blocked['status'] == 'needs_retry' and not client.calls
    assert blocked['usage']['unknown_usage_calls'] == 1
    done = store.execute(rid, cfg, client, retry_failed=True)
    assert done['status'] == 'completed'
    assert done['spent']['extract'] == 2
    assert done['usage']['unknown_usage_calls'] == 1
    assert not done['usage']['complete']
    assert store.calls(rid)[0]['status'] == 'started'


def test_second_worker_cannot_run_same_request(env):
    cfg, kb, pid = env
    store = AnalysisRuns(kb)
    rid = store.create(cfg, Client(cfg), pid)['id']
    errors = []
    def other():
        try:
            store.execute(rid, cfg, Client(cfg))
        except RunConflict:
            errors.append('locked')
    with _run_lock(kb, rid):
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(timeout=3)
    assert errors == ['locked'] and not store.calls(rid)


def test_no_secrets_or_messages_are_in_public_run_detail(env):
    cfg, kb, pid = env
    cfg.llm.api_key = 'private-token-that-must-not-be-stored'
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid)['id']
    detail = store.execute(rid, cfg, client)
    assert 'private-token' not in json.dumps(detail)
    assert 'messages' not in detail and 'response' not in detail
    row = store.row(rid)
    assert 'private-token' not in row['binding']


def test_changed_connect_timeout_rejects_resume_before_call(env, monkeypatch):
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    monkeypatch.setenv('LITPRISM_REVIEW_CONNECT_TIMEOUT', '10')
    rid = store.create(cfg, client, pid)['id']
    monkeypatch.setenv('LITPRISM_REVIEW_CONNECT_TIMEOUT', '30')
    with pytest.raises(RunConflict):
        store.execute(rid, cfg, client)
    assert not client.calls and store.detail(rid)['status'] == 'stale'


@pytest.mark.parametrize('limits', [{'extract': True}, {'unknown': 1}, {'extract': -1}, {'concepts': 1.5}])
def test_invalid_budget_is_rejected_without_creating_run(env, limits):
    cfg, kb, pid = env
    with pytest.raises(ValueError):
        AnalysisRuns(kb).create(cfg, Client(cfg), pid, limits)
    assert not AnalysisRuns(kb).list(pid)


@pytest.mark.parametrize('change', ['source', 'analysis'])
def test_change_during_generation_prevents_publication(env, change):
    cfg, kb, pid = env
    kb.save_analysis(pid, {'research_question': 'original'})
    store, client = AnalysisRuns(kb), Client(cfg)
    original = client.chat_json
    def alter(*args, **kwargs):
        if kwargs['task'] == 'extract':
            if change == 'source':
                kb.add_chunks(pid, [{'text': 'changed during call', 'page': 2}])
            else:
                kb.save_analysis(pid, {'research_question': 'new manual analysis'})
        return original(*args, **kwargs)
    client.chat_json = alter
    rid = store.create(cfg, client, pid)['id']
    assert store.execute(rid, cfg, client)['status'] == 'stale'
    assert kb.get_analysis(pid)['payload'] == {'research_question': 'original' if change == 'source' else 'new manual analysis'}
    assert not kb.list_concepts(pid)


def test_replayed_success_does_not_multiply_usage(env):
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid)['id']
    done = store.execute(rid, cfg, client)
    client.calls.clear()
    repeated = store.execute(rid, cfg, client)
    assert repeated['status'] == 'completed'
    assert repeated['usage'] == done['usage'] and not client.calls


def test_truncated_concepts_remain_partial_and_require_explicit_retry(env):
    from litprism.llm import OutputLimitError
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    original = client.chat_json
    def truncated(*args, **kwargs):
        if kwargs['task'] == 'concepts':
            raise OutputLimitError('private provider detail')
        return original(*args, **kwargs)
    client.chat_json = truncated
    rid = store.create(cfg, client, pid)['id']
    result = store.execute(rid, cfg, client)
    assert result['status'] == 'partial' and result['retry_required']
    assert kb.get_analysis(pid)['payload']['_semantic_review']['passed']
    assert 'private provider' not in json.dumps(kb.get_analysis(pid)['payload'])
    client.chat_json = original
    client.calls.clear()
    done = store.execute(rid, cfg, client, retry_failed=True)
    assert done['status'] == 'completed' and client.calls == ['concepts']


def test_retry_never_refunds_budget_and_downstream_cache_is_request_bound(env):
    from litprism.analysis_runs import RunCallFailed
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    limits = {'extract': 1, 'concepts': 1, 'semantic_review': 1, 'semantic_cross_review': 1}
    rid = store.create(cfg, client, pid, limits)['id']
    journal = JournalClient(store, rid, client, limits, False)
    original = client.chat_json
    client.chat_json = lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(RunCallFailed):
        journal.chat_json([{'content': 'original'}], task='extract')
    client.chat_json = original
    # 预算不再拦截：哪怕超出旧限额也继续，只按日志做重放/新增。
    assert len(store.calls(rid)) == 1
    limits['extract'] = 2
    retried = JournalClient(store, rid, client, limits, True)
    retried.chat_json([{'content': 'original'}], task='extract')
    retried.chat_json([{'content': 'old downstream'}], task='concepts')
    replay = JournalClient(store, rid, client, limits | {'concepts': 2}, False)
    replay.chat_json([{'content': 'original'}], task='extract')
    replay.chat_json([{'content': 'new downstream'}], task='concepts')
    assert not replay.last_call_replayed and store.detail(rid)['spent']['concepts'] == 2
    assert [c['attempt'] for c in store.calls(rid)] == [1, 2, 1, 2]


def test_mismatched_request_without_retried_parent_is_never_sent(env):
    cfg, kb, pid = env
    store, client = AnalysisRuns(kb), Client(cfg)
    rid = store.create(cfg, client, pid)['id']
    limits = store.detail(rid)['budgets']
    JournalClient(store, rid, client, limits, False).chat_json([{'content': 'first'}], task='extract')
    with pytest.raises(RunConflict):
        JournalClient(store, rid, client, limits, False).chat_json([{'content': 'different'}], task='extract')
    assert client.calls == ['extract']


def test_migration_from_v6_preserves_existing_analysis_and_is_idempotent(tmp_path):
    from litprism.schema import MIGRATIONS, SCHEMA_VERSION, apply_migrations, current_version
    con = sqlite3.connect(tmp_path/'old.sqlite')
    try:
        for migration in MIGRATIONS:
            if migration.version <= 6:
                migration.apply(con)
        con.execute('PRAGMA user_version=6')
        con.execute("INSERT INTO papers(id,title) VALUES ('old','old paper')")
        con.execute("INSERT INTO analyses(paper_id,payload) VALUES ('old',?)", ('{"research_question":"preserved"}',))
        con.commit()
        assert len(apply_migrations(con)) == SCHEMA_VERSION - 6
        assert current_version(con) == SCHEMA_VERSION
        assert not apply_migrations(con)
        assert con.execute('SELECT COUNT(*) FROM analysis_runs').fetchone()[0] == 0
        assert json.loads(con.execute("SELECT payload FROM analyses WHERE paper_id='old'").fetchone()[0]) == {'research_question': 'preserved'}
    finally:
        con.close()


@pytest.mark.parametrize('provider', ['openai', 'anthropic'])
@pytest.mark.parametrize('failure', ['timeout', 'response_format'])
def test_durable_transport_never_retries_internally(env, monkeypatch, provider, failure):
    from types import SimpleNamespace

    import requests

    from litprism.llm import AnthropicClient, OpenAICompatClient
    cfg, kb, pid = env
    cfg.llm.provider = provider
    client = (AnthropicClient if provider == 'anthropic' else OpenAICompatClient)(cfg.llm)
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        if failure == 'timeout':
            raise requests.exceptions.ReadTimeout('redacted')
        return SimpleNamespace(status_code=400, text='response_format not supported')
    monkeypatch.setattr(requests, 'post', post)
    store = AnalysisRuns(kb)
    rid = store.create(cfg, client, pid)['id']
    result = store.execute(rid, cfg, client)
    assert result['status'] == 'failed'
    assert len(calls) == 1 and result['spent']['extract'] == 1


def wait_api_task(api, tid):
    deadline = time.monotonic()+8
    while time.monotonic() < deadline:
        result = api.get('/api/tasks/'+tid).json()
        if result['status'] in ('succeeded', 'failed', 'cancelled'):
            return result
        time.sleep(.01)
    pytest.fail('offline task did not finish')


def test_api_pause_and_resume_are_persistent_and_input_checked(env, monkeypatch):
    from fastapi.testclient import TestClient

    from litprism.web import server
    cfg, kb, pid = env
    model = Client(cfg)
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    monkeypatch.setattr(server, 'build_llm', lambda _: model)
    api = TestClient(server.create_app(cfg))
    assert api.post(f'/api/paper/{pid}/analysis-runs', json={'budgets': {'extract': True}}).status_code == 400
    assert api.get('/api/analysis-runs/missing').status_code == 404
    started = api.post(f'/api/paper/{pid}/analysis-runs', json={'budgets': {'semantic_review': 1}}).json()
    # 预算已取消：小预算不再暂停，直接跑完。
    assert wait_api_task(api, started['task_id'])['result']['status'] == 'completed'
    rid = started['run_id']
    assert api.get(f'/api/paper/{pid}/analysis-runs').json()['items'][0]['id'] == rid
    assert api.post(f'/api/analysis-runs/{rid}/resume', json={'retry_failed': 'yes'}).status_code == 400
    model.calls.clear()
    resumed = api.post(f'/api/analysis-runs/{rid}/resume', json={}).json()
    assert wait_api_task(api, resumed['task_id'])['result']['status'] == 'completed'
    assert model.calls == []
    # A fresh web app retains records; the transient TaskManager is not the journal.
    reopened_api = TestClient(server.create_app(cfg))
    assert reopened_api.get(f'/api/analysis-runs/{rid}').json()['status'] == 'completed'


def test_api_duplicate_worker_rejected_and_cancellation_preserves_checkpoint(env, monkeypatch):
    from fastapi.testclient import TestClient

    from litprism.web import server
    cfg, kb, pid = env
    model = Client(cfg)
    entered, release = threading.Event(), threading.Event()
    original = model.chat_json
    def block(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    model.chat_json = block
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    monkeypatch.setattr(server, 'build_llm', lambda _: model)
    api = TestClient(server.create_app(cfg))
    started = api.post(f'/api/paper/{pid}/analysis-runs', json={}).json()
    rid, tid = started['run_id'], started['task_id']
    try:
        assert entered.wait(3)
        assert api.post(f'/api/analysis-runs/{rid}/resume', json={}).status_code == 409
        api.post(f'/api/tasks/{tid}/cancel')
    finally:
        release.set()
    assert wait_api_task(api, tid)['status'] == 'cancelled'
    record = api.get(f'/api/analysis-runs/{rid}').json()
    assert record['status'] == 'cancelled' and record['spent']['extract'] == 1
    assert model.calls == ['extract'] and not kb.get_analysis(pid)
    model.chat_json = original
    model.calls.clear()
    resumed = api.post(f'/api/analysis-runs/{rid}/resume', json={}).json()
    assert wait_api_task(api, resumed['task_id'])['result']['status'] == 'completed'
    assert 'extract' not in model.calls
