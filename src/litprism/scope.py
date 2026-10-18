"""显式文献范围：未知、已删除或空范围不得静默扩展为全库。"""
from __future__ import annotations


def collection_ids(kb, cid: str) -> list[str]:
    rows = {r['id']: r['parent_id'] for r in kb._con.execute('SELECT id,parent_id FROM collections WHERE deleted_at IS NULL')}
    if cid not in rows:
        raise ValueError('分组范围不存在或已删除。')
    found = {cid}
    while True:
        children = {k for k, parent in rows.items() if parent in found}
        if children <= found:
            return sorted(found)
        found |= children


def paper_scope(kb, *sources: dict) -> list[str] | None:
    scope = None
    for source in sources:
        groups = []
        for key in ('paper_ids', 'papers', 'paper_id'):
            if key not in source or source[key] is None:
                continue
            raw = source[key]
            if isinstance(raw, str):
                raw = [v.strip() for v in raw.split(',') if v.strip()]
            if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
                raise ValueError('文献范围必须是 id 列表。')
            if any(kb.get_paper(pid) is None for pid in raw):
                raise ValueError('文献范围包含不存在或已删除的文献。')
            groups.append(set(raw))
        for key in ('collection_id', 'collection'):
            if key not in source or source[key] is None:
                continue
            cid = source[key]
            ids = collection_ids(kb, cid)
            groups.append({r['id'] for r in kb._con.execute(
                f"SELECT id FROM papers WHERE collection_id IN ({','.join('?' for _ in ids)}) AND deleted_at IS NULL", ids)})
        for group in groups:
            scope = group if scope is None else scope & group
    return sorted(scope) if scope is not None else None
