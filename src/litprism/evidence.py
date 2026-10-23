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


def citations_for(excerpts: list[dict]) -> list[dict]:
    """把片段清单转成引用清单，按 tag 去重并保留首个。

    ``reading_batches`` 在预算不足时会把一个 chunk 拆成多段、沿用同一个 tag，
    直接逐条转换会产生重复 tag —— 而 ``stored_excerpts`` 对重复编号是直接报错的，
    等于这类论文在导出/复核时硬失败。这里按 tag 收敛成一条引用。
    """
    out, seen = [], set()
    for e in excerpts:
        tag = e.get('tag')
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cite = {'tag': tag, 'chunk_id': e['id'], 'paper_id': e['paper_id'], 'page': e['page']}
        if e.get('material_id'):
            cite['material_id'] = e['material_id']
        out.append(cite)
    return out


def stored_excerpts(kb, citations: list[dict], *, allow_session: bool = False) -> list[dict]:
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
            if allow_session and e is not None and cite.get('web_grant_id'):
                from .chat_evidence import allowed
                if allowed(kb, e, cite['web_grant_id']):
                    e['web_grant_id'] = cite['web_grant_id']
                    e['session_only'] = e['review_status'] == 'pending'
            if e is None or not (e['citable'] or e.get('web_grant_id')):
                raise ValueError('引用知识已失效或未经人工确认，禁止导出。')
        else:
            if row['deleted_at'] or not is_citable(row['verify_level']):
                raise ValueError("引用片段已失效或文献未经核验，禁止导出。")
            e = dict(row)
        if cite.get('paper_id') and cite['paper_id'] != e['paper_id']:
            raise ValueError("引用片段不属于指定论文。")
        if cite.get('material_id') and cite['material_id'] != e.get('material_id'):
            raise ValueError('引用片段不属于指定材料版本。')
        e['tag'] = cite.get('tag') or f'P{i+1}'
        if e['tag'] in tags:
            raise ValueError('引用编号重复，无法确定对应来源。')
        tags.add(e['tag'])
        result.append(e)
    return result
