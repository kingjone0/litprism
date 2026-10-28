"""FastAPI Web 界面：把 CLI 的能力搬到浏览器里，便于交互式探索。

启动：``python -m litprism serve``  默认 http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
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
    from ..whitelist import Whitelist
    verifier = PaperVerifier(cfg, Whitelist(kb))

    def task_llm():
        from ..llm import build_active_llm
        try:
            return build_active_llm(kb, cfg, allow_mock=True, mock_factory=build_llm)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # 静态资源（app.css / app.js）
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- 页面
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    # ---------------------------------------------------------------- 状态
    @app.get("/api/ping")
    def ping():
        """存活探测。不读任何数据，也是外壳模式下唯一免 token 的 /api 路径。"""
        return {"pong": True, "app": cfg.app.name}

    @app.get("/api/status")
    def status():
        import copy as _copy

        from ..providers import apply_active_window

        # 用副本探测：查询状态不该改写运行中的配置
        probe = _copy.deepcopy(cfg)
        window_info = apply_active_window(probe, kb)
        return {
            "app": cfg.app.name,
            "stats": kb.stats(),
            "llm": llm_status(cfg),
            "sources": verifier.enabled_sources(),
            "window": window_info,
            "config": config_to_dict(cfg),
            "config_path": str(Path(cfg.config_path).resolve()) if cfg.config_path else None,
            'schema_version': kb._con.execute('PRAGMA user_version').fetchone()[0],
        }

    @app.patch("/api/config")
    def patch_config_api(req: dict):
        """局部更新配置。**校验通过才落盘**，非法配置一律拒绝。

        注意：数据目录、分块参数等需要重建索引的项，改动后要重启应用才完全生效。
        """
        from ..config import patch_config as _patch

        if any(k in (req.get('llm') or {}) for k in ('max_tokens', 'review_max_tokens', 'cross_review_max_tokens')) or 'output_reserve_tokens' in (req.get('context') or {}):
            raise HTTPException(400, '任务输出额度已改为自动分配；请在模型目录维护上下文窗口与模型最大输出能力。')
        if not cfg.config_path:
            raise HTTPException(400, "当前使用内置默认配置，没有可写入的配置文件。")
        if 'data_dir' in (req.get('app') or {}):
            raise HTTPException(400, '请使用备份恢复中的目录切换；运行中不能直接修改数据目录。')
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
    def papers(status_filter: str | None = None, q: str | None = None,
               limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0),
               collection_id: str | None = None):
        try:
            return kb.paper_page(status=status_filter, query=q, collection_id=collection_id,
                                 limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/papers/catalog')
    def paper_catalog():
        items = kb.paper_catalog()
        return {'items': items, 'count': len(items)}

    @app.patch("/api/paper/{paper_id}")
    def rename_paper(paper_id: str, req: dict):
        """改显示名。只动 ``display_title``，原始题录标题保持不变。"""
        if 'collection_id' in req:
            cid = req['collection_id']
            if cid and not kb._con.execute('SELECT id FROM collections WHERE id=? AND deleted_at IS NULL', (cid,)).fetchone():
                raise HTTPException(400, '分组不存在')
            if not kb.get_paper(paper_id):
                raise HTTPException(404, '未找到论文')
            with kb.tx() as con:
                con.execute('UPDATE papers SET collection_id=? WHERE id=?', (cid or None, paper_id))
            return {'ok': True}
        if not kb.rename_paper(paper_id, req.get("display_title") or ""):
            raise HTTPException(404, "未找到论文")
        return {"ok": True}

    @app.delete("/api/paper/{paper_id}")
    def delete_paper(paper_id: str):
        if not kb.get_paper(paper_id):
            raise HTTPException(404, "未找到论文")
        from datetime import datetime
        with kb.tx() as con:
            con.execute('UPDATE papers SET deleted_at=? WHERE id=?', (datetime.now().isoformat(), paper_id))
        return {"ok": True}

    @app.get("/api/paper/{paper_id}/text")
    def paper_text(paper_id: str, material_id: str | None = None):
        with kb._tx_lock:
            """原文分块（按页分组）。供论文详情的「原文」标签使用。"""
            p = kb.resolve_paper(paper_id)
            if not p:
                raise HTTPException(404, "未找到论文")
            if material_id and not any(m['id'] == material_id for m in kb.list_materials(p['id'])):
                raise HTTPException(404, '材料版本不存在或不属于该论文。')
            chunks = kb.get_chunks(p["id"], limit=2000, material_id=material_id)
            pages: dict[int | None, list[dict]] = {}
            for c in chunks:
                pages.setdefault(c.get("page"), []).append(
                    {"section": c.get("section"), "text": c.get("text"), "chunk_id": c.get("id")}
                )
            ordered = [
                {"page": pg, "chunks": items}
                for pg, items in sorted(pages.items(), key=lambda x: (x[0] is None, x[0] or 0))
            ]
            return {"total": len(chunks), "pages": ordered, 'material_id': material_id or p.get('current_material_id')}

    @app.get('/api/paper/{paper_id}/materials')
    def paper_materials(paper_id: str):
        with kb._tx_lock:
            p = kb.resolve_paper(paper_id)
            if not p:
                raise HTTPException(404, '未找到论文')
            return {'items': kb.list_materials(p['id']), 'current_material_id': p.get('current_material_id'),
                    'analyses': [dict(r) for r in kb._con.execute(
                        "SELECT id,material_id,model,created_at FROM analysis_versions WHERE paper_id=? AND id NOT LIKE 'ac_%' ORDER BY rowid DESC", (p['id'],))]}

    @app.get("/api/paper/{paper_id}")
    def paper(paper_id: str, version_id: str | None = None):
        with kb._tx_lock:
            p = kb.resolve_paper(paper_id)
            if not p:
                raise HTTPException(404, "未找到论文")
            analysis = kb.get_analysis(p["id"], version_id=version_id)
            if version_id and not analysis:
                raise HTTPException(404, '历史分析不存在。')
            payload = dict((analysis or {}).get("payload") or {})
            payload.setdefault('concepts', kb.list_concepts(p['id']) if not version_id else [])
            p.pop("full_text", None)
            from ..candidate_snapshots import pending
            candidate = pending(kb, p['id'], analysis) if not version_id else None
            from ..materials import reading_status
            return {
                "paper": p,
                'reading': reading_status(kb, p),
                "analysis": payload,
                'candidate_analysis': (candidate or {}).get('payload'),
                "markdown": format_analysis(p, payload) if payload else "",
                "chunks": len(kb.get_chunks(p["id"])),
                'analysis_material': {'id': (analysis or {}).get('material_id'),
                                      'current': (analysis or {}).get('material_current', False)} if analysis else None,
                'materials': kb.list_materials(p['id']),
                'analysis_version_id': version_id,
                'analysis_history': [dict(r) for r in kb._con.execute(
                    "SELECT id,material_id,model,created_at FROM analysis_versions WHERE paper_id=? AND id NOT LIKE 'ac_%' ORDER BY rowid DESC", (p['id'],))],
            }

    @app.post("/api/analyze/{paper_id}")
    def analyze(paper_id: str, force: bool = False):
        p = kb.resolve_paper(paper_id)
        if not p:
            raise HTTPException(404, "未找到论文")
        try:
            data = analyze_paper(kb, cfg, task_llm(), p["id"], force=force)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e
        return {"analysis": data, "markdown": format_analysis(p, dict(data))}

    # ---------------------------------------------------------------- 导入
    @app.post("/api/upload")
    async def upload(files: Annotated[list[UploadFile], File()], analyze: bool = True, verify: bool = True,
                     material_policy: str = 'replace'):
        if material_policy not in ('replace', 'keep_both'):
            raise HTTPException(400, '材料策略必须为 replace 或 keep_both。')
        results = []
        from ..tasks import checkpoint
        for index, f in enumerate(files, 1):
            checkpoint(f'导入第 {index}/{len(files)} 个文件：解析与核验')
            suffix = Path(f.filename or "").suffix.lower()
            if suffix not in SUPPORTED_EXT:
                results.append({"file": f.filename, "ok": False, "error": f"不支持的格式 {suffix}"})
                continue
            with tempfile.NamedTemporaryFile(prefix='litprism_', suffix=suffix, delete=False) as upload_file:
                upload_file.write(await f.read())
                tmp = Path(upload_file.name)
            try:
                doc = load_document(tmp)
                try:
                    from ..enrich import enrich_document
                    enrich_document(doc, verifier)
                except Exception:
                    pass
                v_level, v_conf, v_status, v_ev = "unknown", 0.0, "unverified", {}
                vr = verifier.verify(
                    title=doc.title, doi=doc.doi, authors=doc.authors,
                    year=doc.year, arxiv_id=doc.arxiv_id, log=True,
                )
                v_level, v_conf = vr.level, vr.confidence
                v_status = "verified" if vr.level == "verified" else "unverified"
                v_ev = vr.to_dict()

                from ..evidence import require_citable
                try:
                    require_citable(v_level)
                except ValueError as exc:
                    results.append({"file": f.filename, "ok": False, "error": str(exc),
                                    "title": doc.title, "doi": doc.doi, "verification": v_ev})
                    continue
                checkpoint('保存核验通过的文献')
                with kb.tx():
                    pid = kb.add_paper(
                        title=doc.title, authors=doc.authors, year=doc.year, abstract=doc.abstract,
                        doi=doc.doi, arxiv_id=doc.arxiv_id, source="local_pdf", origin=f.filename,
                        full_text=doc.full_text, tags=["本地上传"], material_level="fulltext",
                        verify_status=v_status, verify_level=v_level,
                        verify_confidence=v_conf, verify_evidence=v_ev,
                    )
                    chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
                    kb.add_chunks(pid, chunks, policy=material_policy, level='fulltext', source='local_pdf',
                                  origin=f.filename, full_text=doc.full_text)
                entry = {
                    "file": f.filename, "ok": True, "paper_id": pid,
                    "title": doc.title, "pages": doc.page_count, "chunks": len(chunks),
                    "verify_level": v_level, "verify_confidence": v_conf,
                    "warnings": doc.warnings,
                }
                if analyze:
                    try:
                        analyze_paper(kb, cfg, task_llm(), pid, force=True)
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
        agent = QAAgent(kb, cfg, task_llm())
        ans = agent.ask(req.question, paper_ids=req.paper_ids, top_k=req.top_k)
        return ans.to_dict()

    # ---------------------------------------------------------------- 检索
    @app.post("/api/search")
    def search(req: SearchReq):
        local = search_local(kb, req.query, top_k=req.limit, llm=task_llm(), citable_only=True)
        out = {"local": local, "external": []}
        if req.external:
            recs = search_external(cfg, verifier, req.query, limit=req.limit)
            out["external"] = recs
            try:
                # 候选池只做留档与去重；能不能引用仍由核验/人工放行决定。
                from ..candidates import save_candidates
                out["candidates"] = save_candidates(kb, recs, query=req.query, kind='paper')
            except Exception:  # noqa: BLE001 - 留档失败不该让检索本身失败
                out["candidates"] = None
        return out

    # ---------------------------------------------------------------- 对比
    @app.post("/api/compare")
    def compare(req: CompareReq):
        agent = CompareAgent(kb, cfg, task_llm())
        try:
            data = agent.compare(req.refs, aspects=req.aspects)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        return {"data": data, "markdown": render_comparison(data), 'sources': citation_sources(data.get('_citations'))}

    # ---------------------------------------------------------------- 推荐
    @app.post("/api/recommend")
    def recommend(req: RecommendReq):
        agent = RecommendAgent(kb, cfg, task_llm(), verifier)
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
        if not vr.exists or (not vr.best_match and not vr.manual):
            raise HTTPException(409, f"未通过核验，拒绝导入。{vr.message}")
        from ..verify import SourceRecord
        b = vr.best_match or SourceRecord(source='whitelist', title=req.title or req.doi or '', doi=req.doi)
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
            if p is None:
                raise HTTPException(404, '文献不存在')
            pid = p['id']
        return {"items": kb.list_concepts(paper_id=pid, term=term, limit=limit)}

    # ---------------------------------------------------------------- 会话
    @app.get("/api/conversations")
    def conversations(limit: int = Query(100, ge=1, le=2000), q: str = '', offset: int = Query(0, ge=0)):
        with kb.tx():
            items = kb.list_conversations(limit=limit, query=q, offset=offset)
            total = kb.count_conversations(q)
        return {'items': items, 'total': total, 'offset': offset, 'has_more': offset + len(items) < total}

    @app.post("/api/conversations")
    def create_conversation(req: dict):
        from ..scope import chat_paper_scope, chat_strict
        scope = req.get('scope')
        if scope is not None:
            if not isinstance(scope, dict):
                raise HTTPException(400, '范围格式不正确。')
            try:
                chat_paper_scope(kb, scope)
                chat_strict(scope, '')
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        cid = kb.add_conversation(req.get("title") or "新会话", scope=req.get("scope"))
        return {"id": cid}

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str):
        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        return {"ok": kb.delete_conversation(conv["id"])}

    def citation_sources(citations):
        """来源面板也走 GenUI 数据库回填，不信任模型或历史保存的页码。"""
        from ..genui import process as genui_process

        if isinstance(citations, str):
            citations = json.loads(citations)
        ids = [c["chunk_id"] for c in (citations or []) if c.get("chunk_id")]
        if not ids:
            return []
        fence = "```lit-ui\n" + json.dumps({"v": 1, "type": "citation-list", "chunk_ids": ids}) + "\n```"
        return genui_process(fence, kb).segments

    def evidence_links(citations, content):
        from ..textutil import parse_citations
        from .evidence_routes import evidence_detail
        if isinstance(citations, str):
            citations = json.loads(citations)
        used = {tag for tag, _ in parse_citations(content)}
        links = []
        for cite in citations or []:
            if cite.get('tag') not in used or not cite.get('chunk_id'):
                continue
            try:
                source = evidence_detail(kb, cite['chunk_id'])
                links.append({'ref': cite['chunk_id'], 'tag': cite.get('tag'), 'title': source.get('title') or source.get('paper_title'),
                              'kind': source['source_kind'], 'page': source.get('page'), 'trust_level': source['trust_level'], 'session_only': bool(cite.get('web_grant_id') and source['trust_level'] == 'pending'), 'citable': bool(source['citable'] or cite.get('web_grant_id') and source['trust_level']=='pending')})
            except HTTPException:
                links.append({'ref': cite['chunk_id'], 'title': '来源已失效', 'tag': cite.get('tag')})
        return links

    @app.get("/api/conversations/{conv_id}/messages")
    def messages(conv_id: str):
        from ..genui import process as genui_process

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        items = kb.get_messages(conv["id"])
        # 每条消息附带 segments（组件已由框架注入核验状态与页码）
        for m in items:
            meta = json.loads(m.get('meta') or '{}')
            check = json.loads(m.get('citation_check') or '{}')
            saved_cites = json.loads(m.get('citations') or '[]')
            if m['role'] == 'assistant' and saved_cites:
                from ..evidence import citation_issues, stored_excerpts
                try:
                    issues = citation_issues(m['content'], stored_excerpts(kb, saved_cites, allow_session=True))
                except ValueError as exc:
                    issues = [str(exc)]
                if issues:
                    check = {'passed': False, 'issues': issues}
            m['citation_check'] = check
            m['model'] = meta.get('model')
            m['candidate_links'] = meta.get('candidate_links') or []
            m['source_warning'] = meta.get('source_warning') or ''
            m['reasoning'] = meta.get('reasoning') or ''
            m['request_settings'] = meta.get('request_settings') or {}
            m['status'] = ('invalid_citation' if check.get('passed') is False else meta.get('status') or 'answered')
            m['usage'] = meta.get('usage') or {}
            m['retrieval'] = meta.get('retrieval') or {}
            m['guard_issues'] = meta.get('guard_issues') or []
            from ..qa_claims import presentation_warnings
            m['audit_warnings'], m['audit_notices'] = presentation_warnings(meta.get('audit_warnings') or [])
            m['scope_snapshot'] = meta.get('scope_snapshot')
            m['qa_review_id'] = meta.get('qa_review_id')
            from ..qa_reviews import latest_decision
            m['human_review'] = latest_decision(kb, m['qa_review_id'])
            if m['role'] == 'assistant' and m['human_review'] and m['status'] == 'answered':
                m['status'] = 'human_'+m['human_review']['action']
                if m['human_review']['action'] != 'partial':
                    m['content'] = '此回复已完成人工裁决，原始草稿留在审核记录中。请查看修订正文与知识快照。'
            if m['qa_review_id'] and m['role'] == 'assistant' and m['status'] in ('answered', 'human_partial'):
                from ..qa_unit_reviews import display_units
                m['unit_review'] = display_units(kb, m['qa_review_id'])
            audit_links = evidence_links(saved_cites, m.get('content') or '')
            if m['role'] == 'assistant' and m['status'] == 'invalid_citation':
                m['content'] = '回答的来源校验未通过，已阻止展示结论。请核对原文后重新提问。'
                m['citations'] = []
            m['segments'] = genui_process(m.get('content') or '', kb).segments
            m['source_links'] = audit_links
        latest = next((m for m in reversed(items) if m["role"] == "assistant"), {})
        return {"conversation": conv, "items": items, "sources": citation_sources(latest.get("citations"))}

    @app.post("/api/chat/{conv_id}")
    def chat(conv_id: str, req: dict):
        return chat_body(conv_id, req)

    def chat_body(conv_id: str, req: dict, *, execution=None):
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
        try:
            top_k = int(req.get('top_k', cfg.retrieval.top_k))
            if not 1 <= top_k <= 50:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, '召回数必须在 1–50 之间。') from None

        scope = conv.get('scope') or {}
        if isinstance(scope, str):
            scope = json.loads(scope)
        from ..scope import chat_paper_scope, chat_strict
        try:
            paper_ids = chat_paper_scope(kb, scope)
            strict_scope = chat_strict(scope, question)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        scope_snapshot = {'scope': scope, 'paper_ids': paper_ids, 'strict': strict_scope}
        chat_llm = execution['client'] if execution else task_llm()
        chat_cfg = getattr(chat_llm, 'runtime_config', cfg)
        user_msg = execution['user'] if execution else kb.append_message(conv["id"], "user", question, meta={'scope_snapshot': scope_snapshot})
        candidates, source_warning, external = [], '', []
        if req.get('web_search') and strict_scope:
            source_warning = '本轮明确限制来源，未进行联网补充。'
        if req.get('web_search') and not strict_scope:
            from ..tasks import checkpoint
            from .evidence_routes import discover_web
            checkpoint('检索本轮外部证据；保留搜索摘要及未审核标识')
            try:
                discover = lambda: discover_web(kb, chat_cfg, question)
                discovered = chat_llm.invoke('web_search', {'question': question}, discover) if execution else discover()
                candidates = [{'ref': s['id'], 'title': s['title']} for s in discovered['items']]
                from ..chat_evidence import grant, excerpts as web_excerpts
                external = web_excerpts(kb, grant(kb, conv['id'], user_msg['id'], question, discovered['items']))
            except HTTPException as exc:
                source_warning = str(exc.detail)
            except ValueError as exc:
                if execution:
                    from ..analysis_runs import RunConflict
                    raise RunConflict(str(exc)) from exc
                raise HTTPException(409, str(exc)) from exc
        try:
            ans = QAAgent(kb, chat_cfg, chat_llm).ask(
                question, top_k=top_k,
                conversation_id=conv["id"], before_seq=user_msg["seq"],
                paper_ids=paper_ids,
                strict_scope=strict_scope, external=external,
            )
        except Exception as e:  # noqa: BLE001
            if execution:
                raise
            kb.append_message(
                conv["id"], "assistant",
                f"回答失败：{type(e).__name__}: {e}",
                meta={"error": True},
            )
            raise HTTPException(500, f"回答失败：{e}") from e

        if execution:
            execution['validate']()
            if ans.status == 'error':
                raise RuntimeError('模型调用失败；请求状态与用量已留存。')
            ans.usage = execution['usage']()

        # Refusals may explain the evidence boundary with valid quotations.
        # Preserve their checked links without treating the refusal as a fact.
        excerpts = (getattr(ans, "excerpts", []) or []) if (
            ans.status in ('answered', 'refused', 'needs_review') and ans.citation_check_passed) else []
        from ..textutil import parse_citations
        used_tags = {tag for tag, _ in parse_citations(ans.answer)}
        excerpts = [e for e in excerpts if e.get('tag') in used_tags]
        cites = [
            {
                "tag": e.get("tag"), "chunk_id": e.get("chunk_id"),
                "paper_id": e.get("paper_id"), "page": e.get("page"),
                "section": e.get("section"),
                "web_grant_id": e.get('web_grant_id'),
            }
            for e in excerpts
        ]
        check_dict = {
            "passed": bool(getattr(ans, "citation_check_passed", True)),
            "issues": list(getattr(ans, "citation_issues", []) or []),
        }

        content = ans.answer
        from ..tasks import checkpoint
        checkpoint('保存回答与引用检查')
        # 模型没输出结构化围栏时，框架补一个引用片段组件
        if excerpts and not parse_fences(content):
            chunk_ids = [e["chunk_id"] for e in excerpts if e.get("chunk_id") and not e.get("web_grant_id")]
            if chunk_ids:
                content += (
                    "\n\n```lit-ui\n"
                    + json.dumps(
                        {"v": 1, "type": "citation-list", "chunk_ids": chunk_ids},
                        ensure_ascii=False,
                    )
                    + "\n```"
                )

        import uuid
        qa_review_id = ('qr_'+uuid.uuid4().hex[:16]) if ans.draft_answer and ans.excerpts and ans.status in (
            'answered', 'needs_review', 'invalid_citation', 'refused') else None
        proc = genui_process(content, kb)
        auto_compact = None
        def result_payload():
            from ..qa_unit_reviews import display_units
            from ..qa_claims import presentation_warnings
            audit_warnings, audit_notices = presentation_warnings(ans.evidence_audit.get('warnings', []))
            return {
                "user": user_msg,
                "assistant": {**asst, "segments": proc.segments, "citation_check": check_dict, 'model': ans.model,
                              'source_links': evidence_links(cites, ans.answer), 'candidate_links': candidates,
                              'source_warning': source_warning, 'reasoning': ans.reasoning,
                              'request_settings': ans.request_settings, 'status': ans.status,
                              'guard_issues': ans.guard_issues, 'audit_warnings': audit_warnings, 'audit_notices': audit_notices,
                              'qa_review_id': qa_review_id,
                              'unit_review': display_units(kb, qa_review_id) if qa_review_id and ans.status == 'answered' else None,
                              'usage': ans.usage, 'retrieval': ans.retrieval, 'scope_snapshot': scope_snapshot},
                "confidence": getattr(ans, "confidence", None),
                "insufficient_evidence": bool(getattr(ans, "insufficient_evidence", False)),
                "sources": citation_sources(cites),
                "issues": proc.issues,
                "auto_compact": auto_compact,
                "model": ans.model,
                'status': ans.status,
                'usage': ans.usage,
                'retrieval': ans.retrieval,
            }

        with kb.tx():
            if execution:
                execution['validate']()
            if cites:
                from ..evidence import stored_excerpts
                stored_excerpts(kb, cites, allow_session=True)
            asst = kb.append_message(
                conv["id"], "assistant", content,
                citations=cites, citation_check=check_dict,
                meta={'model': ans.model, 'provider_usage': ans.provider_usage, 'estimated_input': ans.request_tokens,
                      'reasoning': ans.reasoning, 'request_settings': ans.request_settings,
                      'status': ans.status, 'usage': ans.usage, 'retrieval': ans.retrieval,
                      'guard_issues': ans.guard_issues, 'audit_warnings': ans.evidence_audit.get('warnings', []),
                      'scope_snapshot': scope_snapshot,
                      'blocked_answer': ans.draft_answer if ans.status in ('invalid_citation', 'needs_review') else '',
                      'candidate_links': candidates, 'source_warning': source_warning, 'qa_review_id': qa_review_id,
                      'chat_run_id': execution['rid'] if execution else None},
            )
            if qa_review_id:
                from ..qa_reviews import save as save_qa_review
                save_qa_review(kb, qa_review_id, asst, conv['id'], question, ans, scope_snapshot)
            if execution:
                execution['publish'](result_payload())
        last_request = {'model': ans.model, 'provider_usage': ans.provider_usage, 'estimated_input': ans.request_tokens,
                        'usage': ans.usage, 'status': ans.status}
        with kb.tx() as con:
            con.execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ('last_request:'+conv['id'], json.dumps(last_request)))

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

            meter = Telemetry(kb, chat_cfg).meter
            policy = Policy(chat_cfg)
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
                    seam = Seam(kb, chat_cfg)
                    tx_id, _ = seam.begin(conv["id"], decision.trigger, sel)
                    if tx_id:
                        in_range = [m for m in msgs if sel.start_seq <= m["seq"] <= sel.end_seq]
                        summary = Summarizer(
                            chat_cfg, chat_llm, backend=ExtractiveSummarizer()
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

        return result_payload()

    # ---------------------------------------------------------------- 上下文
    @app.get("/api/context/{conv_id}")
    def context(conv_id: str):
        from ..context.telemetry import Telemetry

        conv = kb.resolve_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        from copy import deepcopy

        from ..providers import apply_active_window
        runtime = deepcopy(cfg)
        apply_active_window(runtime, kb)
        snap = Telemetry(kb, runtime).snapshot(conv["id"])
        snap["conversation"] = {"id": conv["id"], "title": conv["title"]}
        last = kb._con.execute('SELECT value FROM meta WHERE key=?', ('last_request:'+conv['id'],)).fetchone()
        snap['last_request'] = json.loads(last['value']) if last else None
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
        summary = Summarizer(cfg, task_llm(), backend=ExtractiveSummarizer()).summarize(
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
            "围绕「{{topic}}」整理要点。\n\n"
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

        reg.reload()
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
        current = store.resolve(did)
        if current is None:
            raise HTTPException(404, "交付物不存在")
        from dataclasses import replace
        fields = {k: v for k, v in (req or {}).items() if k in ('status', 'title', 'content')}
        try:
            store.validate_export(replace(current, **fields))
            store.update(current.id, **fields)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        d = store.get(current.id)
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
            latest = verifier.verify(title=req.get('title') or '', doi=req.get('doi'))
            e = Whitelist(kb).add(
                title=req.get("title") or "",
                reason=req.get("reason") or "",
                doi=req.get("doi"),
                level_at_confirm=latest.level,
                verify_evidence=latest.to_dict(),
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
        from ..providers import ProviderError
        mode = (req or {}).get("mode") or "replace"
        models = req.get("models") or []
        try:
            if mode == "merge":
                return _provider_store().add_models(pid, models)
            return {"ok": True, "count": _provider_store().set_models(pid, models)}
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/providers/{pid}/test")
    def test_provider(pid: str):
        return _provider_store().test(pid)

    @app.post("/api/providers/{pid}/active")
    def set_active(pid: str, req: dict):
        model_id = req.get("model_id") or ""
        if not model_id:
            raise HTTPException(400, "model_id 不能为空")
        store = _provider_store()
        ok_ = store.set_active_model(pid, model_id)
        refreshed = None
        if ok_:
            # 选模即尝试刷新一次能力目录：一次拉取后长期本地复用，失败不影响选模。
            try:
                store.fetch_models(pid)
                refreshed = True
            except Exception:  # noqa: BLE001 - 网络/权限问题不应阻断选模
                refreshed = False
        return {"ok": ok_, "capability_refreshed": refreshed}

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

    from .workflows import register_workflows
    register_workflows(app, kb, cfg, task_llm, verifier)
    from .evidence_routes import register_evidence
    register_evidence(app, kb, cfg, verifier)
    from .semantic_routes import register_semantics
    register_semantics(app, kb)
    from ..qa_reviews import register_qa_reviews
    register_qa_reviews(app, kb)
    from ..tasks import register_tasks
    register_tasks(app)
    from .analysis_run_routes import register_analysis_runs
    register_analysis_runs(app, kb, cfg, task_llm)
    from .chat_run_routes import register_chat_runs
    register_chat_runs(app, kb, cfg, task_llm, chat_body, messages)
    from .backup_routes import register_backups
    register_backups(app, kb, cfg)
    return app


def run_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")
