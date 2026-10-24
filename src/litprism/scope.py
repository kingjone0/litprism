"""来源范围：聊天空选择不限定；无效选择报错，批量/内核空列表仍为空。"""
from __future__ import annotations

import re


def chat_strict(scope, question):
    """Chat selection is a focus; explicit 'only' remains a hard boundary."""
    if scope.get('mode') not in (None, 'focus', 'strict'):
        raise ValueError('对话来源策略必须是重点补充或严格限定。')
    if scope.get('mode') == 'strict':
        return True
    text = re.sub(r'(?:不要|不应|不必|无需)(?:仅|只)(?:依据|根据|使用|参考|用|看)', '', question)
    # Require a source-selection instruction. "BERT 只用左侧上下文" describes
    # architecture, not authorization to suppress other evidence.
    chinese = (r'(?:^|[，,。！？；;\n]|请(?:你)?|(?:本轮|这次)(?:回答|问答)?(?:请)?)\s*'
               r'(?:仅|只)(?:依据|根据|使用|参考|用|看)\s*'
               r'(?:所选|指定|选定|已选|当前|本地|知识库|这篇|该篇|这些|这两篇|上述|提供的|论文|文献|原文|材料|[《“"])')
    english = (r'(?:^|[.!?;,\n]|\bplease\s+|\bcan you\s+)\s*only\s+'
               r'(?:use|using|based on)\s+(?:the\s+)?'
               r'(?:selected|specified|provided|current|local|these|this|above|papers?\b|sources?\b|documents?\b)')
    return bool(re.search(chinese, text) or re.search(english, text, re.I))


def chat_paper_scope(kb, scope):
    """No paper selection means unrestricted chat retrieval.

    Resolve explicit groups and nonempty selections normally: a missing/deleted
    ID must not be erased, nor may an empty selected group turn into all papers.
    Other entry points retain paper_scope's empty-list semantics.
    """
    clean = dict(scope)
    for key in ('paper_ids', 'papers', 'paper_id'):
        if clean.get(key) == []:
            clean.pop(key)
    return paper_scope(kb, clean)


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
