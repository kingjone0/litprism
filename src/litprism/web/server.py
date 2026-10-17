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
from ..config import Config, config_to_dict, load_config
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
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="文献棱镜 LitPrism", version="1.0.0")
    kb = KnowledgeBase(cfg)
    llm = build_llm(cfg)
    verifier = PaperVerifier(cfg)

    # 静态资源（app.css / app.js）
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

    @app.patch("/api/config")
    def patch_config_api(req: dict):
        """局部更新配置。**校验通过才落盘**，非法配置一律拒绝。

        注意：数据目录、分块参数等需要重建索引的项，改动后要重启应用才完全生效。
        """
        from ..config import patch_config as _patch

        if not cfg.config_path:
            raise HTTPException(400, "当前使用内置默认配置，没有可写入的配置文件。")
        try:
            _patch(cfg.config_path, req)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # 重新载入，让本次会话立即生效
        try:
            fresh = load_config(cfg.config_path)
            cfg.__dict__.update(fresh.__dict__)
        except Exception:  # noqa: BLE001
            pass

        warnings = list(getattr(cfg, "config_warnings", []) or [])
        return {"ok": True, "config": config_to_dict(cfg), "warnings": warnings}

    @app.get("/api/papers")
    def papers(status_filter: str | None = None, q: str | None = None, limit: int = 300):
        items = kb.list_papers(status=status_filter, query=q, limit=limit)
        for p in items:
            p.pop("full_text", None)
        return {"items": items, "count": len(items)}

    @app.patch("/api/paper/{paper_id}")
    def rename_paper(paper_id: str, req: dict):
        """改显示名。只动 ``display_title``，原始题录标题保持不变。"""
        if not kb.rename_paper(paper_id, req.get("display_title") or ""):
            raise HTTPException(404, "未找到论文")
        return {"ok": True}

    @app.delete("/api/paper/{paper_id}")
    def delete_paper(paper_id: str):
        if not kb.delete_paper(paper_id):
            raise HTTPException(404, "未找到论文")
        return {"ok": True}

    @app.get("/api/paper/{paper_id}/text")
    def paper_text(paper_id: str):
        """原文分块（按页分组）。供论文详情的「原文」标签使用。"""
        p = kb.resolve_paper(paper_id)
        if not p:
            raise HTTPException(404, "未找到论文")
        chunks = kb.get_chunks(p["id"], limit=2000)
        pages: dict[int | None, list[dict]] = {}
        for c in chunks:
            pages.setdefault(c.get("page"), []).append(
                {"section": c.get("section"), "text": c.get("text"), "chunk_id": c.get("id")}
            )
        ordered = [
            {"page": pg, "chunks": items}
            for pg, items in sorted(pages.items(), key=lambda x: (x[0] is None, x[0] or 0))
        ]
        return {"total": len(chunks), "pages": ordered}

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

    # ---------------------------------------------------------------- 会话
    @app.get("/api/conversations")
    def conversations(limit: int = 100):
        return {"items": kb.list_conversations(limit=limit)}

    @app.post("/api/conversations")
    def create_conversation(req: dict):
        cid = kb.add_conversation(req.get("title") or "新会话", scope=req.get("scope"))
        return {"id": cid}

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str):
        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        return {"ok": kb.delete_conversation(conv["id"])}

    @app.get("/api/conversations/{conv_id}/messages")
    def messages(conv_id: str):
        from ..genui import process as genui_process

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        items = kb.get_messages(conv["id"])
        # 每条消息附带 segments（组件已由框架注入核验状态与页码）
        for m in items:
            m["segments"] = genui_process(m.get("content") or "", kb).segments
        return {"conversation": conv, "items": items}

    @app.post("/api/chat/{conv_id}")
    def chat(conv_id: str, req: dict):
        """问答：写入用户消息 → 调模型 → 写入回答（含引用校验）→ 返回 segments。

        若模型的回答里没有 ``lit-ui`` 围栏，**框架会自动附加一个 citation-list** ——
        引用片段的页码与原文本来就来自数据库，不该指望模型自己列。
        这样用户看到的来源永远是权威值，而且「不列来源」这件事在结构上不可能发生。
        """
        from ..genui import parse_fences
        from ..genui import process as genui_process

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        question = (req.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "问题不能为空")

        user_msg = kb.append_message(conv["id"], "user", question)

        # 优先用「设置里选中的模型」；没配就退回配置文件的默认
        chat_llm = llm
        try:
            from ..llm import build_llm_for
            from ..providers import ProviderStore

            store = ProviderStore(kb, cfg.data_path)
            active, model_id = store.get_active()
            if active and model_id:
                key = store.secrets.get(active.api_key_ref)
                if key or active.base_url.startswith("http://localhost"):
                    chat_llm = build_llm_for(
                        cfg, base_url=active.base_url, api_key=key or "",
                        model=model_id, protocol=active.protocol,
                    )
        except Exception:  # noqa: BLE001 - 提供商配置有问题时退回默认模型
            chat_llm = llm

        try:
            ans = QAAgent(kb, cfg, chat_llm).ask(question, top_k=int(req.get("top_k") or 8))
        except Exception as e:  # noqa: BLE001
            kb.append_message(
                conv["id"], "assistant",
                f"回答失败：{type(e).__name__}: {e}",
                meta={"error": True},
            )
            raise HTTPException(500, f"回答失败：{e}") from e

        excerpts = getattr(ans, "excerpts", []) or []
        cites = [
            {
                "tag": e.get("tag"), "chunk_id": e.get("chunk_id"),
                "paper_id": e.get("paper_id"), "page": e.get("page"),
                "section": e.get("section"),
            }
            for e in excerpts
        ]
        check_dict = {
            "passed": bool(getattr(ans, "citation_check_passed", True)),
            "issues": list(getattr(ans, "citation_issues", []) or []),
        }

        content = ans.answer
        # 模型没输出结构化围栏时，框架补一个引用片段组件
        if excerpts and not parse_fences(content):
            chunk_ids = [e["chunk_id"] for e in excerpts if e.get("chunk_id")]
            if chunk_ids:
                content += (
                    "\n\n```lit-ui\n"
                    + json.dumps(
                        {"v": 1, "type": "citation-list", "chunk_ids": chunk_ids},
                        ensure_ascii=False,
                    )
                    + "\n```"
                )

        asst = kb.append_message(
            conv["id"], "assistant", content,
            citations=cites, citation_check=check_dict,
        )

        proc = genui_process(content, kb)

        # ---- 自动压缩：策略判定达到阈值就折，不让用户操心 ----
        # 手动 /compact 仍保留在 CLI 作为逃生门，但界面不暴露主动入口 ——
        # 用户不应该需要理解「上下文占用」才能正常使用。
        auto_compact = None
        try:
            from ..context.policy import Policy
            from ..context.seam import Seam
            from ..context.summarizer import ExtractiveSummarizer, Summarizer
            from ..context.telemetry import Telemetry

            meter = Telemetry(kb, cfg).meter
            policy = Policy(cfg)
            fresh_conv = kb.resolve_conversation(conv["id"])
            msgs = kb.get_messages(conv["id"])
            meas = meter.measure(kb, conv["id"])
            zones = policy.protected_zones(meas, msgs)
            decision = policy.decide_trigger(
                meas,
                anchor_seq=int((fresh_conv or {}).get("compact_anchor_seq") or 0),
                messages=msgs,
                protected=zones,
            )
            if decision.should_compact:
                sel = policy.select(meas, messages=msgs, aggressive=decision.emergency_truncate)
                if sel.ok:
                    seam = Seam(kb, cfg)
                    tx_id, _ = seam.begin(conv["id"], decision.trigger, sel)
                    if tx_id:
                        in_range = [m for m in msgs if sel.start_seq <= m["seq"] <= sel.end_seq]
                        summary = Summarizer(
                            cfg, chat_llm, backend=ExtractiveSummarizer()
                        ).summarize(in_range, sel.shadowed_tokens)
                        if summary.ok:
                            res = seam.commit(tx_id, summary)
                            if res.ok:
                                auto_compact = {
                                    "trigger": decision.trigger,
                                    "range": sel.range_text,
                                    "shadowed_tokens": sel.shadowed_tokens,
                                    "saved_per_turn": (res.ledger or {}).get("saved_per_turn", 0),
                                    "block_id": res.block_id,
                                }
                        else:
                            seam.fail(tx_id, "summary", summary.error)
        except Exception:  # noqa: BLE001 - 自动压缩失败绝不能影响回答
            auto_compact = None

        return {
            "user": user_msg,
            "assistant": {**asst, "segments": proc.segments, "citation_check": check_dict},
            "confidence": getattr(ans, "confidence", None),
            "insufficient_evidence": bool(getattr(ans, "insufficient_evidence", False)),
            "issues": proc.issues,
            "auto_compact": auto_compact,
        }

    # ---------------------------------------------------------------- 上下文
    @app.get("/api/context/{conv_id}")
    def context(conv_id: str):
        from ..context.telemetry import Telemetry

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        snap = Telemetry(kb, cfg).snapshot(conv["id"])
        snap["conversation"] = {"id": conv["id"], "title": conv["title"]}
        return snap

    @app.post("/api/compact/{conv_id}")
    def compact(conv_id: str, req: dict | None = None):
        from ..context.policy import Policy
        from ..context.seam import Seam
        from ..context.summarizer import ExtractiveSummarizer, Summarizer
        from ..context.telemetry import Telemetry

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        dry = bool((req or {}).get("dry_run"))

        meter = Telemetry(kb, cfg).meter
        policy = Policy(cfg)
        msgs = kb.get_messages(conv["id"])
        meas = meter.measure(kb, conv["id"])
        zones = policy.protected_zones(meas, msgs)
        decision = policy.decide_trigger(
            meas,
            anchor_seq=int(conv.get("compact_anchor_seq") or 0),
            messages=msgs,
            protected=zones,
            force="manual" if dry else None,
        )
        if not decision.should_compact:
            return {"ok": False, "stage": "trigger", "reason": decision.reason}
        sel = policy.select(meas, messages=msgs, aggressive=decision.emergency_truncate)
        if not sel.ok:
            return {"ok": False, "stage": "select", "reason": sel.noop_reason}
        if dry:
            return {
                "ok": True, "dry_run": True,
                "trigger": decision.trigger,
                "range": sel.range_text,
                "shadowed_refs": sel.shadowed_refs,
                "shadowed_tokens": sel.shadowed_tokens,
                "retained_tokens": sel.retained_tokens,
            }

        seam = Seam(kb, cfg)
        tx_id, code = seam.begin(conv["id"], decision.trigger or "manual", sel)
        if not tx_id:
            return {"ok": False, "stage": "begin", "failure_code": code}
        in_range = [m for m in msgs if sel.start_seq <= m["seq"] <= sel.end_seq]
        summary = Summarizer(cfg, llm, backend=ExtractiveSummarizer()).summarize(
            in_range, sel.shadowed_tokens
        )
        if not summary.ok:
            seam.fail(tx_id, "summary", summary.error)
            return {"ok": False, "stage": "summary", "reason": summary.error}
        result = seam.commit(tx_id, summary)
        return {
            "ok": result.ok,
            "block_id": result.block_id,
            "replacement_ref": result.replacement_ref,
            "failure_code": result.failure_code,
            "ledger": result.ledger,
        }

    @app.get("/api/blocks/{conv_id}")
    def blocks(conv_id: str):
        from ..context.restore import Restorer

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        return Restorer(kb, cfg).stats(conv["id"])

    # ---------------------------------------------------------------- 记忆
    @app.get("/api/memories")
    def memories(kind: str | None = None):
        from ..memory.store import MemoryStore

        store = MemoryStore(kb, cfg)
        return {"items": [m.to_dict() for m in store.list(kind=kind)], "stats": store.stats()}

    @app.post("/api/memories")
    def add_memory(req: dict):
        from ..memory.store import MemoryStore

        store = MemoryStore(kb, cfg)
        receipt = store.save(
            req.get("value") or "",
            kind=req.get("kind") or "misc",
            key=req.get("key"),
            pinned=bool(req.get("pinned", True)),
            priority=int(req.get("priority", 100)),
        )
        return {"action": receipt.action, "id": receipt.memory_id, "reason": receipt.reason}

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: str):
        from ..memory.store import MemoryStore

        return {"ok": MemoryStore(kb, cfg).delete(memory_id)}

    @app.get("/api/turns/{conv_id}")
    def turns(conv_id: str, limit: int = 20):
        from ..memory.view import TurnViewStore

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        vs = TurnViewStore(kb)
        return {"items": [v.to_dict() for v in vs.list(conv["id"], limit=limit)], "stats": vs.stats(conv["id"])}

    # ---------------------------------------------------------------- Skill
    @app.get("/api/skills")
    def skills():
        from ..skills.registry import SkillRegistry

        reg = SkillRegistry(cfg)
        entries = reg.discover(force=True)
        return {
            "items": [e.to_dict() for e in entries],
            "stats": reg.stats(),
            "dir": str(reg.skills_dir),
            "empty_text": reg.empty_state_text(),
        }

    @app.post("/api/skills/scaffold")
    def scaffold_skill(req: dict):
        """按模板新建一个 Skill 目录。只生成声明式文件，不含任何可执行代码。"""
        import re as _re

        from ..skills.manifest import SCHEMA_VERSION
        from ..skills.registry import SkillRegistry

        sid = (req.get("id") or "").strip()
        if not _re.match(r"^[a-z0-9][a-z0-9-]{1,48}$", sid):
            raise HTTPException(
                400, "id 不合法：需以小写字母或数字开头，只含小写字母/数字/连字符，长度 2–49。"
            )

        reg = SkillRegistry(cfg)
        target = reg.ensure_dir() / sid
        if target.exists():
            raise HTTPException(400, f"Skill {sid} 已存在。")

        name = (req.get("name") or sid).strip()
        desc = (req.get("description") or "（待填写用途）").strip()

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "id": sid,
            "name": name,
            "version": "0.1.0",
            "description": desc,
            "author": "",
            "trigger": {"prefix": f"/{sid}", "keywords": [name]},
            "inputs": [
                {"key": "topic", "label": "主题", "type": "text", "required": True,
                 "placeholder": "例如：注意力机制"},
            ],
            "context": [
                {"capability": "search_passages", "query": "{{topic}}", "top_k": 6},
            ],
            "capabilities": ["search_passages"],
            "output": {"type": "answer", "citation_required": True},
        }
        prompt = (
            f"围绕「{{{{topic}}}}」整理要点。\n\n"
            "要求：\n"
            "1. 只依据给定的原文片段作答，片段里没有的信息不要写入。\n"
            "2. 每条结论后标注来源，格式为 [P<编号> p.<页码>]。\n"
            "3. 区分原文事实与你的推断，推断需显式标注。\n"
        )

        import yaml as _yaml

        target.mkdir(parents=True, exist_ok=False)
        (target / "skill.yaml").write_text(
            _yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (target / "prompt.md").write_text(prompt, encoding="utf-8")
        (target / "README.md").write_text(
            f"# {name}\n\n{desc}\n\n"
            "Skill 是**声明式**的提示词扩展，不含可执行代码。\n\n"
            "- `skill.yaml` 清单：元数据 / 输入契约 / 上下文声明 / 能力白名单 / 输出形态\n"
            "- `prompt.md` 提示词模板，可用 `{{变量}}` 引用 inputs 中声明的输入\n\n"
            "可用能力（固定，不可扩展）：search_passages / search_papers / get_analysis /\n"
            "list_collections / get_graph / verify_paper / compare_papers\n",
            encoding="utf-8",
        )

        entries = reg.reload()
        entry = reg.get(sid)
        return {
            "ok": True,
            "path": str(target),
            "skill": entry.to_dict() if entry else None,
            "stats": reg.stats(),
        }

    # ---------------------------------------------------------------- 交付物
    @app.get("/api/deliverables")
    def deliverables(type: str | None = None, status: str | None = None):
        from ..deliverables import DeliverableStore

        store = DeliverableStore(kb)
        return {"items": [d.to_dict() for d in store.list(type=type, status=status)], "stats": store.stats()}

    @app.get("/api/deliverables/{did}")
    def deliverable(did: str):
        from ..deliverables import DeliverableStore

        store = DeliverableStore(kb)
        d = store.resolve(did)
        if d is None:
            raise HTTPException(404, "交付物不存在")
        return {**d.to_dict(), "content": d.content}

    @app.put("/api/deliverables/{did}")
    def update_deliverable(did: str, req: dict):
        from ..deliverables import DeliverableStore

        store = DeliverableStore(kb)
        if store.resolve(did) is None:
            raise HTTPException(404, "交付物不存在")
        store.update(did, **{k: v for k, v in (req or {}).items() if k in ("status", "title", "content")})
        d = store.get(did)
        return {**d.to_dict(), "content": d.content}

    @app.post("/api/deliverables/{did}/export")
    def export_deliverable(did: str, req: dict):
        from ..deliverables import DeliverableStore

        store = DeliverableStore(kb)
        if store.resolve(did) is None:
            raise HTTPException(404, "交付物不存在")
        path = (req or {}).get("path")
        if not path:
            raise HTTPException(400, "path 不能为空")
        fmt = (req or {}).get("format") or "md"
        if fmt not in ("md", "json"):
            raise HTTPException(400, "format 只支持 md / json")
        try:
            p = store.export(did, path, fmt=fmt)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "path": str(p)}

    # ---------------------------------------------------------------- 白名单
    @app.get("/api/whitelist")
    def whitelist():
        from ..whitelist import Whitelist

        wl = Whitelist(kb)
        return {"items": [e.to_dict() for e in wl.list()], "stats": wl.stats()}

    @app.post("/api/whitelist")
    def add_whitelist(req: dict):
        from ..whitelist import Whitelist, WhitelistError

        try:
            e = Whitelist(kb).add(
                title=req.get("title") or "",
                reason=req.get("reason") or "",
                doi=req.get("doi"),
                level_at_confirm=req.get("level_at_confirm") or "not_found",
            )
        except WhitelistError as exc:
            raise HTTPException(400, str(exc)) from exc
        return e.to_dict()

    @app.delete("/api/whitelist/{entry_id}")
    def remove_whitelist(entry_id: str):
        from ..whitelist import Whitelist

        return {"ok": Whitelist(kb).remove(entry_id)}

    # ---------------------------------------------------------------- 模型提供商
    def _provider_store():
        from ..providers import ProviderStore

        return ProviderStore(kb, cfg.data_path)

    @app.get("/api/providers")
    def providers():
        from ..providers import PROTOCOLS, dump_presets

        store = _provider_store()
        active, active_model = store.get_active()
        return {
            "items": [p.to_dict() for p in store.list()],
            "presets": dump_presets(),
            "protocols": [{"key": k, "label": v} for k, v in PROTOCOLS.items()],
            "active": {"provider_id": active.id if active else None, "model": active_model},
            "secrets": store.secrets.describe(),
        }

    @app.post("/api/providers")
    def create_provider(req: dict):
        from ..providers import ProviderError

        try:
            p = _provider_store().create(
                name=req.get("name") or "",
                base_url=req.get("base_url") or "",
                api_key=req.get("api_key") or "",
                protocol=req.get("protocol") or "openai",
                chat_model=req.get("chat_model"),
                preset=req.get("preset"),
            )
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        return p.to_dict()

    @app.put("/api/providers/{pid}")
    def update_provider(pid: str, req: dict):
        from ..providers import ProviderError

        try:
            p = _provider_store().update(
                pid,
                name=req.get("name"),
                base_url=req.get("base_url"),
                api_key=req.get("api_key") or None,
                protocol=req.get("protocol"),
                chat_model=req.get("chat_model"),
                multimodal=req.get("multimodal"),
            )
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        return p.to_dict()

    @app.delete("/api/providers/{pid}")
    def delete_provider(pid: str):
        return {"ok": _provider_store().delete(pid)}

    @app.post("/api/providers/{pid}/fetch-models")
    def fetch_models(pid: str):
        from ..providers import ProviderError

        try:
            return {"ok": True, "items": _provider_store().fetch_models(pid)}
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/providers/{pid}/models")
    def set_models(pid: str, req: dict):
        mode = (req or {}).get("mode") or "replace"
        models = req.get("models") or []
        if mode == "merge":
            return _provider_store().add_models(pid, models)
        return {"ok": True, "count": _provider_store().set_models(pid, models)}

    @app.post("/api/providers/{pid}/test")
    def test_provider(pid: str):
        return _provider_store().test(pid)

    @app.post("/api/providers/{pid}/active")
    def set_active(pid: str, req: dict):
        model_id = req.get("model_id") or ""
        if not model_id:
            raise HTTPException(400, "model_id 不能为空")
        ok_ = _provider_store().set_active_model(pid, model_id)
        return {"ok": ok_}

    @app.get("/api/models")
    def models():
        """chat 的模型选择器数据。"""
        store = _provider_store()
        active, active_model = store.get_active()
        return {
            "items": store.chat_options(),
            "active": {"provider_id": active.id if active else None, "model": active_model},
        }

    @app.exception_handler(Exception)
    async def unhandled(_request, exc):  # pragma: no cover
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


def run_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")
