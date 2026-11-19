"""原始附件逐版本保管（G07）：内容寻址、write-once 的原件归档。

原件按文件字节 sha256 寻址存放于 data/attachments/<前2位>/<sha256>；
attachments 表把「材料版本 ↔ 原件」关联起来，同一文件重复导入复用同一 blob。
不回填历史材料：v17 之前导入的版本如实显示"无原件记录"。
归档失败不应让导入本身失败：调用方捕获 AttachmentError 后显式回告用户。
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 与批量上传任务的单批上限一致


class AttachmentError(ValueError):
    pass


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def store_dir(cfg) -> Path:
    return Path(cfg.data_path) / 'attachments'


def blob_path(cfg, sha256: str) -> Path:
    return store_dir(cfg) / sha256[:2] / sha256


def register(kb, cfg, paper_id: str, material_id: str, src_path, *,
             original_name: str | None = None, source: str = 'upload') -> dict:
    """归档原件并登记到材料版本；返回附件信息，失败抛 AttachmentError。"""
    src = Path(src_path)
    if not src.is_file():
        raise AttachmentError('原件不存在，未归档。')
    size = src.stat().st_size
    if size <= 0:
        raise AttachmentError('原件为空文件，未归档。')
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f'原件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB 上限，未归档。')
    sha = _sha256_file(src)
    dest = blob_path(cfg, sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.stat().st_size != size:
            raise AttachmentError('附件库中同哈希文件大小不一致，疑为损坏，未归档；请检查 data/attachments。')
    else:
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix='.tmp')
        tmp = Path(tmp_name)
        try:
            with open(fd, 'wb') as out, open(src, 'rb') as inp:
                shutil.copyfileobj(inp, out)
            # write-once：发布前再核对哈希，避免并发写入或源文件变化
            if _sha256_file(tmp) != sha:
                raise AttachmentError('归档期间原件内容发生变化，未归档。')
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    row_id = 'att_' + hashlib.sha256(f'{paper_id}:{material_id}:{sha}'.encode('utf-8')).hexdigest()[:24]
    from .knowledge import now
    with kb.tx() as con:
        if not con.execute('SELECT 1 FROM materials WHERE id=? AND paper_id=?',
                           (material_id, paper_id)).fetchone():
            raise AttachmentError('材料版本不存在，未归档附件。')
        name = original_name or src.name
        con.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?,?,?,?)',
                    (row_id, paper_id, material_id, sha, size,
                     name, src.suffix.lower() or None, source, now()))
    return {'id': row_id, 'sha256': sha, 'size': size, 'original_name': name, 'source': source}


def list_for_paper(kb, pid) -> list[dict]:
    rows = kb._con.execute(
        'SELECT * FROM attachments WHERE paper_id=? ORDER BY created_at, id', (pid,)).fetchall()
    return [dict(r) for r in rows]


def get(kb, pid, att_id) -> dict | None:
    row = kb._con.execute('SELECT * FROM attachments WHERE paper_id=? AND id=?',
                          (pid, att_id)).fetchone()
    return dict(row) if row else None
