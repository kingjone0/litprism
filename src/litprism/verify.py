"""论文真实性核验 —— 防幻觉机制的核心。

设计要点
--------
1. **多源交叉验证**：同一篇论文同时在 Crossref、OpenAlex、arXiv、Semantic Scholar
   中检索，命中来源越多、标题相似度越高，可信度越高。
2. **三级结论 + 一个"无法判定"**：

   ================  ==========================================================
   verified          至少一个权威库返回的标题与查询标题相似度 ≥ 阈值（默认 0.92），
                     或 DOI 精确命中。可安全引用。
   likely            相似度落在 [0.85, 0.92)，疑似匹配，必须人工确认后才能引用。
   not_found         所有已启用的数据源都正常响应，但没有任何记录达到 likely 阈值。
                     —— 这**不等于**"这篇论文是编造的"：可能是新预印本、小众期刊、
                     非英文文献尚未收录。因此在输出中一律表述为"未能核到"，
                     并要求引用前人工复核。
   unchecked         所有数据源都请求失败（断网/限流）。**绝不能**把这种情况
                     判为 not_found，否则会把真实论文误标为虚构。
   ================  ==========================================================

3. **全量留痕**：每次核验结果都写入 verify_log，可审计、可复现。
4. **磁盘缓存**：相同查询 7 天内复用，减少对公共 API 的压力。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from .config import Config, SourceConfig
from .textutil import (
    author_overlap,
    normalize_doi,
    normalize_title,
    title_similarity,
    truncate,
)

USER_AGENT = "LitPrism/1.0 (academic paper verification; +https://github.com/)"
_CACHE_TTL = timedelta(days=7)
_LAST_CALL: dict[str, float] = {}
_MIN_INTERVAL = {"crossref": 0.6, "openalex": 0.15, "arxiv": 3.0, "semantic_scholar": 1.2}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class SourceRecord:
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    abstract: str | None = None
    citation_count: int | None = None
    similarity: float = 0.0        # 与查询标题的相似度（核验后回填）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationResult:
    query_title: str = ""
    doi: str | None = None
    exists: bool = False
    level: str = "unchecked"        # verified | likely | not_found | unchecked
    confidence: float = 0.0
    best_match: SourceRecord | None = None
    matches: list[SourceRecord] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked_at: str = ""
    elapsed_ms: int = 0
    cached: bool = False

    @property
    def verified(self) -> bool:
        return self.level == "verified"

    @property
    def message(self) -> str:
        if self.level == "verified":
            if self.best_match is None:
                return f"✅ 已核验（置信度 {self.confidence:.2f}）"
            s = self.best_match
            src = "/".join(sorted({m.source for m in self.matches})) if self.matches else s.source
            return f"✅ 已核验：在 {src} 命中「{truncate(s.title, 70)}」（相似度 {self.confidence:.2f}）"
        if self.level == "likely":
            if self.best_match is None:
                return f"⚠️ 疑似匹配（相似度 {self.confidence:.2f}），引用前请人工确认"
            s = self.best_match
            return f"⚠️ 疑似匹配：最相近的是「{truncate(s.title, 70)}」（相似度 {self.confidence:.2f}），引用前请人工确认"
        if self.level == "not_found":
            return (
                "❌ 未能核到：所有已启用的学术数据库均未返回匹配记录。"
                "这不等于该文献不存在（可能尚未收录），但在任何情况下都**不得**当作真实文献引用。"
            )
        return f"❔ 无法判定：{'; '.join(self.errors) or '数据源不可用'}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["message"] = self.message
        d["verified"] = self.verified
        return d


# ---------------------------------------------------------------------------
# HTTP 与缓存
# ---------------------------------------------------------------------------
class _Http:
    def __init__(self, cache_dir: Path | None = None, timeout: int = 20):
        self.cache_dir = cache_dir
        self.timeout = timeout
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session() if requests else None
        if self.session:
            self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def _cache_file(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def get_json(self, url: str, *, source: str, params: dict | None = None) -> Any:
        if self.session is None:
            raise RuntimeError("缺少 requests 依赖")
        key = url + json.dumps(params or {}, sort_keys=True)
        cf = self._cache_file(key)
        if cf and cf.exists():
            try:
                payload = json.loads(cf.read_text(encoding="utf-8"))
                ts = datetime.fromisoformat(payload["_ts"])
                if datetime.now(timezone.utc) - ts < _CACHE_TTL:
                    return payload["data"]
            except Exception:
                pass

        gap = _MIN_INTERVAL.get(source, 0.3)
        last = _LAST_CALL.get(source, 0.0)
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[source] = time.time()

        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if cf:
            try:
                cf.write_text(
                    json.dumps({"_ts": datetime.now(timezone.utc).isoformat(), "data": data}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return data

    def get_text(self, url: str, *, source: str, params: dict | None = None) -> str:
        if self.session is None:
            raise RuntimeError("缺少 requests 依赖")
        key = url + json.dumps(params or {}, sort_keys=True)
        cf = self._cache_file(key)
        if cf and cf.exists():
            try:
                payload = json.loads(cf.read_text(encoding="utf-8"))
                ts = datetime.fromisoformat(payload["_ts"])
                if datetime.now(timezone.utc) - ts < _CACHE_TTL:
                    return payload["data"]
            except Exception:
                pass
        gap = _MIN_INTERVAL.get(source, 0.3)
        wait = gap - (time.time() - _LAST_CALL.get(source, 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[source] = time.time()
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.text
        if cf:
            try:
                cf.write_text(
                    json.dumps({"_ts": datetime.now(timezone.utc).isoformat(), "data": text}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return text


# ---------------------------------------------------------------------------
# 各数据源适配器
# ---------------------------------------------------------------------------
def _clean_abstract(a: str | None) -> str | None:
    if not a:
        return None
    t = re.sub(r"<[^>]+>", " ", a)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


class CrossrefSource:
    name = "crossref"
    base = "https://api.crossref.org/works"

    def __init__(self, cfg: SourceConfig, http: _Http):
        self.cfg = cfg
        self.http = http

    def _params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.cfg.mailto and "@" in self.cfg.mailto:
            p["mailto"] = self.cfg.mailto
        return p

    def by_doi(self, doi: str) -> SourceRecord | None:
        data = self.http.get_json(f"{self.base}/{doi}", source=self.name, params=self._params({}))
        msg = data.get("message") if isinstance(data, dict) else None
        return self._to_record(msg) if msg else None

    def by_title(self, title: str, limit: int = 5) -> list[SourceRecord]:
        data = self.http.get_json(
            self.base,
            source=self.name,
            params=self._params(
                {"query.bibliographic": title, "rows": limit,
                 "select": "DOI,title,author,issued,container-title,URL,abstract,is-referenced-by-count,type"}
            ),
        )
        items = (data or {}).get("message", {}).get("items", []) or []
        return [r for r in (self._to_record(i) for i in items) if r]

    @staticmethod
    def _to_record(item: dict) -> SourceRecord | None:
        if not isinstance(item, dict):
            return None
        titles = item.get("title") or []
        title = titles[0] if titles else ""
        if not title:
            return None
        authors = []
        for a in item.get("author") or []:
            nm = " ".join(x for x in [a.get("given"), a.get("family")] if x).strip()
            if nm:
                authors.append(nm)
        year = None
        parts = (item.get("issued") or {}).get("date-parts") or [[None]]
        if parts and parts[0]:
            year = parts[0][0]
        ct = item.get("container-title") or []
        return SourceRecord(
            source="crossref",
            title=title,
            authors=authors,
            year=year,
            venue=ct[0] if ct else None,
            doi=normalize_doi(item.get("DOI")),
            url=item.get("URL"),
            abstract=_clean_abstract(item.get("abstract")),
            citation_count=item.get("is-referenced-by-count"),
        )


class OpenAlexSource:
    name = "openalex"
    base = "https://api.openalex.org/works"

    def __init__(self, cfg: SourceConfig, http: _Http):
        self.cfg = cfg
        self.http = http

    def _params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.cfg.mailto and "@" in self.cfg.mailto:
            p["mailto"] = self.cfg.mailto
        return p

    def by_doi(self, doi: str) -> SourceRecord | None:
        data = self.http.get_json(f"{self.base}/doi:{doi}", source=self.name, params=self._params({}))
        return self._to_record(data) if isinstance(data, dict) and data.get("title") else None

    def by_title(self, title: str, limit: int = 5) -> list[SourceRecord]:
        data = self.http.get_json(
            self.base, source=self.name, params=self._params({"search": title, "per-page": limit})
        )
        return [r for r in (self._to_record(i) for i in (data or {}).get("results", [])) if r]

    def related(self, openalex_id: str, limit: int = 10) -> list[SourceRecord]:
        wid = openalex_id.rstrip("/").split("/")[-1]
        data = self.http.get_json(f"{self.base}/{wid}/related_works", source=self.name, params=self._params({}))
        out = []
        for w in (data or {}).get("results", [])[:limit]:
            r = self._to_record(w)
            if r:
                out.append(r)
        return out

    def by_id(self, openalex_id: str) -> SourceRecord | None:
        wid = openalex_id.rstrip("/").split("/")[-1]
        data = self.http.get_json(f"{self.base}/{wid}", source=self.name, params=self._params({}))
        return self._to_record(data) if isinstance(data, dict) and data.get("title") else None

    @staticmethod
    def _to_record(item: dict) -> SourceRecord | None:
        if not isinstance(item, dict):
            return None
        title = item.get("title") or item.get("display_name") or ""
        if not title:
            return None
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (item.get("authorships") or [])
        ]
        authors = [a for a in authors if a]
        loc = (item.get("primary_location") or {}).get("source") or {}
        return SourceRecord(
            source="openalex",
            title=title,
            authors=authors,
            year=item.get("publication_year"),
            venue=loc.get("display_name") or (item.get("host_venue") or {}).get("display_name"),
            doi=normalize_doi(item.get("doi")),
            url=item.get("doi") or item.get("id"),
            abstract=None,
            citation_count=item.get("cited_by_count"),
        )


class ArxivSource:
    name = "arxiv"
    base = "https://export.arxiv.org/api/query"
    _NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

    def __init__(self, cfg: SourceConfig, http: _Http):
        self.cfg = cfg
        self.http = http

    def by_id(self, arxiv_id: str) -> SourceRecord | None:
        recs = self._query({"id_list": arxiv_id, "max_results": 1})
        return recs[0] if recs else None

    def by_title(self, title: str, limit: int = 5) -> list[SourceRecord]:
        safe = re.sub(r'["\\]', " ", title)
        return self._query({"search_query": f'ti:"{safe}"', "max_results": limit})

    def search(self, query: str, limit: int = 10) -> list[SourceRecord]:
        safe = re.sub(r'["\\]', " ", query)
        return self._query({"search_query": f"all:{safe}", "max_results": limit, "sortBy": "relevance"})

    def _query(self, params: dict) -> list[SourceRecord]:
        xml = self.http.get_text(self.base, source=self.name, params=params)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        out = []
        for e in root.findall("a:entry", self._NS):
            t = (e.findtext("a:title", "", self._NS) or "").strip()
            t = re.sub(r"\s+", " ", t)
            if not t:
                continue
            authors = [
                (a.findtext("a:name", "", self._NS) or "").strip()
                for a in e.findall("a:author", self._NS)
            ]
            published = e.findtext("a:published", "", self._NS) or ""
            year = int(published[:4]) if published[:4].isdigit() else None
            link = ""
            for lk in e.findall("a:link", self._NS):
                if lk.get("rel") == "alternate":
                    link = lk.get("href", "")
            aid = (e.findtext("a:id", "", self._NS) or "").rstrip("/").split("/")[-1]
            doi = e.findtext("arxiv:doi", None, self._NS)
            out.append(
                SourceRecord(
                    source="arxiv",
                    title=t,
                    authors=[a for a in authors if a],
                    year=year,
                    venue="arXiv",
                    doi=normalize_doi(doi),
                    arxiv_id=aid or None,
                    url=link or (f"https://arxiv.org/abs/{aid}" if aid else None),
                    abstract=re.sub(r"\s+", " ", (e.findtext("a:summary", "", self._NS) or "").strip()) or None,
                )
            )
        return out


class SemanticScholarSource:
    name = "semantic_scholar"
    base = "https://api.semanticscholar.org/graph/v1/paper"
    FIELDS = "title,authors,year,venue,externalIds,url,abstract,citationCount"

    def __init__(self, cfg: SourceConfig, http: _Http):
        self.cfg = cfg
        self.http = http
        if cfg.api_key and http.session is not None:
            http.session.headers["x-api-key"] = cfg.api_key

    def by_doi(self, doi: str) -> SourceRecord | None:
        data = self.http.get_json(f"{self.base}/DOI:{doi}", source=self.name, params={"fields": self.FIELDS})
        return self._to_record(data) if isinstance(data, dict) and data.get("title") else None

    def by_title(self, title: str, limit: int = 5) -> list[SourceRecord]:
        data = self.http.get_json(
            f"{self.base}/search",
            source=self.name,
            params={"query": title, "limit": limit, "fields": self.FIELDS},
        )
        return [r for r in (self._to_record(i) for i in (data or {}).get("data", [])) if r]

    @staticmethod
    def _to_record(item: dict) -> SourceRecord | None:
        if not isinstance(item, dict):
            return None
        title = item.get("title") or ""
        if not title:
            return None
        ext = item.get("externalIds") or {}
        return SourceRecord(
            source="semantic_scholar",
            title=title,
            authors=[(a.get("name") or "") for a in (item.get("authors") or []) if a.get("name")],
            year=item.get("year"),
            venue=item.get("venue"),
            doi=normalize_doi(ext.get("DOI")),
            arxiv_id=ext.get("ArXiv"),
            url=item.get("url"),
            abstract=item.get("abstract"),
            citation_count=item.get("citationCount"),
        )


# ---------------------------------------------------------------------------
# 核验器
# ---------------------------------------------------------------------------
class PaperVerifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = _Http(cfg.cache_path, timeout=25)
        self._sources: dict[str, Any] = {}
        if cfg.sources.crossref.enabled:
            self._sources["crossref"] = CrossrefSource(cfg.sources.crossref, self.http)
        if cfg.sources.openalex.enabled:
            self._sources["openalex"] = OpenAlexSource(cfg.sources.openalex, self.http)
        if cfg.sources.arxiv.enabled:
            self._sources["arxiv"] = ArxivSource(cfg.sources.arxiv, self.http)
        if cfg.sources.semantic_scholar.enabled:
            self._sources["semantic_scholar"] = SemanticScholarSource(cfg.sources.semantic_scholar, self.http)

    # -- 对外 --
    @property
    def openalex(self) -> OpenAlexSource | None:
        return self._sources.get("openalex")

    @property
    def arxiv(self) -> ArxivSource | None:
        return self._sources.get("arxiv")

    def enabled_sources(self) -> list[str]:
        return list(self._sources.keys())

    def verify(
        self,
        *,
        title: str = "",
        doi: str | None = None,
        authors: list[str] | None = None,
        year: int | None = None,
        arxiv_id: str | None = None,
        log: bool = False,
    ) -> VerificationResult:
        t0 = time.time()
        res = VerificationResult(
            query_title=title,
            doi=normalize_doi(doi),
            checked_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        )
        if not title and not res.doi and not arxiv_id:
            res.level = "unchecked"
            res.errors.append("缺少标题与 DOI，无法核验")
            return res

        collected: list[SourceRecord] = []
        doi_hit = False

        for name, src in self._sources.items():
            try:
                if res.doi and hasattr(src, "by_doi"):
                    rec = src.by_doi(res.doi)
                    if rec:
                        rec.similarity = 1.0 if not title else max(
                            title_similarity(title, rec.title), 0.97
                        )
                        collected.append(rec)
                        doi_hit = True
                        res.sources_ok.append(name)
                        continue
                if arxiv_id and name == "arxiv":
                    rec = src.by_id(arxiv_id)  # type: ignore[attr-defined]
                    if rec:
                        rec.similarity = max(title_similarity(title, rec.title), 0.97) if title else 1.0
                        collected.append(rec)
                        doi_hit = True
                        res.sources_ok.append(name)
                        continue
                if title:
                    cands = src.by_title(title, limit=self.cfg.verification.max_candidates)
                    for c in cands:
                        c.similarity = title_similarity(title, c.title)
                    if cands:
                        best = max(cands, key=lambda x: x.similarity)
                        collected.append(best)
                    res.sources_ok.append(name)
                else:
                    res.sources_ok.append(name)
            except Exception as e:  # noqa: BLE001
                res.sources_failed.append(name)
                res.errors.append(f"{name}: {type(e).__name__} {truncate(str(e), 120)}")

        # 去重（同 DOI 或 同归一化标题保留信息最全的一条）
        seen: dict[str, SourceRecord] = {}
        for r in collected:
            key = r.doi or normalize_title(r.title)
            if key not in seen or (r.similarity > seen[key].similarity):
                seen[key] = r
        # 相似度相同则优先引用数更高、来源更权威的记录，避免展示到同名异文
        res.matches = sorted(
            seen.values(),
            key=lambda x: (-x.similarity, -(x.citation_count or 0), x.source),
        )

        if not res.matches:
            res.level = "not_found" if res.sources_ok else "unchecked"
            res.confidence = 0.0
        else:
            best = res.matches[0]
            res.best_match = best
            sim = best.similarity
            vth = self.cfg.verification.verified_threshold
            lth = self.cfg.verification.likely_threshold
            corroborated = True
            if self.cfg.verification.require_year_match and year and best.year:
                corroborated = abs(int(year) - int(best.year)) <= 1
            if authors:
                ov = author_overlap(authors, best.authors)
                corroborated = corroborated or ov >= 0.5

            if doi_hit or (sim >= vth and corroborated):
                res.level = "verified"
                res.confidence = round(min(1.0, sim if sim < 0.999 else 1.0), 4)
                res.exists = True
            elif sim >= lth:
                res.level = "likely"
                res.confidence = round(sim, 4)
                res.exists = True
            else:
                res.level = "not_found"
                res.confidence = round(sim, 4)
                res.exists = False

        res.elapsed_ms = int((time.time() - t0) * 1000)
        if log:
            try:
                from .db import KnowledgeBase
                KnowledgeBase(self.cfg).log_verification(title or (res.doi or ""), res.to_dict())
            except Exception:
                pass
        return res

    def verify_record(self, rec: SourceRecord, log: bool = False) -> VerificationResult:
        return self.verify(
            title=rec.title, doi=rec.doi, authors=rec.authors, year=rec.year,
            arxiv_id=rec.arxiv_id, log=log,
        )

    def verify_batch(self, items: list[dict], log: bool = False,
                     progress: Callable[[int, int, str], None] | None = None) -> list[VerificationResult]:
        out = []
        for i, it in enumerate(items, 1):
            if progress:
                progress(i, len(items), it.get("title", "")[:60])
            out.append(
                self.verify(
                    title=it.get("title", ""), doi=it.get("doi"),
                    authors=it.get("authors"), year=it.get("year"),
                    arxiv_id=it.get("arxiv_id"), log=log,
                )
            )
        return out
