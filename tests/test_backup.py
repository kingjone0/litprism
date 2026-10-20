"""N08 uses real WAL SQLite and isolated files; no user DB or real API calls."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from litprism import backup
from litprism.config import Config, config_to_dict, load_config
from litprism.db import KnowledgeBase
from litprism.evidence import stored_excerpts
from litprism.knowledge import create_source, review_source
from litprism.manual_claims import add, list_claims
from litprism.semantics import payload_hash


@pytest.fixture
def library(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path/'source')
    cfg.config_path = tmp_path/'config.yaml'
    cfg.config_path.write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True), encoding='utf-8')
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Versioned paper', verify_level='verified', material_level='fulltext')
    kb.add_chunks(pid, [{'text': 'originalmarker old evidence', 'page': 1}], level='fulltext')
    old = kb.get_chunks(pid)[0]
    payload = {'research_question': 'old finding', '_citations': [
        {'tag': 'P1', 'paper_id': pid, 'chunk_id': old['id'], 'page': 1}]}
    kb.save_analysis(pid, payload)
    digest = payload_hash(payload)
    add(kb, pid, digest, {'field': 'methodology', 'text': 'manual finding',
                         'chunk_id': old['id'], 'quote': 'old evidence'})
    source = create_source(kb, kind='knowledge', title='reviewed source', content='reviewmarker sourced claim')
    review_source(kb, source['id'], action='confirm', note='checked source', content_hash=source['content_hash'])
    with kb.tx() as con:
        con.execute('INSERT INTO semantic_reviews VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('sr_test', pid, None, digest, 'claim1', 'confirm', 'checked', 'corrected', source['id'], '2026-10-20'))
        con.execute("INSERT INTO meta(key,value) VALUES ('test-preference','concise')")
        con.execute("INSERT INTO memories(id,kind,value,source) VALUES ('mem1','style_preference','concise','explicit')")
        con.execute("INSERT INTO deliverables(id,type,title,content) VALUES ('output1','notes','Research output','saved finding')")
        con.execute("INSERT INTO providers(id,name,base_url,api_key_ref) VALUES ('provider1','Saved provider','https://example.invalid/v1','fake-secret-reference')")
    kb.add_chunks(pid, [{'text': 'replacementmarker current evidence', 'page': 2}], level='fulltext')
    kb.save_analysis(pid, {'research_question': 'new finding'})
    conv = kb.add_conversation('Research history')
    kb.append_message(conv, role='user', content='A research question')
    (cfg.data_path/'papers').mkdir()
    (cfg.data_path/'papers'/'managed.pdf').write_bytes(b'%PDF-1.4 managed original')
    (cfg.notes_path/'note.md').write_text('saved research output', encoding='utf-8')
    (cfg.data_path/'skills').mkdir()
    (cfg.data_path/'skills'/'custom.yaml').write_text('name: custom', encoding='utf-8')
    # Deliberately fake credentials: only test exclusion, never access SecretStore.
    (cfg.data_path/'secrets.json').write_text('SECRET_STORE_MUST_NOT_TRANSFER', encoding='utf-8')
    (cfg.data_path/'.secrets.key').write_text('MASTER_KEY_MUST_NOT_TRANSFER', encoding='utf-8')
    cfg.llm.api_key = 'FAKE_CONFIG_KEY_MUST_NOT_TRANSFER'
    cfg.sources.semantic_scholar.api_key = 'FAKE_SOURCE_KEY_MUST_NOT_TRANSFER'
    yield cfg, kb, pid, old, payload
    kb.close()


def snapshot(library, tmp_path, include=True):
    cfg, kb, *_ = library
    archive = tmp_path/'backup.zip'
    manifest = backup.create_backup(kb, cfg, archive, include_originals=include)
    return archive, manifest


def rewrite_zip(archive, change):
    with zipfile.ZipFile(archive) as z:
        entries = {i.filename: z.read(i) for i in z.infolist()}
    change(entries)
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)


def test_export_restore_export_preserves_all_logical_content(library, tmp_path):
    cfg, kb, pid, old, payload = library
    archive, first = snapshot(library, tmp_path)
    assert backup.inspect_backup(archive) == first
    target = tmp_path/'restored'
    result = backup.restore_backup(archive, target, current_root=cfg.data_path)
    restored_cfg = load_config(result['config_path'])
    restored = KnowledgeBase(restored_cfg)
    try:
        assert restored.get_chunks(pid)[0]['text'].startswith('replacementmarker')
        assert stored_excerpts(restored, payload['_citations'])[0]['id'] == old['id']
        assert list_claims(restored, pid, payload_hash(payload))[0]['chunk_id'] == old['id']
        assert restored._con.execute('SELECT count(*) FROM semantic_reviews').fetchone()[0] == 1
        assert restored._con.execute('SELECT count(*) FROM analysis_versions').fetchone()[0] == 1
        assert restored._con.execute('SELECT value FROM memories').fetchone()[0] == 'concise'
        assert restored._con.execute('SELECT content FROM deliverables').fetchone()[0] == 'saved finding'
        assert restored._con.execute('SELECT api_key_ref FROM providers').fetchone()[0] == 'fake-secret-reference'
        assert (target/'papers'/'managed.pdf').read_bytes().startswith(b'%PDF')
        second = backup.create_backup(restored, restored_cfg, tmp_path/'second.zip', include_originals=True)
        assert second['content'] == first['content']
        assert second['content_hash'] == first['content_hash']
        assert not restored.search_chunks('originalmarker')
        assert restored.search_chunks('replacementmarker')
    finally:
        restored.close()
    assert kb.get_chunks(pid)[0]['text'].startswith('replacementmarker')


def test_secrets_are_excluded_and_originals_optional(library, tmp_path):
    archive, manifest = snapshot(library, tmp_path, include=False)
    with zipfile.ZipFile(archive) as z:
        assert not any(name.startswith('papers/') or 'secrets' in name for name in z.namelist())
        config = z.read('settings.json')
        assert b'FAKE_CONFIG_KEY' not in config and b'FAKE_SOURCE_KEY' not in config
        assert b'MUST_NOT_TRANSFER' not in b''.join(z.read(n) for n in z.namelist())
    assert not manifest['content']['secrets_included']


def test_restore_never_overwrites_current_or_existing_directory(library, tmp_path):
    cfg, kb, pid, *_ = library
    archive, _ = snapshot(library, tmp_path)
    for path in (cfg.data_path, cfg.data_path/'nested', tmp_path):
        with pytest.raises(backup.BackupError):
            backup.restore_backup(archive, path, current_root=cfg.data_path)
    assert kb.get_paper(pid)


def test_partial_restore_and_changed_preview_leave_original_untouched(library, tmp_path, monkeypatch):
    cfg, kb, pid, *_ = library
    archive, _ = snapshot(library, tmp_path)
    target = tmp_path/'failed'
    original = backup._unpack_verified
    def fail(archive, stage):
        original(archive, stage)
        raise OSError('simulated interrupted restore')
    monkeypatch.setattr(backup, '_unpack_verified', fail)
    with pytest.raises(OSError):
        backup.restore_backup(archive, target, current_root=cfg.data_path)
    assert not target.exists() and not list(tmp_path.glob('.litprism-restore-*'))
    assert kb.get_paper(pid)
    monkeypatch.setattr(backup, '_unpack_verified', original)
    with pytest.raises(backup.BackupError, match='预检'):
        backup.restore_backup(archive, target, expected_hash='wrong hash')
    assert not target.exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/escape', 'notes/../../escape',
                                'notes\\escape', 'notes/CON.txt', 'notes/alias.'])
def test_unsafe_archive_paths_rejected(library, tmp_path, name):
    archive, _ = snapshot(library, tmp_path)
    rewrite_zip(archive, lambda e: e.update({name: b'bad'}))
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(archive)
    assert not (tmp_path/'escape').exists()


def test_duplicate_paths_links_and_secret_files_rejected(library, tmp_path):
    archive, _ = snapshot(library, tmp_path)
    with zipfile.ZipFile(archive, 'a') as z:
        z.writestr('SETTINGS.JSON', b'case alias')
    with pytest.raises(backup.BackupError):
        backup.inspect_backup(archive)
    archive.unlink()
    snapshot(library, tmp_path)
    with zipfile.ZipFile(archive, 'a') as z:
        info = zipfile.ZipInfo('notes/link')
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        z.writestr(info, '/somewhere')
    with pytest.raises(backup.BackupError, match='链接'):
        backup.inspect_backup(archive)


@pytest.mark.parametrize('kind', ['bytes', 'manifest', 'missing', 'schema'])
def test_corrupt_backup_is_rejected_before_publishing(library, tmp_path, kind):
    archive, _ = snapshot(library, tmp_path)
    def corrupt(entries):
        if kind == 'bytes':
            entries['litprism.db'] = b'not a database'
        elif kind == 'manifest':
            entries['manifest.json'] = b'{bad json'
        elif kind == 'missing':
            entries.pop('settings.json')
        else:
            m = json.loads(entries['manifest.json'])
            m['content']['database']['schema_version'] += 1
            entries['manifest.json'] = json.dumps(m).encode()
    rewrite_zip(archive, corrupt)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(archive, tmp_path/'rejected')
    assert not (tmp_path/'rejected').exists()


def test_file_limits_and_symlinks_rejected(library, tmp_path, monkeypatch):
    archive, _ = snapshot(library, tmp_path)
    monkeypatch.setattr(backup, 'MAX_BYTES', 10)
    with pytest.raises(backup.BackupError, match='大小限制'):
        backup.inspect_backup(archive)


def test_backup_during_open_transaction_and_existing_output_rejected(library, tmp_path):
    cfg, kb, *_ = library
    with kb.tx():
        kb._con.execute("UPDATE meta SET value='changed' WHERE key='test-preference'")
        with pytest.raises(backup.BackupError, match='事务'):
            backup.create_backup(kb, cfg, tmp_path/'bad.zip')
    assert not (tmp_path/'bad.zip').exists()
    archive, _ = snapshot(library, tmp_path)
    before = archive.read_bytes()
    with pytest.raises(backup.BackupError, match='已存在'):
        backup.create_backup(kb, cfg, archive)
    assert archive.read_bytes() == before


def test_directory_migration_activation_cancel_and_failed_switch(library, tmp_path, monkeypatch):
    cfg, kb, *_ = library
    result = backup.migrate_data(kb, cfg, tmp_path/'moved')
    previous = cfg.config_path.read_bytes()
    switch = backup.activate_directory(cfg, result['data_dir'])
    assert yaml.safe_load(cfg.config_path.read_bytes())['app']['data_dir'] == result['data_dir']
    assert kb.db_path.parent == cfg.data_path
    backup.cancel_activation(switch)
    assert cfg.config_path.read_bytes() == previous
    monkeypatch.setattr(backup.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('switch failed')))
    with pytest.raises(OSError):
        backup.activate_directory(cfg, result['data_dir'])
    assert cfg.config_path.read_bytes() == previous
    assert not list(tmp_path.glob('.litprism-switch-*'))


def test_restored_credentials_never_fall_back_to_mock(library, tmp_path):
    from litprism.llm import build_llm, llm_status
    archive, _ = snapshot(library, tmp_path)
    result = backup.restore_backup(archive, tmp_path/'recovered')
    cfg = load_config(result['config_path'])
    with pytest.raises(backup.BackupError, match='重新配置'):
        build_llm(cfg)
    assert llm_status(cfg)['mode'] == 'needs_configuration'
    cfg.llm.provider = 'openai'
    cfg.llm.api_key = 'explicitly-reconfigured-fake-key'
    assert build_llm(cfg).name


def test_cli_inspect_and_restore_do_not_open_source_database(library, tmp_path, monkeypatch, capsys):
    from litprism import cli
    archive, _ = snapshot(library, tmp_path)
    monkeypatch.setattr(cli, 'Ctx', lambda *_: pytest.fail('must not open primary DB'))
    assert cli.main(['backup', 'inspect', str(archive)]) == 0
    assert cli.main(['backup', 'restore', str(archive), '--target', str(tmp_path/'cli-restored')]) == 0
    assert 'content_hash' in capsys.readouterr().out


def test_api_preview_restore_switch_and_cancel(library, tmp_path, monkeypatch):
    from litprism.web import server
    cfg, kb, *_ = library
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    app = server.create_app(cfg)
    client = TestClient(app)
    archive, _ = snapshot(library, tmp_path)
    preview = client.post('/api/backups/inspect', files={'file': ('backup.zip', archive.read_bytes(), 'application/zip')})
    assert preview.status_code == 200, preview.text
    target = tmp_path/'api-restored'
    result = client.post('/api/backups/restore', json={'token': preview.json()['token'], 'target': str(target)})
    assert result.status_code == 200, result.text
    before = cfg.config_path.read_bytes()
    active = client.post('/api/backups/activate', json={'target': str(target)})
    assert active.status_code == 200 and active.json()['requires_restart']
    assert client.post('/api/conversations', json={'title': 'blocked mutation'}).status_code == 409
    assert client.get('/api/papers').status_code == 200
    assert client.post('/api/backups/cancel-switch').status_code == 200
    assert cfg.config_path.read_bytes() == before
    assert client.post('/api/conversations', json={'title': 'allowed mutation'}).status_code == 200
    assert client.patch('/api/config', json={'app': {'data_dir': 'unsafe-hot-switch'}}).status_code == 400
    assert client.delete('/api/backups/previews/expired-token').status_code == 200


def test_live_wal_pages_are_included_without_wal_sidecar(library, tmp_path):
    cfg, kb, *_ = library
    writer = sqlite3.connect(kb.db_path)
    try:
        writer.execute("INSERT INTO meta(key,value) VALUES ('live-wal','committed external writer')")
        writer.commit()
        assert Path(str(kb.db_path)+'-wal').exists()
        archive, _ = snapshot(library, tmp_path)
        with zipfile.ZipFile(archive) as z:
            assert not any(n.endswith(('-wal', '-shm')) for n in z.namelist())
        result = backup.restore_backup(archive, tmp_path/'wal-restored')
        con = backup._read_db(Path(result['data_dir'])/'litprism.db')
        try:
            assert con.execute("SELECT value FROM meta WHERE key='live-wal'").fetchone()[0] == 'committed external writer'
        finally:
            con.close()
    finally:
        writer.close()


@pytest.mark.parametrize('kind', ['future_schema', 'missing_table', 'trigger'])
def test_rehashed_but_incompatible_database_is_rejected(library, tmp_path, kind):
    archive, _ = snapshot(library, tmp_path)
    with zipfile.ZipFile(archive) as z:
        db = tmp_path/'modified.db'
        db.write_bytes(z.read('litprism.db'))
    con = sqlite3.connect(db)
    if kind == 'future_schema':
        con.execute('PRAGMA user_version=999')
    elif kind == 'missing_table':
        con.execute('DROP TABLE manual_claims')
    else:
        con.execute("CREATE TRIGGER unsupported AFTER INSERT ON meta BEGIN DELETE FROM meta; END")
    con.commit()
    con.close()
    def update(entries):
        entries['litprism.db'] = db.read_bytes()
        manifest = json.loads(entries['manifest.json'])
        manifest['files']['litprism.db'] = {'sha256': backup._sha(db), 'size': db.stat().st_size}
        entries['manifest.json'] = json.dumps(manifest).encode()
    rewrite_zip(archive, update)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(archive, tmp_path/'must-not-exist')
    assert not (tmp_path/'must-not-exist').exists()


def test_invalid_manifest_type_and_changed_destination_rejected(library, tmp_path):
    cfg, _, *_ = library
    archive, _ = snapshot(library, tmp_path)
    result = backup.restore_backup(archive, tmp_path/'prepared')
    previous = cfg.config_path.read_bytes()
    (Path(result['data_dir'])/'notes'/'note.md').write_text('changed after recovery', encoding='utf-8')
    with pytest.raises(backup.BackupError, match='发生变化'):
        backup.activate_directory(cfg, result['data_dir'])
    assert cfg.config_path.read_bytes() == previous
    rewrite_zip(archive, lambda e: e.update({'manifest.json': b'[]'}))
    with pytest.raises(backup.BackupError, match='不支持'):
        backup.inspect_backup(archive)


def test_activation_env_override_and_external_config_changes(library, tmp_path, monkeypatch):
    cfg, kb, *_ = library
    result = backup.migrate_data(kb, cfg, tmp_path/'copy')
    monkeypatch.setenv('LITPRISM_DATA_DIR', str(cfg.data_path))
    with pytest.raises(backup.BackupError, match='环境变量'):
        backup.activate_directory(cfg, result['data_dir'])
    monkeypatch.delenv('LITPRISM_DATA_DIR')
    switch = backup.activate_directory(cfg, result['data_dir'])
    cfg.config_path.write_text('another: setting', encoding='utf-8')
    with pytest.raises(backup.BackupError, match='其他操作'):
        backup.cancel_activation(switch)
    assert cfg.config_path.read_text(encoding='utf-8') == 'another: setting'


def test_activation_refuses_active_tasks_and_restore_requires_setup(library, tmp_path, monkeypatch):
    from litprism.web import server
    cfg, kb, *_ = library
    result = backup.migrate_data(kb, cfg, tmp_path/'new-root')
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    app = server.create_app(cfg)
    client = TestClient(app)
    app.state.tasks.items['fake-job'] = {'status': 'running'}
    before = cfg.config_path.read_bytes()
    assert client.post('/api/backups/activate', json={'target': result['data_dir']}).status_code == 409
    assert cfg.config_path.read_bytes() == before
    app.state.tasks.items.clear()
    restored_cfg = load_config(result['config_path'])
    restored = KnowledgeBase(restored_cfg)
    try:
        monkeypatch.setattr(server, 'KnowledgeBase', lambda _: restored)
        recovered_app = server.create_app(restored_cfg)
        recovered_client = TestClient(recovered_app)
        pid = restored.list_papers()[0]['id']
        response = recovered_client.post('/api/analyze/'+pid)
        assert response.status_code == 400
        assert '重新配置' in response.text
    finally:
        restored.close()


def test_notes_cannot_capture_whole_data_root(library, tmp_path):
    cfg, kb, *_ = library
    cfg.export.notes_dir = str(cfg.data_path)
    with pytest.raises(backup.BackupError, match='独立子目录'):
        backup.create_backup(kb, cfg, tmp_path/'unsafe-notes.zip')
    assert not (tmp_path/'unsafe-notes.zip').exists()


def test_staging_cannot_be_recursively_copied(tmp_path):
    with pytest.raises(backup.BackupError, match='递归'):
        backup._copy_tree(tmp_path, tmp_path/'stage')
