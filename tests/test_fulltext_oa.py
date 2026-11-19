"""开放全文解析：只认合法开放副本，且必须区分"没有"与"没拿到"。"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from litprism.config import SourceConfig, SourcesConfig  # noqa: E402
from litprism import fulltext, remote  # noqa: E402


def cfg(unpaywall=True, openalex=True, email='me@example.org', mailto='me@example.org',
        s2=False, epmc=False, s2_key=''):
    return SimpleNamespace(sources=SourcesConfig(
        unpaywall=SourceConfig(enabled=unpaywall, mailto=email),
        openalex=SourceConfig(enabled=openalex, mailto=mailto),
        crossref=SourceConfig(mailto=mailto),
        arxiv=SourceConfig(),
        semantic_scholar=SourceConfig(enabled=s2, api_key=s2_key),
        europepmc=SourceConfig(enabled=epmc),
    ))


def test_unpaywall_pdf_hit():
    def fake(url, params):
        assert 'api.unpaywall.org' in url
        assert params['email'] == 'me@example.org'
        return {'is_oa': True, 'best_oa_location': {'url_for_pdf': 'https://repo.example.org/a.pdf',
                                                    'license': 'cc-by'}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'}, cfg(), fetch_json=fake)
    assert out.status == 'ok'
    assert out.copy.url == 'https://repo.example.org/a.pdf'
    assert out.copy.provider == 'unpaywall'
    assert out.copy.license == 'cc-by'


def test_openalex_fallback_when_unpaywall_misses():
    def fake(url, params):
        if 'unpaywall' in url:
            return {'is_oa': False, 'journal_is_oa': False}
        return {'best_oa_location': {'pdf_url': 'https://oa.example.org/b.pdf'}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'}, cfg(), fetch_json=fake)
    assert out.status == 'ok' and out.copy.provider == 'openalex'


def test_paywalled_is_reported_separately_from_no_oa():
    def fake(url, params):
        if 'unpaywall' in url:
            return {'is_oa': False, 'journal_is_oa': False, 'best_oa_location': None}
        return {'best_oa_location': None, 'locations': [], 'open_access': {'is_oa': False}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'}, cfg(), fetch_json=fake)
    assert out.status == 'paywalled'


def test_network_error_does_not_become_no_oa():
    def boom(url, params):
        raise TimeoutError('timed out')

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'}, cfg(), fetch_json=boom)
    assert out.status == 'network_error'


def test_missing_doi_is_explicit():
    out = fulltext.resolve_open_copy({'title': 'no doi here'}, cfg(), fetch_json=lambda *a, **k: {})
    assert out.status == 'no_doi'


def test_placeholder_email_skips_unpaywall():
    calls = []

    def fake(url, params):
        calls.append(url)
        return {'best_oa_location': {'pdf_url': 'https://oa.example.org/c.pdf'}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                                     cfg(email='your_email@example.com'), fetch_json=fake)
    assert out.status == 'ok'
    assert all('unpaywall' not in u for u in calls), '占位邮箱不应真的去请求 Unpaywall'


def test_public_url_guard_blocks_private_and_non_http():
    for bad in ('http://127.0.0.1/a.pdf', 'http://localhost/a.pdf',
                'http://192.168.1.5/a.pdf', 'file:///C:/a.pdf'):
        try:
            remote._check_public_url(bad)
        except ValueError:
            continue
        raise AssertionError(f'应拒绝 {bad}')


def test_target_name_is_stable_and_unique():
    doi_a = remote._target_name({'title': 'x'}, None, '10.1234/abc')
    doi_b = remote._target_name({'title': 'x'}, None, '10.1234/abd')
    assert doi_a != doi_b and doi_a.endswith('.pdf')
    assert '/' not in doi_a
    assert remote._target_name({'title': 'Paper'}, '1706.03762v3', None) == '1706.03762v3.pdf'


def test_landing_page_is_not_treated_as_pdf():
    def fake(url, params):
        if 'unpaywall' in url:
            return {'is_oa': True, 'best_oa_location': {'url': 'https://doi.org/10.1/abc'}}
        return {'best_oa_location': {'landing_page_url': 'https://doi.org/10.1/abc'}, 'locations': []}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'}, cfg(), fetch_json=fake)
    assert out.status != 'ok', '只有落地页时不能假装拿到了 PDF'


def test_semantic_scholar_open_pdf():
    def fake(url, params, headers=None):
        assert 'semanticscholar' in url
        assert params['fields'] == 'openAccessPdf,isOpenAccess'
        return {'openAccessPdf': {'url': 'https://s2.example.org/p.pdf'}, 'isOpenAccess': True}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                                     cfg(unpaywall=False, openalex=False, s2=True), fetch_json=fake)
    assert out.status == 'ok' and out.copy.provider == 'semantic_scholar'


def test_semantic_scholar_sends_api_key_when_configured():
    seen = {}

    def fake(url, params, headers=None):
        seen.update(headers or {})
        return {'openAccessPdf': None}

    fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                               cfg(unpaywall=False, openalex=False, s2=True, s2_key='k1'),
                               fetch_json=fake)
    assert seen.get('x-api-key') == 'k1'


def test_europepmc_pdf_entry():
    def fake(url, params, headers=None):
        assert 'europepmc' in url
        return {'resultList': {'result': [{
            'pmcid': 'PMC123',
            'hasPDF': 'Y',
            'fullTextUrlList': {'fullTextUrl': [
                {'url': 'https://europepmc.org/x.pdf', 'documentStyle': 'pdf', 'availability': 'Open access'}]},
        }]}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                                     cfg(unpaywall=False, openalex=False, epmc=True), fetch_json=fake)
    assert out.status == 'ok' and out.copy.provider == 'europepmc'


def test_provenance_is_recorded_on_candidate(tmp_path):
    import json
    from litprism.db import KnowledgeBase

    from litprism.config import Config

    cfg_obj = Config()
    cfg_obj.app.data_path = tmp_path
    kb = KnowledgeBase(cfg_obj, db_path=tmp_path / 'provenance.db')
    try:
        import uuid
        kb._con.execute("""INSERT INTO source_candidates
            (id,kind,title,fingerprint,status,created_at,updated_at)
            VALUES ('c1','paper','T',?,'new','2026-01-01','2026-01-01')""", (uuid.uuid4().hex,))
        remote._record_provenance(kb, 'c1', provider='openalex',
                                  url='https://oa.example.org/a.pdf', license='cc-by')
        row = kb._con.execute("SELECT extra FROM source_candidates WHERE id='c1'").fetchone()
        data = json.loads(row['extra'])['fulltext']
        assert data['provider'] == 'openalex'
        assert data['url'].endswith('a.pdf')
        assert data['license'] == 'cc-by'
        assert data['retrieved_at']
    finally:
        kb.close()


def test_semantic_scholar_open_pdf():
    def fake(url, params, headers=None):
        assert 'semanticscholar' in url
        assert params['fields'] == 'openAccessPdf,isOpenAccess'
        return {'openAccessPdf': {'url': 'https://s2.example.org/p.pdf'}, 'isOpenAccess': True}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                                     cfg(unpaywall=False, openalex=False, s2=True), fetch_json=fake)
    assert out.status == 'ok' and out.copy.provider == 'semantic_scholar'


def test_semantic_scholar_sends_api_key_when_configured():
    seen = {}

    def fake(url, params, headers=None):
        seen.update(headers or {})
        return {'openAccessPdf': None}

    fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                               cfg(unpaywall=False, openalex=False, s2=True, s2_key='k1'),
                               fetch_json=fake)
    assert seen.get('x-api-key') == 'k1'


def test_europepmc_pdf_entry():
    def fake(url, params, headers=None):
        assert 'europepmc' in url
        return {'resultList': {'result': [{
            'pmcid': 'PMC123',
            'hasPDF': 'Y',
            'fullTextUrlList': {'fullTextUrl': [
                {'url': 'https://europepmc.org/x.pdf', 'documentStyle': 'pdf', 'availability': 'Open access'}]},
        }]}}

    out = fulltext.resolve_open_copy({'doi': '10.1234/abc'},
                                     cfg(unpaywall=False, openalex=False, epmc=True), fetch_json=fake)
    assert out.status == 'ok' and out.copy.provider == 'europepmc'


def test_provenance_is_recorded_on_candidate(tmp_path):
    import json
    from litprism.db import KnowledgeBase

    from litprism.config import Config

    cfg_obj = Config()
    cfg_obj.app.data_path = tmp_path
    kb = KnowledgeBase(cfg_obj, db_path=tmp_path / 'provenance.db')
    try:
        import uuid
        kb._con.execute("""INSERT INTO source_candidates
            (id,kind,title,fingerprint,status,created_at,updated_at)
            VALUES ('c1','paper','T',?,'new','2026-01-01','2026-01-01')""", (uuid.uuid4().hex,))
        remote._record_provenance(kb, 'c1', provider='openalex',
                                  url='https://oa.example.org/a.pdf', license='cc-by')
        row = kb._con.execute("SELECT extra FROM source_candidates WHERE id='c1'").fetchone()
        data = json.loads(row['extra'])['fulltext']
        assert data['provider'] == 'openalex'
        assert data['url'].endswith('a.pdf')
        assert data['license'] == 'cc-by'
        assert data['retrieved_at']
    finally:
        kb.close()
