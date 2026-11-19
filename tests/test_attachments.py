"""G07 原始附件逐版本保管：内容寻址归档、上传捕获、备份往返与取回路由。"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from litprism.attachments import (MAX_ATTACHMENT_BYTES, AttachmentError, blob_path, get,
                                  list_for_paper, register)
from litprism.schema import SCHEMA_VERSION, apply_migrations

from test_workflow_guards import env  # noqa: F401


def _fixture_pdf_bytes() -> bytes:
    from test_pdf_reading import _table_page
    pdf, _page = _table_page()
    try:
        return pdf.tobytes()
    finally:
        pdf.close()


@pytest.fixture
def uploaded(env, monkeypatch):
    """上传一份真实可解析的 PDF；返回 (paper_id, attachment_id, 原始字节, kb, client)。"""
    from litprism.verify import PaperVerifier, VerificationResult
    cfg, kb, _, client = env
    monkeypatch.setattr('litprism.enrich.enrich_document', lambda *a: None)
    monkeypatch.setattr(PaperVerifier, 'verify', lambda *a, **kw: VerificationResult(level='verified'))
    content = _fixture_pdf_bytes()
    response = client.post('/api/upload?analyze=false',
                           files={'files': ('样本.pdf', content, 'application/pdf')})
    assert response.status_code == 200, response.text
    data = response.json()['results'][0]
    assert data['ok'] and data.get('attachment_id'), data
    return data['paper_id'], data['attachment_id'], content, kb, client


def test_register_content_addresses_and_dedupes(env, tmp_path):
    cfg, kb, _, _ = env
    pid = kb.add_paper(title='附件去重样本', verify_level='verified')
    mid = kb.add_chunks(pid, [{'text': 'alphatopic attachment dedupe', 'page': 1}])
    assert mid.startswith('mat_') and kb.get_paper(pid)['current_material_id'] == mid

    src = tmp_path / 'a.txt'
    src.write_bytes(b'attachment bytes 123')
    first = register(kb, cfg, pid, mid, src, original_name='a.txt', source='upload')
    again = register(kb, cfg, pid, mid, src, original_name='a.txt', source='upload')
    assert first['id'] == again['id'] and first['sha256'] == again['sha256']
    assert len(list(blob_path(cfg, first['sha256']).parent.iterdir())) == 1, \
        '同一文件重复归档不得复制出第二个 blob'

    # 同字节文件换到新材料版本：blob 复用，登记行为独立
    mid2 = kb.add_chunks(pid, [{'text': 'alphatopic attachment v2', 'page': 2}])
    second = register(kb, cfg, pid, mid2, src, original_name='a.txt', source='upload')
    assert second['id'] != first['id']
    assert blob_path(cfg, second['sha256']).is_file()
    assert {r['material_id'] for r in list_for_paper(kb, pid)} == {mid, mid2}


def test_register_rejects_missing_empty_oversize_and_unknown_material(env, tmp_path, monkeypatch):
    cfg, kb, _, _ = env
    pid = kb.add_paper(title='附件守卫样本', verify_level='verified')
    mid = kb.add_chunks(pid, [{'text': 'alphatopic guard', 'page': 1}])
    with pytest.raises(AttachmentError):
        register(kb, cfg, pid, mid, tmp_path / 'missing.txt')
    empty = tmp_path / 'empty.txt'
    empty.write_bytes(b'')
    with pytest.raises(AttachmentError):
        register(kb, cfg, pid, mid, empty)
    # 收紧上限以避免真实写入 100MB 文件
    monkeypatch.setattr('litprism.attachments.MAX_ATTACHMENT_BYTES', 10)
    big = tmp_path / 'big.bin'
    big.write_bytes(b'\0' * 11)
    with pytest.raises(AttachmentError):
        register(kb, cfg, pid, mid, big)
    # 合法大小文件 + 不存在的材料版本：材料守卫必须兜住
    unknown = kb.add_paper(title='没有材料的论文', verify_level='verified')
    ok_file = tmp_path / 'ok.txt'
    ok_file.write_bytes(b'ok bytes')
    with pytest.raises(AttachmentError):
        register(kb, cfg, unknown, 'mat_not_exists', ok_file)


def test_upload_restores_original_bytes_and_material_listing(env, uploaded):
    cfg, kb, _, client = env
    pid, att_id, content, _kb, _client = uploaded
    att = get(kb, pid, att_id)
    assert att and att['source'] == 'upload' and att['original_name'] == '样本.pdf'
    blob = blob_path(cfg, att['sha256'])
    assert blob.is_file() and blob.read_bytes() == content, '归档字节必须与原件一致'

    materials = client.get(f'/api/paper/{pid}/materials').json()
    assert materials['items'][0]['attachments'][0]['id'] == att_id

    response = client.get(f'/api/paper/{pid}/attachments/{att_id}')
    assert response.status_code == 200
    assert response.content == content
    from urllib.parse import unquote
    assert '样本.pdf' in unquote(response.headers.get('content-disposition', ''))
    assert client.get(f'/api/paper/{pid}/attachments/att_missing').status_code == 404
    assert client.get('/api/paper/att_wrong_paper/attachments/'+att_id).status_code == 404


def test_attachment_failure_does_not_block_import(env, uploaded, monkeypatch):
    _cfg, _kb, _model, client = env
    _pid, _att_id, _content, _kb2, _client2 = uploaded

    def broken_register(*a, **kw):
        raise AttachmentError('原件不存在，未归档。')

    from litprism.verify import PaperVerifier, VerificationResult
    monkeypatch.setattr('litprism.attachments.register', broken_register)
    monkeypatch.setattr('litprism.enrich.enrich_document', lambda *a: None)
    monkeypatch.setattr(PaperVerifier, 'verify', lambda *a, **kw: VerificationResult(level='verified'))
    response = client.post('/api/upload?analyze=false',
                           files={'files': ('样本2.pdf', _fixture_pdf_bytes(), 'application/pdf')})
    data = response.json()['results'][0]
    # 新论文导入成功，但归档失败被显式报告
    assert data['ok'] and 'attachment_error' in data and 'attachment_id' not in data


def test_backup_roundtrip_includes_attachments(tmp_path):
    from litprism import backup
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    cfg = Config()
    cfg.app.data_dir = str(tmp_path / 'source')
    kb = KnowledgeBase(cfg)
    try:
        pid = kb.add_paper(title='附件备份样本', verify_level='verified')
        mid = kb.add_chunks(pid, [{'text': 'alphatopic backup attachment', 'page': 1}])
        src = tmp_path / 'original.txt'
        src.write_bytes(b'attachment bytes for backup roundtrip')
        att = register(kb, cfg, pid, mid, src, original_name='original.txt', source='upload')
        sha = att['sha256']

        archive = tmp_path / 'with-originals.zip'
        manifest = backup.create_backup(kb, cfg, archive, include_originals=True)
        assert f'attachments/{sha[:2]}/{sha}' in manifest['files']

        target = tmp_path / 'restored'
        backup.restore_backup(archive, target, current_root=cfg.data_path)
        restored_blob = target / 'attachments' / sha[:2] / sha
        assert restored_blob.is_file() and restored_blob.read_bytes() == src.read_bytes()

        slim = tmp_path / 'without-originals.zip'
        backup.create_backup(kb, cfg, slim, include_originals=False)
        with zipfile.ZipFile(slim) as archive_zip:
            assert not any(n.startswith('attachments/') for n in archive_zip.namelist()), \
                '不含原文的备份不得携带附件库'
    finally:
        kb.close()


def test_v17_migration_is_idempotent(tmp_path):
    con = sqlite3.connect(tmp_path / 'fresh.db')
    try:
        assert len(apply_migrations(con)) == SCHEMA_VERSION
        assert con.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert len(apply_migrations(con)) == 0, '重复迁移必须幂等'
        con.execute("INSERT INTO papers (id, title) VALUES ('p1', 't')")
        con.execute("INSERT INTO materials VALUES "
                    "('mat1','p1',NULL,NULL,'hash1','2026-01-01','fulltext','current')")
        sha = '1' * 64
        con.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?)',
                    ('att1', 'p1', 'mat1', sha, 2, 'a.txt', '.txt', 'upload', '2026-01-01'))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?)',
                        ('att1', 'p1', 'mat1', sha, 2, 'a.txt', '.txt', 'upload', '2026-01-01'))
    finally:
        con.close()
