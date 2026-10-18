"""所有学术产出共用的证据约束：核验、真实片段、页码与引用。"""
from __future__ import annotations

from .memory.guards import detect_memory_as_fact, validate_citations
from .textutil import parse_citations
from .whitelist import is_citable


def require_citable(level: str | None) -> None:
    if not is_citable(level):
        raise ValueError(f"拒绝导入：文献未通过真实性核验（{level or 'unchecked'}），请核验或人工确认后重试。")


def citation_issues(text: str, excerpts: list[dict], *, required: bool = True) -> list[str]:
    issues = []
    valid = {e['tag'].upper(): e for e in excerpts}
    cited = parse_citations(text)
    if required and not excerpts:
        issues.append("没有可引用的原文片段，不能生成学术结论。")
    if required and not cited:
        issues.append("回答中没有任何来源标记，无法追溯依据。")
    for tag, page in cited:
        if tag not in valid:
            issues.append(f"引用了不存在的片段 [{tag}]。")
            continue
        actual = valid[tag].get('page')
        if page is not None and (actual is None or str(page).strip() != str(actual).strip()):
            issues.append(f"[{tag}] 标注页码为 {page}，但原文页码为 {actual if actual is not None else '无页码'}。")
    for result in (detect_memory_as_fact(text), validate_citations(text)):
        issues.extend(v.message for v in result.violations)
    return list(dict.fromkeys(issues))


def stored_excerpts(kb, citations: list[dict]) -> list[dict]:
    """重新读取引用，不信任调用方、缓存或模型保存的原文与页码。"""
    result = []
    tags = set()
    for i, cite in enumerate(citations):
        if not isinstance(cite, dict):
            raise ValueError('引用清单格式错误。')
        row = kb._con.execute(
            "SELECT c.*, p.verify_level, p.title AS paper_title, p.deleted_at FROM chunks c"
            " JOIN papers p ON p.id=c.paper_id WHERE c.id=?", (cite.get('chunk_id') or cite.get('id'),),
        ).fetchone()
        if row is None:
            from .knowledge import source_chunk
            e = source_chunk(kb, cite.get('chunk_id') or cite.get('id'))
            if e is None or e['trust_level'] != 'manual_confirmed':
                raise ValueError('引用知识已失效或未经人工确认，禁止导出。')
        else:
            if row['deleted_at'] or not is_citable(row['verify_level']):
                raise ValueError("引用片段已失效或文献未经核验，禁止导出。")
            e = dict(row)
        if cite.get('paper_id') and cite['paper_id'] != e['paper_id']:
            raise ValueError("引用片段不属于指定论文。")
        e['tag'] = cite.get('tag') or f'P{i+1}'
        if e['tag'] in tags:
            raise ValueError('引用编号重复，无法确定对应来源。')
        tags.add(e['tag'])
        result.append(e)
    return result
