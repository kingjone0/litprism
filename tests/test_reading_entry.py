"""Reading preparation is source-aware and never sends an unapproved duplicate request."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from litprism.config import Config
from litprism.db import KnowledgeBase
from litprism.web import server


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    api = TestClient(server.create_app(cfg))
    yield cfg, kb, api
    kb.close()


@pytest.mark.parametrize('level', ['metadata', 'abstract', 'partial', 'fulltext', 'unknown'])
def test_material_preparation_precedes_generation_and_uses_current_snapshot(env, level):
    _, kb, api = env
    pid = kb.add_paper(title='Prepared paper', verify_level='verified', material_level='fulltext')
    kb.add_chunks(pid, [{'text': 'Current indexed evidence. ' * 12, 'page': 1}], level=level)
    prepared = api.get(f'/api/paper/{pid}/reading').json()
    assert prepared['level'] == level and prepared['ready'] and not prepared['has_analysis']
    assert prepared['material_id'] == kb.get_paper(pid)['current_material_id']
    assert prepared['chunks'] == 1 and prepared['indexed_chars'] > 150
    assert prepared['active_run'] is None
    assert api.get('/api/papers').json()['items'][0]['current_material_level'] == level
    assert api.get('/api/papers/catalog').json()['items'][0]['current_material_level'] == level
    assert api.get(f'/api/paper/{pid}').json()['reading']['level'] == level


def test_insufficient_material_stops_before_model_setup_or_run_creation(env, monkeypatch):
    _, kb, api = env
    pid = kb.add_paper(title='Metadata only', verify_level='verified', material_level='metadata')
    monkeypatch.setattr(server, 'build_llm', lambda _: pytest.fail('Model must not be constructed'))
    prepared = api.get(f'/api/paper/{pid}/reading').json()
    assert not prepared['ready'] and prepared['reason']
    assert api.post(f'/api/paper/{pid}/analysis-runs', json={}).status_code == 400
    assert api.get(f'/api/paper/{pid}/analysis-runs').json()['items'] == []


def test_changed_prepared_material_is_not_silently_replaced(env, monkeypatch):
    _, kb, api = env
    pid = kb.add_paper(title='Versioned preparation', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'Original evidence. ' * 20}], level='abstract')
    prepared = api.get(f'/api/paper/{pid}/reading').json()
    kb.save_analysis(pid, {'key_conclusions': 'Keep previous analysis'})
    kb.add_chunks(pid, [{'text': 'Replacement evidence. ' * 20}], level='partial')
    monkeypatch.setattr(server, 'build_llm', lambda _: pytest.fail('No request after material change'))
    response = api.post(f'/api/paper/{pid}/analysis-runs', json={'material_id': prepared['material_id']})
    assert response.status_code == 409
    assert kb.get_analysis(pid)['payload']['key_conclusions'] == 'Keep previous analysis'


def test_active_paper_run_is_visible_and_cannot_be_started_twice(env, monkeypatch):
    _, kb, api = env
    pid = kb.add_paper(title='One active reading', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'Evidence for a pending call. ' * 20}], level='fulltext')
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        name = 'offline:reading-entry'
        calls = 0
        def chat_json(self, messages, *, task=None):
            self.calls += 1
            entered.set()
            assert release.wait(8)
            raise TimeoutError('Deliberate unknown outcome')

    model = Blocked()
    monkeypatch.setattr(server, 'build_llm', lambda _: model)
    started = api.post(f'/api/paper/{pid}/analysis-runs', json={'with_concepts': False}).json()
    try:
        assert entered.wait(5)
        active = api.get(f'/api/paper/{pid}/reading').json()['active_run']
        assert active['id'] == started['run_id'] and active['task_id'] == started['task_id'] and active['active']
        assert api.post(f'/api/paper/{pid}/analysis-runs', json={}).status_code == 409
        assert model.calls == 1
    finally:
        release.set()
        for _ in range(500):
            task = api.get('/api/tasks/' + started['task_id']).json()
            if task['status'] in ('succeeded', 'failed', 'cancelled'):
                break
            time.sleep(.01)
    assert not api.get('/api/analysis-runs/' + started['run_id']).json()['active']
