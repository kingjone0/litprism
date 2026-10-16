"""题录信息补全（metadata enrichment）。

本地 PDF 的标题/作者/年份经常提取不准（首页有版权声明、arXiv 页眉、期刊模板）。
本模块用权威数据库返回的**真实题录**校正本地解析结果，并在结果中明确标注
"哪些字段来自数据库、哪些来自 PDF 解析"，避免静默篡改。

优先级：arXiv 编号 → DOI → 标题核验命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ingest import Document
from .textutil import normalize_doi, title_similarity
from .verify import PaperVerifier, SourceRecord


@dataclass
class EnrichResult:
    applied: bool = False
    source: str = ""
    changes: dict = field(default_factory=dict)     # 字段 → (旧值, 新值)
    record: dict = field(default_factory=dict)
    note: str = ""

    def summary(self) -> str:
        if not self.applied:
            return f"未补全题录（{self.note or '未找到权威记录'}）"
        parts = [f"{k}: {v[0]!r} → {v[1]!r}" for k, v in self.changes.items()]
        return f"已按 {self.source} 权威题录校正：" + "；".join(parts)


def _lookup(verifier: PaperVerifier, doc: Document) -> tuple[SourceRecord | None, str]:
    arxiv = verifier.arxiv
    if doc.arxiv_id and arxiv:
        try:
            rec = arxiv.by_id(doc.arxiv_id)
            if rec:
                return rec, "arxiv"
        except Exception:
            pass
    if doc.doi:
        for name in ("crossref", "openalex"):
            src = verifier._sources.get(name)
            if src is not None:
                try:
                    rec = src.by_doi(doc.doi)
                    if rec:
                        return rec, name
                except Exception:
                    pass
    if doc.title:
        vr = verifier.verify(title=doc.title, doi=doc.doi, authors=doc.authors, year=doc.year)
        if vr.best_match and vr.level in ("verified", "likely"):
            return vr.best_match, vr.best_match.source
    return None, ""


def enrich_document(doc: Document, verifier: PaperVerifier, *, force_title: bool = False) -> EnrichResult:
    """用权威题录校正 Document 的元数据（就地修改 doc）。"""
    res = EnrichResult()
    rec, src = _lookup(verifier, doc)
    if rec is None:
        res.note = "所有数据源均未返回可用题录"
        return res

    res.source = src
    res.record = rec.to_dict()

    # 标题：本地标题与权威标题差异过大时以权威为准（除非用户要求保留本地）
    sim = title_similarity(doc.title or "", rec.title) if doc.title else 0.0
    if rec.title and (not doc.title or sim < 0.75 or force_title):
        res.changes["title"] = (doc.title, rec.title)
        doc.title = rec.title

    if rec.authors and (not doc.authors or len(doc.authors) < len(rec.authors)):
        res.changes["authors"] = (doc.authors, rec.authors)
        doc.authors = list(rec.authors)

    if rec.year and (not doc.year or abs(int(doc.year) - int(rec.year)) > 1):
        res.changes["year"] = (doc.year, rec.year)
        doc.year = int(rec.year)

    if rec.venue and not doc.venue:
        res.changes["venue"] = (doc.venue, rec.venue)
        doc.venue = rec.venue

    if rec.doi and not doc.doi:
        res.changes["doi"] = (doc.doi, normalize_doi(rec.doi))
        doc.doi = normalize_doi(rec.doi)

    if rec.arxiv_id and not doc.arxiv_id:
        res.changes["arxiv_id"] = (doc.arxiv_id, rec.arxiv_id)
        doc.arxiv_id = rec.arxiv_id

    if rec.abstract and len(rec.abstract) > len(doc.abstract or ""):
        res.changes["abstract"] = (f"({len(doc.abstract or '')} 字符)", f"({len(rec.abstract)} 字符)")
        doc.abstract = rec.abstract

    res.applied = bool(res.changes)
    if not res.applied:
        res.note = "本地解析结果与权威题录一致，无需修改"
    doc.meta["_enriched_from"] = src
    return res
