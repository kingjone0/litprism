"""FastAPI Web 界面：把 CLI 的能力搬到浏览器里，便于交互式探索。

启动：``python -m litprism serve``  默认 http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..agents import CompareAgent, QAAgent, RecommendAgent, render_comparison, render_recommendation
from ..config import Config, config_to_dict
from ..db import KnowledgeBase
from ..extract import analyze_paper, format_analysis
from ..ingest import SUPPORTED_EXT, chunk_document, load_document
from ..llm import build_llm, llm_status
from ..relations import cluster, compute_relations, field_map
from ..search import import_record, search_external, search_local
from ..textutil import truncate
from ..verify import PaperVerifier

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AskReq(BaseModel):
    question: str
    paper_ids: list[str] | None = None
    top_k: int | None = None


class CompareReq(BaseModel):
    refs: list[str]
    aspects: list[str] | None = None


class RecommendReq(BaseModel):
    topic: str | None = None
    paper: str | None = None
    limit: int | None = None
    import_to_kb: bool = False


class VerifyReq(BaseModel):
    title: str | None = None
    doi: str | None = None


class ImportReq(BaseModel):
    title: str | None = None
    doi: str | None = None
    tags: list[str] | None = None


class SearchReq(BaseModel):
    query: str
    external: bool = False
    limit: int = 8


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="文献棱镜 LitPrism", version="1.0.0")
    kb = KnowledgeBase(cfg)
    llm = build_llm(cfg)
    verifier = PaperVerifier(cfg)

    # ---------------------------------------------------------------- 页面
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    # ---------------------------------------------------------------- 状态
    @app.get("/api/status")
    def status():
        return {
            "app": cfg.app.name,
            "stats": kb.stats(),
            "llm": llm_status(cfg),
            "sources": verifier.enabled_sources(),
            "config": config_to_dict(cfg),
        }

    @app.get("/api/papers")
    def papers(status_filter: str | None = None, q: str | None = None, limit: int = 300):
        items = kb.list_papers(status=status_filter, query=q, limit=limit)
        for p in items:
            p.pop("full_text", None)
        return {"items": items, "count": len(items)}

    @app.get("/api/paper/{paper_id}")
    def paper(paper_id: str):
        p = kb.resolve_paper(paper_id)
        if not p:
            raise HTTPException(404, "未找到论文")
        analysis = kb.get_analysis(p["id"])
        payload = dict((analysis or {}).get("payload") or {})
        payload["concepts"] = kb.list_concepts(p["id"])
        p.pop("full_text", None)
        return {
            "paper": p,
            "analysis": payload,
            "markdown": format_analysis(p, payload) if payload else "",
            "chunks": len(kb.get_chunks(p["id"])),
        }

    @app.post("/api/analyze/{paper_id}")
    def analyze(paper_id: str, force: bool = False):
        p = kb.resolve_paper(paper_id)
        if not p:
            raise HTTPException(404, "未找到论文")
        try:
            data = analyze_paper(kb, cfg, llm, p["id"], force=force)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e
        return {"analysis": data, "markdown": format_analysis(p, dict(data))}

    @app.delete("/api/paper/{paper_id}")
    def delete_paper(paper_id: str):
        p = kb.resolve_paper(paper_id)
        if not p:
            raise HTTPException(404, "未找到论文")
        return {"deleted": kb.delete_paper(p["id"])}

    # ---------------------------------------------------------------- 导入
    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...), analyze: bool = True, verify: bool = True):
        results = []
        for f in files:
            suffix = Path(f.filename or "").suffix.lower()
            if suffix not in SUPPORTED_EXT:
                results.append({"file": f.filename, "ok": False, "error": f"不支持的格式 {suffix}"})
                continue
            tmp = Path(tempfile.gettempdir()) / f"litprism_{abs(hash(f.filename))}{suffix}"
            tmp.write_bytes(await f.read())
            try:
                doc = load_document(tmp)
                try:
                    from ..enrich import enrich_document
                    enrich_document(doc, verifier)
                except Exception:
                    pass
                v_level, v_conf, v_status, v_ev = "unknown", 0.0, "unverified", {}
                if verify:
                    vr = verifier.verify(
                        title=doc.title, doi=doc.doi, authors=doc.authors,
                        year=doc.year, arxiv_id=doc.arxiv_id, log=True,
                    )
                    v_level, v_conf = vr.level, vr.confidence
                    v_status = "verified" if vr.level == "verified" else "unverified"
                    v_ev = vr.to_dict()
                pid = kb.add_paper(
                    title=doc.title, authors=doc.authors, year=doc.year, abstract=doc.abstract,
                    doi=doc.doi, arxiv_id=doc.arxiv_id, source="local_pdf", origin=f.filename,
                    full_text=doc.full_text, tags=["本地上传"],
                    verify_status=v_status, verify_level=v_level,
                    verify_confidence=v_conf, verify_evidence=v_ev,
                )
                chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
                kb.add_chunks(pid, chunks)
                entry = {
                    "file": f.filename, "ok": True, "paper_id": pid,
                    "title": doc.title, "pages": doc.page_count, "chunks": len(chunks),
                    "verify_level": v_level, "verify_confidence": v_conf,
                    "warnings": doc.warnings,
                }
                if analyze:
                    try:
                        analyze_paper(kb, cfg, llm, pid, force=True)
                        entry["analyzed"] = True
                    except Exception as e:  # noqa: BLE001
                        entry["analyzed"] = False
                        entry["analyze_error"] = str(e)
                results.append(entry)
            except Exception as e:  # noqa: BLE001
                results.append({"file": f.filename, "ok": False, "error": str(e)})
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass
        return {"results": results}

    # ---------------------------------------------------------------- 问答
    @app.post("/api/ask")
    def ask(req: AskReq):
        agent = QAAgent(kb, cfg, llm)
        ans = agent.ask(req.question, paper_ids=req.paper_ids, top_k=req.top_k)
        return ans.to_dict()

    # ---------------------------------------------------------------- 检索
    @app.post("/api/search")
    def search(req: SearchReq):
        local = search_local(kb, req.query, top_k=req.limit, llm=llm)
        out = {"local": local, "external": []}
        if req.external:
            recs = search_external(cfg, verifier, req.query, limit=req.limit)
            out["external"] = recs
        return out

    # ---------------------------------------------------------------- 对比
    @app.post("/api/compare")
    def compare(req: CompareReq):
        agent = CompareAgent(kb, cfg, llm)
        try:
            data = agent.compare(req.refs, aspects=req.aspects)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        return {"data": data, "markdown": render_comparison(data)}

    # ---------------------------------------------------------------- 推荐
    @app.post("/api/recommend")
    def recommend(req: RecommendReq):
        agent = RecommendAgent(kb, cfg, llm, verifier)
        try:
            data = agent.recommend(
                topic=req.topic, paper_ref=req.paper, limit=req.limit, import_to_kb=req.import_to_kb
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        return {"data": data, "markdown": render_recommendation(data)}

    # ---------------------------------------------------------------- 核验
    @app.post("/api/verify")
    def verify(req: VerifyReq):
        vr = verifier.verify(title=req.title or "", doi=req.doi, log=True)
        return vr.to_dict()

    @app.post("/api/import")
    def import_paper(req: ImportReq):
        vr = verifier.verify(title=req.title or "", doi=req.doi, log=True)
        if not vr.exists or not vr.best_match:
            raise HTTPException(409, f"未通过核验，拒绝导入。{vr.message}")
        b = vr.best_match
        pid, created = import_record(
            kb,
            {
                "title": b.title, "authors": b.authors, "year": b.year, "venue": b.venue,
                "doi": b.doi, "url": b.url, "abstract": b.abstract, "origin": "web_import",
            },
            verification=vr,
            tags=req.tags or ["核验导入"],
        )
        return {"paper_id": pid, "created": created, "title": b.title, "verification": vr.to_dict()}

    # ---------------------------------------------------------------- 关联
    @app.get("/api/relations")
    def relations(threshold: float = 0.18):
        rels = compute_relations(kb, threshold=threshold)
        groups = cluster(kb, threshold=max(threshold, 0.2))
        return {
            "relations": rels,
            "clusters": [[kb.get_paper(x)["title"] if kb.get_paper(x) else x for x in g] for g in groups if len(g) > 1],
        }

    @app.get("/api/graph")
    def graph():
        return field_map(kb)

    @app.get("/api/concepts")
    def concepts(paper: str | None = None, term: str | None = None, limit: int = 300):
        pid = None
        if paper:
            p = kb.resolve_paper(paper)
            pid = p["id"] if p else None
        return {"items": kb.list_concepts(paper_id=pid, term=term, limit=limit)}

    @app.exception_handler(Exception)
    async def unhandled(_request, exc):  # pragma: no cover
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


def run_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")
