"""Acceptance infrastructure must preserve evidence and never report semantics from mocks."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


def tool():
    path = Path(__file__).resolve().parents[1] / 'tools/chat_mixed_acceptance.py'
    spec = importlib.util.spec_from_file_location('mixed_acceptance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_snapshot_rejects_changed_bytes_or_missing_acquisition_provenance():
    t = tool()
    row = {'id': 'web', 'url': 'https://arxiv.org/abs/1810.04805', 'content': 'actual excerpt',
           'obtained_at': '2026-10-24T00:00:00Z', 'acquisition': 'direct_abstract',
           'content_sha256': hashlib.sha256(b'actual excerpt').hexdigest()}
    assert t.validate_snapshots([row])['web'] == row
    with pytest.raises(ValueError):
        t.validate_snapshots([dict(row, content='different plausible excerpt')])
    with pytest.raises(ValueError):
        t.validate_snapshots([dict(row, acquisition='')])


def test_policy_score_detects_focus_silently_turned_into_strict():
    t = tool()
    # Actual acceptance counterexample; the score must flag this implementation
    # behavior rather than reuse the same parser to generate the expectation.
    observed = {'strict': True}  # c1 saved receipt, despite a focus scenario
    assert not t.scope_policy_matches(observed, expected_strict=False)
    assert t.scope_policy_matches({'strict': True}, expected_strict=True)
    assert not t.scope_policy_matches(None, expected_strict=False)


def test_acceptance_database_copy_never_migrates_or_overwrites_source(tmp_path):
    t = tool()
    source, target = tmp_path / 'old.db', tmp_path / 'copy.db'
    c = sqlite3.connect(source)
    c.execute('PRAGMA user_version=14')
    c.execute('CREATE TABLE original(value)')
    c.execute("INSERT INTO original VALUES ('frozen')")
    c.commit()
    c.close()
    before = source.read_bytes()
    t.backup_readonly(source, target)
    copy = sqlite3.connect(target)
    copy.execute('PRAGMA user_version=15')
    copy.commit()
    assert copy.execute('SELECT value FROM original').fetchone()[0] == 'frozen'
    copy.close()
    assert source.read_bytes() == before
    with pytest.raises(ValueError):
        t.backup_readonly(source, target)


def test_source_aware_audit_checks_frozen_reference_and_explicit_page(tmp_path):
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    from litprism.qa_reviews import source_hash
    t = tool()
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    try:
        pid = kb.add_paper(title='Study', verify_level='verified')
        kb.add_chunks(pid, [{'text': 'actual passage', 'page': 3}])
        chunk = kb.get_chunks(pid)[0]
        ref = dict(tag='P1', chunk_id=chunk['id'], paper_id=pid, page=3,
                   material_id=chunk['material_id'], source_hash=source_hash(dict(chunk)))
        payload = {'_citations': [ref], 'scope': {'paper_ids': [pid], 'strict': True}}
        with kb.tx() as con:
            con.execute('INSERT INTO semantic_runs VALUES (?,?,?,?)', ('qr_test', 'test', json.dumps(payload), 'now'))
        review = {'id': 'qr_test', 'sources': [dict(ref, source_kind='paper')]}
        assert t.citation_audit(kb, review, 'grounded [P1 p.3]')[0]['valid']
        assert not t.citation_audit(kb, review, 'wrong page [P1 p.4]')[0]['valid']
        assert not t.citation_audit(kb, review, 'unknown source [P9]')[0]['valid']
        kb.update_paper(pid, verify_level='unchecked')
        assert not t.citation_audit(kb, review, 'withdrawn [P1]')[0]['valid']
    finally:
        kb.close()


def test_confirm_control_requires_actual_supporting_passage():
    t = tool()
    class Response:
        def json(self):
            return {'sources': [{'paper_id': 'paper', 'citable': True, 'page': 1, 'text': 'author names only'}]}
    class Client:
        def get(self, path):
            return Response()
        def post(self, *args, **kwargs):
            pytest.fail('An author header must never be promoted as support')
    with pytest.raises(ValueError, match='Direct supporting abstract'):
        t.confirm(Client(), {'review': {'id': 'qr_test'}}, 'paper', 'Transformer 不依赖循环或卷积。')


def test_offline_suite_exercises_durable_scope_and_retains_semantic_unknown(tmp_path, monkeypatch):
    import yaml
    from litprism.config import Config, config_to_dict
    from litprism.db import KnowledgeBase
    from litprism.web import server
    t = tool()
    home = tmp_path / '_selftest.mixed'
    home.mkdir()
    cfg = Config()
    cfg.app.data_dir = str(home / 'data')
    cfg.export.notes_dir = str(home / 'notes')
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Study', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'actual passage', 'page': 1}])
    kb.close()
    (home / 'config').mkdir()
    (home / 'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(cfg)), encoding='utf-8')
    snapshot = {'id': 'web', 'url': 'https://arxiv.org/abs/1810.04805', 'content': 'actual excerpt',
                'obtained_at': '2026-10-24T00:00:00Z', 'acquisition': 'direct_abstract',
                'content_sha256': hashlib.sha256(b'actual excerpt').hexdigest()}
    t.write(home / 'web-snapshots.json', [snapshot])
    t.write(home / 'cases.json', {'groups': [{'id': 'strict', 'available': [], 'focus': [],
            'mode': 'strict', 'cases': [{'id': 'n0', 'web': [],
                'question': 'Anything?', 'expect': 'No citable sources in local-only mode, no model requests'}]}]})
    t.write(home / 'manifest.json', {'code_sha256': t.fingerprints(), 'cases_sha256': t.sha(home / 'cases.json'),
            'snapshots_sha256': t.sha(home / 'web-snapshots.json'), 'papers': [{'alias': 'transformer', 'paper_id': pid}],
            'planned_turns': 1, 'search_mode': 'controlled_fixture'})
    def no_model(*args, **kwargs):
        raise AssertionError('No available local evidence may build a client but must not send model requests')
    from litprism.llm import MockClient
    monkeypatch.setattr(MockClient, 'chat_json', no_model)
    t.run(home)
    result = json.loads((home / 'results.json').read_text(encoding='utf-8'))
    assert result['all_mechanical_checks_passed']
    assert result['usage']['calls'] == 0 and result['semantic_passed'] is None
    assert result['live_tavily_passed'] is None
    with pytest.raises(FileExistsError):
        t.run(home)
