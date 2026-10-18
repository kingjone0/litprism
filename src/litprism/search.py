"""检索层：本地知识库检索 + 外部学术数据库检索（全部经真实性核验）。"""

from __future__ import annotations

from .config import Config
from .db import KnowledgeBase
from .query import build_or_fts_query, expand_query
from .textutil import normalize_doi, normalize_title, title_similarity
from .verify import PaperVerifier, SourceRecord, VerificationResult
from .whitelist import is_citable


# ---------------------------------------------------------------------------
# 本地检索
# ---------------------------------------------------------------------------
def search_local(
    kb: KnowledgeBase,
    query: str,
    *,
    top_k: int = 8,
    paper_ids: list[str] | None = None,
    llm=None,
    expand: bool = True,
    citable_only: bool = False,
) -> list[dict]:
    """BM25 全文检索本地知识库，返回带论文标题与页码的片段。

    默认开启跨语言查询扩展（中↔英），否则中文提问无法命中英文文献。
    """
    if paper_ids is not None and not paper_ids:
        return []
    include_knowledge = paper_ids is None
    def finish(hits):
        if include_knowledge:
            from .knowledge import search_knowledge
            hits = hits + search_knowledge(kb, query, top_k)
        return sorted([h for h in hits if h['score'] >= kb.cfg.retrieval.min_score], key=lambda h: -h['score'])[:top_k]
    if citable_only:
        allowed = [r['id'] for r in kb._con.execute(
            "SELECT id FROM papers WHERE verify_level IN ('verified','manual_confirmed') AND deleted_at IS NULL"
        ) if paper_ids is None or r['id'] in paper_ids]
        if not allowed:
            return finish([])
        paper_ids = allowed
    if not expand:
        hits = kb.search_chunks(query, paper_ids=paper_ids, top_k=top_k)
        return finish(hits)
    terms = expand_query(query, llm)
    match = build_or_fts_query(terms)
    hits = kb.search_match(match, paper_ids=paper_ids, top_k=top_k, fallback_terms=terms)
    return finish(hits)


def search_local_papers(kb: KnowledgeBase, query: str, limit: int = 10) -> list[dict]:
    """按标题/摘要/标签匹配本地论文。"""
    return kb.list_papers(query=query, limit=limit)


# ---------------------------------------------------------------------------
# 外部检索
# ---------------------------------------------------------------------------
def _dedupe(records: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for r in records:
        key = normalize_doi(r.get("doi")) or normalize_title(r.get("title", ""))
        if not key:
            continue
        if key in out:
            # 合并来源信息，保留引用数更高的
            prev = out[key]
            srcs = set(prev.get("sources", [])) | set(r.get("sources", []))
            prev["sources"] = sorted(srcs)
            if (r.get("citation_count") or 0) > (prev.get("citation_count") or 0):
                r["sources"] = prev["sources"]
                out[key] = r
        else:
            out[key] = r
    return list(out.values())


def _rec_to_dict(rec: SourceRecord, origin: str) -> dict:
    return {
        "title": rec.title,
        "authors": rec.authors,
        "year": rec.year,
        "venue": rec.venue,
        "doi": rec.doi,
        "arxiv_id": rec.arxiv_id,
        "url": rec.url,
        "abstract": rec.abstract,
        "citation_count": rec.citation_count,
        "sources": [rec.source],
        "origin": origin,
    }


def search_external(
    cfg: Config,
    verifier: PaperVerifier,
    query: str,
    *,
    limit: int = 10,
    sources: list[str] | None = None,
    cross_verify: bool = False,
) -> list[dict]:
    """在权威学术数据库检索文献。

    返回的每条记录都**直接来自**数据库响应，因此其"存在性"由数据库本身背书；
    若 ``cross_verify=True``，还会再跑一次多源交叉核验以补充证据。
    """
    sources = sources or [s for s in ("openalex", "crossref", "arxiv") if s in verifier.enabled_sources()]
    collected: list[dict] = []
    errors: list[str] = []

    for name in sources:
        try:
            if name == "openalex":
                src = verifier.openalex
                if src:
                    data = src.http.get_json(
                        src.base,
                        source="openalex",
                        params={**src._params({}), "search": query, "per-page": limit},
                    )
                    for item in (data or {}).get("results", []):
                        rec = src._to_record(item)
                        if rec:
                            d = _rec_to_dict(rec, "openalex_search")
                            d["_openalex_id"] = item.get("id")
                            collected.append(d)
            elif name == "arxiv" and verifier.arxiv:
                for rec in verifier.arxiv.search(query, limit=limit):
                    collected.append(_rec_to_dict(rec, "arxiv_search"))
            elif name == "crossref":
                from .verify import CrossrefSource
                cs = CrossrefSource(cfg.sources.crossref, verifier.http)
                for rec in cs.by_title(query, limit=limit):
                    collected.append(_rec_to_dict(rec, "crossref_search"))
            elif name == "semantic_scholar":
                from .verify import SemanticScholarSource
                ss = SemanticScholarSource(cfg.sources.semantic_scholar, verifier.http)
                for rec in ss.by_title(query, limit=limit):
                    collected.append(_rec_to_dict(rec, "s2_search"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")

    merged = _dedupe(collected)

    # 与查询的相关性排序：标题相似度 + 引用数（对数）微调
    import math
    for r in merged:
        sim = title_similarity(query, r.get("title", ""))
        cites = r.get("citation_count") or 0
        r["relevance"] = round(sim + min(0.15, math.log10(cites + 1) / 40), 4)
        r["title_similarity"] = sim
    merged.sort(key=lambda x: (-x["relevance"], -(x.get("year") or 0)))
    merged = merged[: limit * 2]

    # 存在性背书
    for r in merged:
        r["verification"] = {
            "level": "verified",
            "exists": True,
            "confidence": 1.0,
            "basis": f"记录直接来自 {', '.join(r['sources'])} 数据库检索结果",
            "sources": r["sources"],
        }

    if cross_verify:
        for r in merged[:limit]:
            vr = verifier.verify(title=r["title"], doi=r.get("doi"), authors=r.get("authors"), year=r.get("year"))
            r["verification"] = {
                "level": vr.level,
                "exists": vr.exists,
                "confidence": vr.confidence,
                "basis": vr.message,
                "sources": sorted({m.source for m in vr.matches}) or r["sources"],
                "cross_checked": True,
            }
    if errors:
        for r in merged:
            r.setdefault("warnings", []).extend(errors)
    return merged[: limit * 2]


def related_works(cfg: Config, verifier: PaperVerifier, paper: dict, limit: int = 10) -> list[dict]:
    """基于 OpenAlex 的 related_works 找出同一主题簇的文献。"""
    src = verifier.openalex
    if src is None:
        return []
    oa_id = None
    try:
        if paper.get("doi"):
            rec = src.by_doi(paper["doi"])
            if rec:
                oa_id = None  # 需要 id，by_doi 未返回；下面重新查
                data = src.http.get_json(
                    src.base, source="openalex",
                    params={**src._params({}), "filter": f"doi:{normalize_doi(paper['doi'])}", "per-page": 1},
                )
                res = (data or {}).get("results") or []
                if res:
                    oa_id = res[0].get("id")
        if not oa_id and paper.get("title"):
            data = src.http.get_json(
                src.base, source="openalex",
                params={**src._params({}), "search": paper["title"], "per-page": 1},
            )
            res = (data or {}).get("results") or []
            if res and title_similarity(paper["title"], res[0].get("title", "")) >= 0.8:
                oa_id = res[0].get("id")
        if not oa_id:
            return []
        recs = src.related(oa_id, limit=limit)
        return [_rec_to_dict(r, "openalex_related") for r in recs]
    except Exception:
        return []


def citing_works(cfg: Config, verifier: PaperVerifier, paper: dict, limit: int = 10) -> list[dict]:
    """找出引用了该论文的文献（OpenAlex cited_by）。"""
    src = verifier.openalex
    if src is None or not paper.get("title"):
        return []
    try:
        data = src.http.get_json(
            src.base, source="openalex",
            params={**src._params({}), "search": paper["title"], "per-page": 1},
        )
        res = (data or {}).get("results") or []
        if not res or title_similarity(paper["title"], res[0].get("title", "")) < 0.8:
            return []
        wid = res[0]["id"].rstrip("/").split("/")[-1]
        cited = src.http.get_json(
            src.base, source="openalex",
            params={**src._params({}), "filter": f"cites:{wid}", "per-page": limit, "sort": "cited_by_count:desc"},
        )
        out = []
        for item in (cited or {}).get("results", []):
            rec = src._to_record(item)
            if rec:
                out.append(_rec_to_dict(rec, "openalex_citing"))
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 导入本地知识库
# ---------------------------------------------------------------------------
def import_record(
    kb: KnowledgeBase,
    record: dict,
    *,
    verification: VerificationResult | None = None,
    tags: list[str] | None = None,
    fetch_abstract: bool = True,
) -> tuple[str, bool]:
    """把一条（已核验的）外部文献记录导入本地知识库。

    返回 ``(paper_id, created)``。若记录未通过核验则拒绝导入。

    五态里只有 ``verified`` 与 ``manual_confirmed`` 可以进库 ——
    后者是人工确认放行，属于记录在案的例外，``verify_evidence`` 会保留
    确认当时的自动判定，不把人的判断伪装成机器的结论。
    """
    level = (record.get("verification") or {}).get("level")
    if verification is not None:
        level = verification.level
    if not is_citable(level):
        raise ValueError(
            f"拒绝导入：该文献未通过真实性核验（level={level or 'unknown'}）。"
            "本地知识库只接受已在权威数据库命中的文献，或经人工确认放行的文献，"
            "以避免污染检索结果。"
        )

    vr_dict = verification.to_dict() if verification else record.get("verification", {})
    pid = kb.add_paper(
        title=record.get("title", ""),
        authors=record.get("authors") or [],
        year=record.get("year"),
        venue=record.get("venue"),
        abstract=record.get("abstract"),
        doi=normalize_doi(record.get("doi")),
        arxiv_id=record.get("arxiv_id"),
        url=record.get("url"),
        source=record.get("origin") or record.get("source") or "external",
        origin=record.get("url") or record.get("doi"),
        tags=tags or ["外部导入"],
        # verify_status 与 verify_level 保持同一取值，避免两套状态互相打架
        verify_status=level or "verified",
        verify_level=level or "verified",
        verify_confidence=float(verification.confidence if verification else 1.0),
        verify_evidence=vr_dict,
    )
    existing = kb.get_paper(pid)
    created = bool(existing and existing.get("created_at"))
    # 摘要可检索化：把标题+摘要作为一个块写入，保证外部文献也能被问答检索到
    if fetch_abstract and (record.get("abstract") or record.get("title")):
        body = f"{record.get('title','')}\n\n{record.get('abstract') or ''}".strip()
        kb.add_chunks(pid, [{"text": body, "page": None, "section": "题录与摘要"}])
    return pid, created
