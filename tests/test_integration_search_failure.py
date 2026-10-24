"""Search failures must report failure, never turn error bodies into evidence."""
import requests
import pytest

from test_workflow_guards import env  # noqa: F401
from litprism.secrets import SecretStore


@pytest.mark.parametrize('failure',['http','schema'])
def test_discovery_failure_does_not_create_sources_or_leak_key(env,monkeypatch,failure):
    cfg,kb,_,client=env
    cfg.websearch.provider='tavily'
    cfg.websearch.api_key_ref=SecretStore(cfg.data_path).set('test-search','synthetic-search-secret')
    calls=[]
    def fail(url,**kwargs):
        calls.append(url)
        response=requests.Response();response.status_code=503 if failure=='http' else 200
        response._content=b'{"error":"synthetic-search-secret"}' if failure=='http' else b'{"results":{"invalid":"schema"}}'
        return response
    monkeypatch.setattr(requests,'post',fail)
    before=kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]
    response=client.post('/api/evidence/search',json={'query':'test-search'})
    assert response.status_code==502 and len(calls)==1
    assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]==before
    assert 'synthetic-search-secret' not in response.text
