"""Growing libraries must remain searchable without widening explicit selections."""
import pytest
from fastapi.testclient import TestClient

from litprism.config import Config
from litprism.db import KnowledgeBase
from litprism.web import server


@pytest.fixture
def library(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    client = TestClient(server.create_app(cfg))
    yield kb, client
    kb.close()


def test_old_paper_search_and_catalog_are_not_truncated(library):
    kb, client = library
    oldest = kb.add_paper(title='Oldest sentinel', authors=['Unique author'], verify_level='verified')
    kb.rename_paper(oldest, 'Custom oldest title')
    for i in range(300):
        kb.add_paper(title=f'Fixture {i}', verify_level='verified')
    with kb.tx() as con:
        con.execute('UPDATE papers SET created_at=?', ('2026-01-01',))
    first = client.get('/api/papers?limit=50').json()
    assert first['total'] == 301 and first['has_more'] and first['count'] == 50
    assert oldest not in {p['id'] for p in first['items']}
    for q in ('Unique author', 'Custom oldest title', 'Oldest sentinel'):
        result = client.get('/api/papers', params={'q': q}).json()
        assert result['total'] == 1 and result['items'][0]['id'] == oldest
    pages = [client.get('/api/papers', params={'limit': 50, 'offset': i}).json() for i in range(0, 301, 50)]
    ids = [p['id'] for page in pages for p in page['items']]
    assert len(ids) == len(set(ids)) == 301 and ids[-1] == oldest
    assert not pages[-1]['has_more']
    catalog = client.get('/api/papers/catalog').json()
    assert catalog['count'] == 301 and oldest in {p['id'] for p in catalog['items']}
    assert not any('full_text' in p or 'abstract' in p or 'verify_evidence' in p for p in catalog['items'])


def test_group_filter_uses_all_descendants_and_never_widens(library):
    kb, client = library
    root = client.post('/api/collections', json={'name': 'Parent'}).json()['id']
    child = client.post('/api/collections', json={'name': 'Child', 'parent_id': root}).json()['id']
    empty = client.post('/api/collections', json={'name': 'Empty'}).json()['id']
    inside = kb.add_paper(title='In child', verify_level='verified')
    kb.add_paper(title='Outside', verify_level='verified')
    with kb.tx() as con:
        con.execute('UPDATE papers SET collection_id=? WHERE id=?', (child, inside))
    result = client.get('/api/papers', params={'collection_id': root}).json()
    assert result['total'] == 1 and result['items'][0]['id'] == inside
    assert client.get('/api/papers', params={'collection_id': empty}).json()['total'] == 0
    assert client.get('/api/papers?collection_id=missing').status_code == 409
    assert client.delete('/api/paper/' + inside).status_code == 200
    assert client.get('/api/papers', params={'collection_id': root}).json()['total'] == 0


def test_literal_search_and_invalid_paging(library):
    kb, client = library
    kb.add_paper(title='Literal 10%_results', verify_level='verified')
    kb.add_paper(title='Other', verify_level='verified')
    assert client.get('/api/papers', params={'q': '%_'}).json()['total'] == 1
    for params in ({'limit': 0}, {'offset': -1}, {'limit': 2001}):
        assert client.get('/api/papers', params=params).status_code == 422


def test_older_conversations_have_stable_pages_and_search(library):
    kb, client = library
    ids = [kb.add_conversation(f'Conversation {i}') for i in range(101)]
    first = client.get('/api/conversations').json()
    assert first['total'] == 101 and first['has_more'] and len(first['items']) == 100
    last = client.get('/api/conversations?offset=100').json()
    assert [c['id'] for c in last['items']] == [ids[0]] and not last['has_more']
    kb.append_message(ids[0], 'user', 'unique old message')
    found = client.get('/api/conversations', params={'q': 'unique old message'}).json()
    assert found['total'] == 1 and found['items'][0]['id'] == ids[0]
