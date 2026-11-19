"""阅读标注：原文摘录 + 批注，绑定论文与材料版本。

边界：
- 只存用户自己选的原文片段与批注，不生成、不改写原文；
- 软删除保留痕迹（deleted_at），列表默认不返回；
- 摘录长度设上限，避免把整篇 PDF 塞进一行标注。
"""
from __future__ import annotations

import uuid

from .db import KnowledgeBase

MAX_QUOTE = 4000
MAX_NOTE = 8000


def _now() -> str:
    from .knowledge import now

    return now()


def _clean(value: str | None, limit: int) -> str:
    text = (value or '').strip()
    if len(text) > limit:
        text = text[:limit]
    return text


def add(kb: KnowledgeBase, paper_id: str, *, quote: str, note: str = '',
        chunk_id: str | None = None, page: int | None = None,
        material_id: str | None = None) -> dict:
    paper = kb.get_paper(paper_id)
    if not paper:
        raise ValueError('论文不存在，无法添加标注。')
    text = _clean(quote, MAX_QUOTE)
    if not text:
        raise ValueError('标注内容不能为空。')
    aid = 'an_' + uuid.uuid4().hex[:16]
    stamp = _now()
    with kb.tx() as con:
        con.execute('INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (aid, paper['id'], material_id or paper.get('current_material_id'),
                     chunk_id, page, text, _clean(note, MAX_NOTE), stamp, stamp, None))
    return get(kb, aid)


def get(kb: KnowledgeBase, annotation_id: str) -> dict:
    row = kb._con.execute('SELECT * FROM annotations WHERE id=? AND deleted_at IS NULL',
                          (annotation_id,)).fetchone()
    if row is None:
        raise ValueError('标注不存在或已删除。')
    return dict(row)


def list_for(kb: KnowledgeBase, paper_id: str, *, include_deleted: bool = False) -> list[dict]:
    sql = 'SELECT * FROM annotations WHERE paper_id=?'
    if not include_deleted:
        sql += ' AND deleted_at IS NULL'
    sql += ' ORDER BY created_at, rowid'
    return [dict(r) for r in kb._con.execute(sql, (paper_id,))]


def update(kb: KnowledgeBase, annotation_id: str, *, note: str) -> dict:
    get(kb, annotation_id)  # 存在性校验
    with kb.tx() as con:
        con.execute('UPDATE annotations SET note=?, updated_at=? WHERE id=? AND deleted_at IS NULL',
                    (_clean(note, MAX_NOTE), _now(), annotation_id))
    return get(kb, annotation_id)


def delete(kb: KnowledgeBase, annotation_id: str) -> None:
    get(kb, annotation_id)
    with kb.tx() as con:
        con.execute('UPDATE annotations SET deleted_at=?, updated_at=? WHERE id=?',
                    (_now(), _now(), annotation_id))


def count(kb: KnowledgeBase, paper_id: str) -> int:
    row = kb._con.execute('SELECT COUNT(*) AS n FROM annotations WHERE paper_id=? AND deleted_at IS NULL',
                          (paper_id,)).fetchone()
    return int(row['n'] or 0)


def to_markdown(kb: KnowledgeBase, paper_id: str) -> str:
    """导出为 Markdown：引用在前、批注在后，便于贴进笔记。"""
    paper = kb.get_paper(paper_id) or {}
    items = list_for(kb, paper_id)
    title = paper.get('display_title') or paper.get('title') or paper_id
    lines = [f'# {title}', '', f'- 标注数：{len(items)}']
    doi = paper.get('doi')
    if doi:
        lines.append(f'- DOI：{doi}')
    lines.append('')
    for index, a in enumerate(items, 1):
        where = f'第 {a["page"]} 页' if a.get('page') else '无页码'
        lines.append(f'## {index}. {where}')
        for line in (a.get('quote') or '').splitlines() or ['']:
            lines.append(f'> {line}')
        if a.get('note'):
            lines.extend(['', a['note']])
        lines.extend(['', f'<sub>{a.get("created_at") or ""}</sub>', ''])
    return '\n'.join(lines).rstrip() + '\n'


def to_csv(kb: KnowledgeBase, paper_id: str) -> str:
    """导出为 CSV（UTF-8 BOM 由调用方决定，这里只给内容）。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['page', 'chunk_id', 'quote', 'note', 'created_at'])
    for a in list_for(kb, paper_id):
        writer.writerow([a.get('page') or '', a.get('chunk_id') or '',
                         a.get('quote') or '', a.get('note') or '', a.get('created_at') or ''])
    return buffer.getvalue()


def list_all(kb: KnowledgeBase, *, paper_id: str | None = None, limit: int = 200) -> list[dict]:
    """跨论文的标注列表：带论文标题，按创建时间倒序。"""
    sql = ('SELECT a.*, p.title AS paper_title, p.display_title AS paper_display_title '
           'FROM annotations a LEFT JOIN papers p ON p.id = a.paper_id '
           'WHERE a.deleted_at IS NULL')
    params: list = []
    if paper_id:
        sql += ' AND a.paper_id = ?'
        params.append(paper_id)
    sql += ' ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?'
    params.append(max(1, min(int(limit), 1000)))
    return [dict(r) for r in kb._con.execute(sql, params)]
