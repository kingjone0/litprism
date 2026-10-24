"""命令行入口。

用法示例::

    python -m litprism init
    python -m litprism add paper.pdf --verify
    python -m litprism analyze --all
    python -m litprism show p_xxxxxxxx
    python -m litprism ask "这篇论文的核心创新点是什么？"
    python -m litprism compare p_aaa p_bbb
    python -m litprism recommend --topic "神经辐射场 三维重建" --import
    python -m litprism verify --doi 10.1038/nature12373
    python -m litprism serve
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .agents import CompareAgent, QAAgent, RecommendAgent, render_comparison, render_recommendation
from .config import config_to_dict, load_config
from .db import KnowledgeBase
from .extract import analyze_all, analyze_paper, export_notes, format_analysis, _verify_badge
from .ingest import SUPPORTED_EXT, chunk_document, load_document, summarize_document
from .invariants import FAILURE_CODES
from .llm import active_llm_status, build_active_llm
from .relations import cluster, compute_relations
from .search import import_record, search_external, search_local
from .textutil import extract_doi, truncate
from .verify import PaperVerifier
from .whitelist import (
    LEVELS,
    Whitelist,
    WhitelistError,
    badge,
    format_entries,
    is_citable,
    label,
)

# Windows 控制台强制 UTF-8，避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------
def hr(char: str = "─", n: int = 78) -> None:
    print(char * n)


def title(text: str) -> None:
    print()
    hr("═")
    print(f"  {text}")
    hr("═")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def err(msg: str) -> None:
    print(f"  ❌ {msg}", file=sys.stderr)


def kv(k: str, v) -> None:
    print(f"  {k:<14}{v}")


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, config_path: str | None = None):
        self.cfg = load_config(config_path)
        self.cfg.ensure_dirs()
        self.kb = KnowledgeBase(self.cfg)
        from .providers import ProviderStore, apply_active_window
        if ProviderStore(self.kb,self.cfg.data_path).get_active()[0] is not None:
            apply_active_window(self.cfg,self.kb)
        self._llm = None
        self._verifier = None

    @property
    def llm(self):
        if self._llm is None:
            self._llm = build_active_llm(self.kb, self.cfg, allow_mock=True)
        return self._llm

    @property
    def verifier(self) -> PaperVerifier:
        if self._verifier is None:
            # 挂上白名单：人工确认过的文献不再重复走网络核验
            self._verifier = PaperVerifier(self.cfg, Whitelist(self.kb))
        return self._verifier


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def cmd_init(ctx: Ctx, args) -> int:
    title("文献棱镜 LitPrism · 初始化")
    cfg = ctx.cfg
    kv("版本", __version__)
    kv("配置文件", cfg.config_path or "（使用内置默认值）")
    kv("数据目录", cfg.data_path)
    kv("数据库", cfg.db_path)
    llm = active_llm_status(ctx.kb,cfg)
    kv("大模型", f"{llm['provider']} / {llm.get('model', '-')}  [{llm['mode']}]")
    if llm.get("note"):
        warn(llm["note"])
    kv("已启用数据源", "、".join(ctx.verifier.enabled_sources()) or "无")
    if not ctx.verifier.enabled_sources():
        warn("没有可用的学术数据源，真实性核验将不可用，请检查 config.yaml")
    st = ctx.kb.stats()
    print()
    print("  知识库状态：")
    kv("论文", st["papers"])
    kv("文本块", st["chunks"])
    kv("已解读", st["analyses"])
    kv("术语", st["concepts"])
    kv("关联", st["relations"])
    kv("核验记录", st["verifications"])
    print()
    ok("初始化完成。下一步：python -m litprism add <论文.pdf>")
    return 0


def cmd_add(ctx: Ctx, args) -> int:
    title("导入论文")
    paths: list[Path] = []
    for p in args.paths:
        pp = Path(p)
        if pp.is_dir():
            paths += sorted([f for f in pp.rglob("*") if f.suffix.lower() in SUPPORTED_EXT])
        else:
            paths.append(pp)
    if not paths:
        err("没有找到可导入的文件")
        return 1

    added = 0
    for p in paths:
        print(f"\n▶ {p.name}")
        try:
            doc = load_document(p)
        except Exception as e:  # noqa: BLE001
            err(f"解析失败：{e}")
            continue
        print("  " + summarize_document(doc).replace("\n", "\n  "))
        for w in doc.warnings:
            warn(w)

        # 题录补全：用权威数据库校正本地解析的标题/作者/年份
        if not args.no_enrich:
            try:
                from .enrich import enrich_document
                er = enrich_document(doc, ctx.verifier)
                print(f"  题录：{er.summary()}")
            except Exception as e:  # noqa: BLE001
                warn(f"题录补全失败（不影响导入）：{e}")

        # 核验（若有 DOI 或标题）
        verify_evidence: dict = {}
        level, status, conf = "unknown", "unverified", 0.0
        vr = ctx.verifier.verify(
            title=doc.title, doi=doc.doi, authors=doc.authors, year=doc.year,
            arxiv_id=doc.arxiv_id, log=True,
        )
        print(f"  核验：{vr.message}")
        verify_evidence = vr.to_dict()
        level, conf = vr.level, vr.confidence
        status = "verified" if vr.level == "verified" else "unverified"

        from .evidence import require_citable
        try:
            require_citable(level)
        except ValueError as exc:
            err(str(exc))
            continue

        with ctx.kb.tx():
            pid = ctx.kb.add_paper(
                title=doc.title, authors=doc.authors, year=doc.year, abstract=doc.abstract,
                doi=doc.doi, arxiv_id=doc.arxiv_id, venue=doc.venue,
                source="local_pdf", origin=str(p), full_text=doc.full_text,
                material_level="fulltext",
                tags=[t for t in (args.tags or "").split(",") if t.strip()],
                verify_status=status, verify_level=level, verify_confidence=conf,
                verify_evidence=verify_evidence,
            )
            chunks = chunk_document(doc, ctx.cfg.retrieval.chunk_size, ctx.cfg.retrieval.chunk_overlap)
            n = ctx.kb.add_chunks(pid, chunks, level='fulltext', source='local_pdf',
                                  origin=str(p), full_text=doc.full_text)
        ok(f"已入库 {pid}（{doc.page_count} 页 / {n} 个文本块 / {len(doc.sections)} 个章节）")
        added += 1

        if not args.no_analyze:
            try:
                analyze_paper(ctx.kb, ctx.cfg, ctx.llm, pid, force=True)
                ok("结构化解读完成")
            except Exception as e:  # noqa: BLE001
                warn(f"结构化解读失败（可稍后重跑 analyze）：{e}")

    print()
    ok(f"共导入 {added} 篇")
    if added:
        print("  提示：运行 `python -m litprism relations` 可计算跨论文关联。")
    return 0


def cmd_list(ctx: Ctx, args) -> int:
    papers = ctx.kb.list_papers(status=args.status, query=args.query, limit=args.limit)
    title(f"论文列表（{len(papers)} 篇）")
    if not papers:
        print("  （空）")
        return 0
    for p in papers:
        print("  " + ctx.kb.paper_brief(p))
    return 0


def cmd_show(ctx: Ctx, args) -> int:
    p = ctx.kb.resolve_paper(args.ref)
    if not p:
        err(f"未找到论文：{args.ref}")
        return 1
    analysis = ctx.kb.get_analysis(p["id"])
    if not analysis:
        warn("尚未做结构化解读，正在生成…")
        try:
            analyze_paper(ctx.kb, ctx.cfg, ctx.llm, p["id"])
            analysis = ctx.kb.get_analysis(p["id"])
        except Exception as e:  # noqa: BLE001
            err(f"解读失败：{e}")
            return 1
    payload = dict(analysis.get("payload") or {})
    if not analysis.get('material_current'):
        warn('这份分析基于旧材料或未绑定材料；不代表当前原文，请重新解读。')
    payload["concepts"] = ctx.kb.list_concepts(p["id"])
    print(format_analysis(p, payload))
    return 0


def cmd_analyze(ctx: Ctx, args) -> int:
    title("结构化解读")
    if args.all:
        res = analyze_all(
            ctx.kb, ctx.cfg, ctx.llm, force=args.force,
            progress=lambda i, n, t: print(f"  [{i}/{n}] {t}"),
        )
        for pid, st in res:
            mark = "✅" if st == "ok" else ("⏭️" if st == "skipped" else "❌")
            print(f"  {mark} {pid}  {st}")
        return 0
    if not args.ref:
        err("请指定论文，或使用 --all")
        return 1
    p = ctx.kb.resolve_paper(args.ref)
    if not p:
        err(f"未找到论文：{args.ref}")
        return 1
    data = analyze_paper(ctx.kb, ctx.cfg, ctx.llm, p["id"], force=args.force)
    payload = dict(data)
    payload["concepts"] = ctx.kb.list_concepts(p["id"])
    print(format_analysis(p, payload))
    return 0


def cmd_search(ctx: Ctx, args) -> int:
    title(f"检索：{args.query}")
    hits = search_local(ctx.kb, args.query, top_k=args.limit, llm=ctx.llm)
    print(f"  【本地知识库】命中 {len(hits)} 个片段")
    for h in hits:
        head = f"  · [{h['paper_id']}] {truncate(h.get('paper_title') or '', 52)}"
        loc = []
        if h.get("page"):
            loc.append(f"p.{h['page']}")
        if h.get("section"):
            loc.append(h["section"])
        print(head + (f"  ({' / '.join(loc)})" if loc else "") + f"   score={h.get('score')}")
        print(f"      {truncate(h['text'].replace(chr(10), ' '), 160)}")

    if args.external:
        print()
        print("  【外部权威数据库】（结果均来自数据库检索，真实存在）")
        recs = search_external(ctx.cfg, ctx.verifier, args.query, limit=args.limit)
        if not recs:
            print("      （无结果）")
        for i, r in enumerate(recs, 1):
            meta = " · ".join(
                str(x) for x in [", ".join(r["authors"][:3]), r.get("year"), r.get("venue")] if x
            )
            print(f"      {i}. {r['title']}")
            print(f"         {meta}   [来源: {', '.join(r['sources'])}]")
            if r.get("doi"):
                print(f"         DOI: {r['doi']}")
    return 0


def cmd_ask(ctx: Ctx, args) -> int:
    selected = [ctx.kb.resolve_paper(ref) for ref in (args.paper or [])]
    if any(p is None for p in selected):
        err('未找到指定论文；请核对 --paper，未扩大到全库。')
        return 1
    agent = QAAgent(ctx.kb, ctx.cfg, ctx.llm)
    paper_ids = [p['id'] for p in selected]
    ans = agent.ask(args.question, paper_ids=paper_ids or None, top_k=args.top_k)
    title("问答")
    print(f"  ❓ {ans.question}\n")
    print(ans.answer)
    print()
    hr()
    kv("置信度", ans.confidence)
    kv("依据是否充分", "否" if ans.insufficient_evidence else "是")
    kv("引用校验", "通过" if ans.citation_check_passed else "未通过")
    for issue in ans.citation_issues:
        warn(issue)
    if ans.excerpts:
        print("\n  引用片段：")
        for e in ans.excerpts:
            loc = f"p.{e['page']}" if e.get("page") else "-"
            print(f"    [{e['tag']}] {truncate(e['title'], 46)} ({loc} / {e['section']})")
    if ans.open_questions:
        print("\n  待进一步检索的问题：")
        for q in ans.open_questions:
            print(f"    · {q}")
    if args.show_evidence:
        print("\n  原文片段全文：")
        for e in ans.excerpts:
            print(f"\n  ── [{e['tag']}] {e['title']} p.{e.get('page')} ──")
            print("  " + e["text"].replace("\n", "\n  "))
    return 0


def cmd_compare(ctx: Ctx, args) -> int:
    agent = CompareAgent(ctx.kb, ctx.cfg, ctx.llm)
    try:
        data = agent.compare(args.refs, aspects=args.aspects)
    except Exception as e:  # noqa: BLE001
        err(str(e))
        return 1
    title("多篇论文对比")
    print(render_comparison(data))
    return 0


def cmd_recommend(ctx: Ctx, args) -> int:
    agent = RecommendAgent(ctx.kb, ctx.cfg, ctx.llm, ctx.verifier)
    try:
        data = agent.recommend(
            topic=args.topic, paper_ref=args.paper, limit=args.limit,
            strategies=args.strategies.split(",") if args.strategies else None,
            explain=not args.no_explain,
            import_to_kb=True if args.import_ else (False if args.no_import else None),
        )
    except Exception as e:  # noqa: BLE001
        err(str(e))
        return 1
    title("文献推荐")
    print(render_recommendation(data))
    return 0


def cmd_verify(ctx: Ctx, args) -> int:
    title("论文真实性核验")
    doi = args.doi
    query = args.query or ""
    if query and not doi:
        maybe = extract_doi(query)
        if maybe and args.auto_doi:
            doi = maybe
            query = ""
    if not query and not doi:
        err("请提供标题（位置参数）或 --doi")
        return 1
    vr = ctx.verifier.verify(title=query, doi=doi, log=True)
    print(f"  查询：{query or doi}")
    print(f"  结果：{vr.message}")
    kv("判定", f"{vr.level}  {badge(vr.level)}")
    kv("是否存在", "是" if vr.exists else "否")
    kv("置信度", f"{vr.confidence:.4f}")
    kv("响应数据源", "、".join(vr.sources_ok) or "无")
    if vr.sources_failed:
        kv("失败数据源", "、".join(vr.sources_failed))
    kv("耗时", f"{vr.elapsed_ms} ms")

    if vr.manual:
        print()
        kv("白名单条目", vr.whitelist_id or "-")
        kv("确认时自动判定", label(vr.original_level or ""))
        kv("确认依据", vr.confirm_reason)

    if vr.matches:
        print("\n  候选匹配：")
        for m in vr.matches:
            print(f"    · [{m.source}] 相似度 {m.similarity:.3f}  {truncate(m.title, 60)}")
            print(f"      {', '.join(m.authors[:4]) or '-'} · {m.year or '-'} · {m.venue or '-'}")
            if m.doi:
                print(f"      DOI: {m.doi}   {m.url or ''}")

    # ---- 人工确认放行 ----
    if args.confirm:
        print()
        title("人工确认放行")
        if vr.level == "verified":
            print("  该文献已通过自动核验，无需人工确认。")
        else:
            try:
                entry = Whitelist(ctx.kb).add(
                    title=query or (vr.best_match.title if vr.best_match else doi or ""),
                    reason=args.confirm,
                    doi=doi,
                    level_at_confirm=vr.level,
                    verify_evidence=vr.to_dict(),
                )
            except WhitelistError as e:
                err(str(e))
                return 2
            ok(f"已加入白名单 {entry.id}")
            print(f"  确认时自动判定：{label(entry.level_at_confirm)}（保留原判定，不覆盖）")
            print(f"  依据：{entry.reason}")
            print()
            print("  说明：白名单是**记录在案的例外**，不是抹掉证据的重写 ——")
            print("        verify_evidence 里保留了确认当时的完整核验结果，将来可复核。")
            print("        引用时徽章显示为「人工确认」，与自动核验的「已核验」区分。")

    if args.json:
        print(json.dumps(vr.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_whitelist(ctx: Ctx, args) -> int:
    wl = Whitelist(ctx.kb)

    if args.action == "add":
        if not args.title:
            err("请提供标题：whitelist add \"论文标题\" --reason \"确认依据\"")
            return 1
        try:
            entry = wl.add(
                title=args.title,
                reason=args.reason or "",
                doi=args.doi,
                level_at_confirm=args.level,
            )
        except WhitelistError as e:
            err(str(e))
            return 2
        title("加入白名单")
        ok(f"已加入 {entry.id}")
        print(f"  {entry.render()}")
        return 0

    if args.action == "remove":
        if not args.id:
            err("请提供白名单条目 id")
            return 1
        title("移除白名单")
        ok(f"已移除 {args.id}") if wl.remove(args.id) else err(f"未找到 {args.id}")
        return 0

    title("人工确认白名单")
    entries = wl.list(limit=args.limit)
    st = wl.stats()
    print(f"  共 {st['total']} 条")
    if st["by_original_verdict"]:
        print(
            "  按确认时自动判定："
            + "、".join(f"{label(k)} {v}" for k, v in st["by_original_verdict"].items())
        )
    print()
    print(format_entries(entries))
    if entries:
        print()
        print("  注意：这些文献**不是自动核验通过**，而是人工确认放行。")
        print("        引用时徽章显示「人工确认」，与「已核验」区分。")
    return 0


def cmd_import(ctx: Ctx, args) -> int:
    title("核验并导入外部文献")
    vr = ctx.verifier.verify(title=args.query or "", doi=args.doi, log=True)
    print(f"  核验：{vr.message}")
    if not is_citable(vr.level):
        err(
            f"未通过核验（{label(vr.level)}），拒绝导入"
            "（避免向本地知识库注入无法确认真实性的文献）。"
        )
        print()
        print("  如果你确认这篇文献真实存在，可以：")
        print(f"    litprism verify \"{args.query or args.doi}\" --confirm \"你的确认依据\"")
        print("  然后再导入。人工确认会留痕，徽章显示为「人工确认」而非「已核验」。")
        return 2

    if vr.best_match is not None:
        best = vr.best_match
        rec = {
            "title": best.title, "authors": best.authors, "year": best.year,
            "venue": best.venue, "doi": best.doi, "url": best.url,
            "abstract": best.abstract, "origin": "verified_import",
        }
    else:
        # 人工确认放行：数据库没有命中记录，只能用工整的题录信息
        rec = {
            "title": args.query or vr.query_title,
            "doi": args.doi,
            "origin": "manual_confirmed_import",
        }
    pid, _ = import_record(ctx.kb, rec, verification=vr, tags=["核验导入"])
    ok(f"已导入 {pid}：{truncate(rec['title'], 60)}")
    print(f"  徽章：{badge(vr.level)}")
    return 0


def cmd_concepts(ctx: Ctx, args) -> int:
    title("专业术语")
    pid = None
    if args.paper:
        p = ctx.kb.resolve_paper(args.paper)
        if not p:
            err(f"未找到论文：{args.paper}")
            return 1
        pid = p["id"]
    concepts = ctx.kb.list_concepts(paper_id=pid, term=args.term, limit=args.limit)
    if not concepts:
        print("  （空）")
        return 0
    for c in concepts:
        p = ctx.kb.get_paper(c["paper_id"]) if c["paper_id"] else None
        src = truncate(p["title"], 40) if p else "-"
        print(f"  · {c['term']}  [{c.get('category') or '术语'}]  来源：{src}")
        print(f"      {c.get('definition')}")
    return 0


def cmd_relations(ctx: Ctx, args) -> int:
    title("跨论文关联分析")
    if args.paper:
        p = ctx.kb.resolve_paper(args.paper)
        if not p:
            err(f"未找到论文：{args.paper}")
            return 1
        rels = ctx.kb.list_relations(p["id"], limit=args.limit)
        print(f"  与「{truncate(p['title'], 50)}」相关的论文：")
        for r in rels:
            other = r["title_b"] if r["paper_a"] == p["id"] else r["title_a"]
            print(f"    · 相似度 {r['score']:.3f}  {truncate(other, 60)}")
            print(f"      {r['evidence']}")
        return 0

    rels = compute_relations(ctx.kb, threshold=args.threshold)
    print(f"  计算得到 {len(rels)} 对相似论文（阈值 {args.threshold}）：")
    for r in rels[: args.limit]:
        print(f"    · {r['score']:.3f}  {truncate(r['title_a'], 40)}  ⟷  {truncate(r['title_b'], 40)}")
        print(f"      {r['evidence']}")
    groups = cluster(ctx.kb, threshold=args.threshold)
    multi = [g for g in groups if len(g) > 1]
    if multi:
        print("\n  研究主题簇：")
        for i, g in enumerate(multi, 1):
            names = [truncate((ctx.kb.get_paper(x) or {}).get("title", x), 34) for x in g]
            print(f"    簇 {i}（{len(g)} 篇）：" + " ｜ ".join(names))
    return 0


def cmd_export(ctx: Ctx, args) -> int:
    title("导出解读笔记")
    pid = None
    if args.paper:
        p = ctx.kb.resolve_paper(args.paper)
        if not p:
            err(f"未找到论文：{args.paper}")
            return 1
        pid = p["id"]
    paths = export_notes(ctx.kb, ctx.cfg, pid)
    if not paths:
        warn("没有可导出的解读结果，请先运行 analyze")
        return 1
    for p in paths:
        print(f"  · {p}")
    ok(f"共导出 {len(paths)} 个文件到 {ctx.cfg.notes_path}")
    return 0


def cmd_stats(ctx: Ctx, args) -> int:
    title("知识库统计")
    st = ctx.kb.stats()
    for k, v in st.items():
        if isinstance(v, dict):
            kv(k, "、".join(f"{a}={b}" for a, b in v.items()) or "-")
        else:
            kv(k, v)
    return 0


def cmd_config(ctx: Ctx, args) -> int:
    title("基础配置与当前模型状态")
    shown = config_to_dict(ctx.cfg)
    for source in shown.get('sources',{}).values():
        if isinstance(source,dict) and source.get('api_key'):
            source['api_key'] = '***'
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    print()
    print(json.dumps(active_llm_status(ctx.kb,ctx.cfg), ensure_ascii=False, indent=2))
    return 0


def cmd_serve(ctx: Ctx, args) -> int:
    try:
        from .web.server import run_server
    except ImportError as e:  # pragma: no cover
        err(f"Web 界面依赖缺失：{e}；请执行 python -m pip install fastapi uvicorn")
        return 1
    print(f"  启动 Web 界面： http://{args.host}:{args.port}")
    run_server(ctx.cfg, host=args.host, port=args.port, reload=False)
    return 0


# ---------------------------------------------------------------------------
# 会话与上下文管理
# ---------------------------------------------------------------------------
def cmd_conv(ctx: Ctx, args) -> int:
    title("会话管理")
    if args.action == "new":
        cid = ctx.kb.add_conversation(args.title or "新会话")
        ok(f"已创建会话 {cid}")
        return 0
    if args.action == "say":
        conv = ctx.kb.resolve_conversation(args.conversation)
        if not conv:
            conv_id = ctx.kb.ensure_default_conversation()
        else:
            conv_id = conv["id"]
        m = ctx.kb.append_message(conv_id, args.role, args.text)
        ok(f"{conv_id}  [{m['ref']}] {m['role']}: {truncate(m['content'], 60)}")
        return 0

    rows = ctx.kb.list_conversations()
    if not rows:
        print("  （暂无会话）运行 `conv new` 创建")
        return 0
    for c in rows:
        anchor = c.get("compact_anchor_seq") or 0
        print(
            f"  [{c['id']}] {truncate(c['title'], 40)}"
            f"  消息 {c['message_count']}  锚点 m{anchor:05d}  更新 {c.get('updated_at', '')[:19]}"
        )
    return 0


def cmd_context(ctx: Ctx, args) -> int:
    from .context.telemetry import Telemetry, format_snapshot

    conv = ctx.kb.resolve_conversation(args.conversation)
    if not conv:
        err("未找到会话。用 `conv new` 创建，或 `conv say` 追加消息。")
        return 1
    tel = Telemetry(ctx.kb, ctx.cfg)
    snap = tel.snapshot(conv["id"])
    title(f"上下文状态 · {truncate(conv['title'], 40)}")
    print(format_snapshot(snap))
    return 0


def cmd_compact(ctx: Ctx, args) -> int:
    from .context.policy import Policy
    from .context.seam import Seam
    from .context.summarizer import ExtractiveSummarizer, Summarizer
    from .context.telemetry import Telemetry

    conv = ctx.kb.resolve_conversation(args.conversation)
    if not conv:
        err("未找到会话")
        return 1
    conv_id = conv["id"]

    meter = Telemetry(ctx.kb, ctx.cfg).meter
    policy = Policy(ctx.cfg)
    meas = meter.measure(ctx.kb, conv_id)
    messages = ctx.kb.get_messages(conv_id)

    zones = policy.protected_zones(meas, messages)
    decision = policy.decide_trigger(
        meas,
        anchor_seq=int(conv.get("compact_anchor_seq") or 0),
        messages=messages,
        protected=zones,
        force="manual" if (args.force or args.dry_run) else None,
    )

    title("上下文压缩")
    print(f"  {decision.describe()}")
    if not decision.should_compact:
        print("  （未触发。加 --force 可强制执行）")
        return 0

    sel = policy.select(meas, messages=messages, aggressive=decision.emergency_truncate)
    print(f"  {sel.describe()}")
    if not sel.ok:
        print(f"  跳过：{sel.noop_reason}")
        return 0
    print(f"  遮蔽：{', '.join(sel.shadowed_refs)}")

    if args.dry_run:
        print("\n  --dry-run：未写入任何数据。")
        return 0

    seam = Seam(ctx.kb, ctx.cfg)
    tx_id, code = seam.begin(conv_id, decision.trigger or "manual", sel)
    if not tx_id:
        err(f"无法开始压缩：{FAILURE_CODES.get(code or '', code)}")
        return 2

    in_range = [m for m in messages if sel.start_seq <= m["seq"] <= sel.end_seq]
    summarizer = Summarizer(ctx.cfg, ctx.llm, backend=ExtractiveSummarizer())
    print(f"\n  正在生成摘要（{len(in_range)} 条消息，{sel.shadowed_tokens} token）…")
    summary = summarizer.summarize(in_range, sel.shadowed_tokens)
    if not summary.ok:
        seam.fail(tx_id, "summary", summary.error)
        err(f"摘要失败：{summary.error}")
        return 2

    result = seam.commit(tx_id, summary)
    if not result.ok:
        err(f"提交失败 [{result.failure_code}]：{result.failure_text}")
        for v in result.violations:
            print(f"    {v}")
        return 2

    ok(f"{summary.describe()}")
    ok(f"压缩块 {result.block_id}，替换消息 {result.replacement_ref}")
    ledger = result.ledger
    print(
        f"  账本：省 {ledger.get('saved_per_turn', 0)} token/轮，"
        f"重付 {ledger.get('repriced_tokens', 0)} token"
    )
    print("\n  提示：`restore <block_id>` 可随时还原；`--to-file` 可写盘而非回填上下文。")
    return 0


def cmd_blocks(ctx: Ctx, args) -> int:
    from .context.restore import Restorer

    conv = ctx.kb.resolve_conversation(args.conversation)
    if not conv:
        err("未找到会话")
        return 1
    st = Restorer(ctx.kb, ctx.cfg).stats(conv["id"])
    title(f"压缩块 · {truncate(conv['title'], 40)}")
    if not st["items"]:
        print("  （暂无压缩块）")
        return 0
    print(f"  共 {st['blocks']} 块（活跃 {st['active_blocks']}）")
    print(f"  遮蔽合计 {st['shadowed_tokens']} token → 摘要合计 {st['summary_tokens']} token")
    print(f"  保留引用句柄 {st['citation_handles']} 个\n")
    for b in st["items"]:
        mark = "●" if b["active"] else "○"
        print(
            f"  {mark} {b['id']}  {b['range']}  {b['shadowed']}→{b['summary']} token"
            f"  句柄 {b['handles']}  {b['topic']}"
        )
    return 0


def cmd_restore(ctx: Ctx, args) -> int:
    from .context.restore import Restorer, format_restore

    r = Restorer(ctx.kb, ctx.cfg)
    if args.to_file:
        p = r.restore_to_file(args.block_id, args.to_file)
        ok(f"已还原到文件：{p}")
        print("  （还原到文件而非回填上下文 —— 既不炸窗口，也不作废前缀缓存）")
        return 0

    result = r.restore_block(args.block_id)
    title("还原压缩块")
    print(f"  {format_restore(result)}")
    if not result.ok:
        return 1
    if args.json:
        print(json.dumps(
            {
                "block_id": result.block_id,
                "messages": result.messages,
                "citations": result.citations,
                "missing_chunks": result.missing_chunks,
            },
            ensure_ascii=False, indent=2,
        ))
    else:
        print()
        print(r.render(result, include_citations=not args.no_citations))
    return 0


def cmd_memory(ctx: Ctx, args) -> int:
    from .memory.guards import format_result, validate_citations
    from .memory.store import KIND_LABELS, MemoryStore, format_memories

    store = MemoryStore(ctx.kb, ctx.cfg)

    if args.action == "add":
        if not args.text:
            err("请提供记忆内容，例如：memory add \"回答要简短\" --kind style_preference")
            return 1
        # 类型校验交给 store 的守卫，好处是错误信息能说清「为什么」而不只是「不在选项里」
        kind = args.kind if args.kind != "all" else "misc"
        receipt = store.save(
            args.text,
            kind=kind,
            key=args.key,
            pinned=not args.no_pin,
            priority=args.priority,
        )
        title("写入记忆")
        if receipt.action == "rejected":
            err(receipt.describe())
            return 2
        ok(receipt.describe())
        m = store.get(receipt.memory_id)
        if m:
            print(f"  {m.render()}")
        return 0

    if args.action == "remove":
        if not args.id:
            err("请提供记忆 id")
            return 1
        title("删除记忆")
        if store.delete(args.id):
            ok(f"已移入回收站 {args.id}（可用 memory restore 找回）")
            return 0
        err(f"未找到记忆 {args.id}")
        return 1

    if args.action == "restore":
        if not args.id:
            err("请提供记忆 id")
            return 1
        ok(f"已恢复 {args.id}") if store.restore(args.id) else err("未找到")
        return 0

    if args.action == "search":
        # 位置参数按顺序填 action → text → query，所以检索词可能落在 text 上
        keyword = args.query or args.text or ""
        title(f"检索记忆 · {keyword}")
        hits = store.search(keyword, limit=args.limit)
        print(format_memories(hits))
        return 0

    if args.action == "budget":
        from .memory.strategy import LayeredStrategy

        title("记忆预算")
        strat = LayeredStrategy(ctx.cfg)
        print(strat.budget_report(strat.assemble(store)))
        print()
        print("  常驻层准入标准：只有「几乎每轮都用得上」的才常驻。")
        print("  拿不准的一律放按需层 —— 宁可多一次检索，不要每轮白付 token。")
        return 0

    # 默认：列出
    title("记忆列表")
    items = store.list(kind=args.kind if args.kind != "all" else None, limit=args.limit)
    st = store.stats()
    print(f"  共 {st['total']} 条（常驻 {st['pinned']}）")
    if st["by_kind"]:
        print("  分布：" + "、".join(f"{KIND_LABELS.get(k, k)} {v}" for k, v in st["by_kind"].items()))
    print()
    print(format_memories(items))
    if not items:
        print("\n  提示：一期只支持**显式**记忆 —— 用 `memory add` 主动添加。")
        print("        不做隐式推断：隐式记忆容易记错，且用户不知道系统记了什么。")
    return 0


def cmd_turns(ctx: Ctx, args) -> int:
    from .memory.view import TurnViewStore, format_views

    conv = ctx.kb.resolve_conversation(args.conversation)
    if not conv:
        err("未找到会话。先用 `conv new` 创建。")
        return 1
    vs = TurnViewStore(ctx.kb)
    views = vs.list(conv["id"], limit=args.limit)
    title(f"不可变轮次视图 · {truncate(conv['title'], 40)}")
    print(format_views(views))
    if views:
        st = vs.stats(conv["id"])
        print()
        print(f"  合计 {st['turns']} 轮，常驻 {st['resident_tokens']} token，按需 {st['retrieved_tokens']} token")
        print("  说明：每轮检索到的记忆固化为不可变 View 并 pin 到该轮 ——")
        print("        换来一致性、可审计（本轮读了什么）、可复现（重放得到同样上下文）。")
    return 0


def cmd_skill(ctx: Ctx, args) -> int:
    from .skills.registry import SkillRegistry, format_entries
    from .skills.runner import SkillRunner, format_result
    from .skills.validate import format_result as format_validation

    reg = SkillRegistry(ctx.cfg)

    if args.action == "dir":
        d = reg.ensure_dir()
        title("Skill 目录")
        print(f"  {d}")
        print()
        print("  每个 Skill 是一个子目录，内含：")
        print("    skill.yaml   清单（元数据 / 输入契约 / 上下文声明 / 能力白名单 / 输出形态）")
        print("    prompt.md    提示词模板，可用 {{变量}} 引用 inputs 中声明的输入")
        print()
        print("  ⚠️ 一期只支持**声明式** Skill，不支持自定义代码 ——")
        print("     允许携带可执行代码等于开放任意代码执行入口。")
        return 0

    if args.action == "show":
        if not args.skill_id:
            err("请提供 skill id")
            return 1
        reg.discover()
        entry = reg.get(args.skill_id)
        if entry is None:
            err(f"未找到 Skill {args.skill_id!r}")
            return 1
        title(f"Skill · {entry.name}")
        kv("id", entry.id)
        kv("版本", entry.version)
        kv("状态", entry.status)
        kv("说明", entry.manifest.description or "-")
        kv("路径", entry.manifest.path)
        kv("触发前缀", entry.manifest.prefix or "-")
        kv("关键词", "、".join(entry.manifest.keywords) or "-")
        kv("能力白名单", "、".join(entry.manifest.capabilities) or "（空）")
        kv("输出形态", f"{entry.manifest.output_type}"
           + (f" / {entry.manifest.deliverable_type}" if entry.manifest.output_type == "deliverable" else ""))
        kv("要求引用", "是" if entry.manifest.citation_required else "否")
        if entry.manifest.inputs:
            print("\n  输入契约：")
            for i in entry.manifest.inputs:
                req = "必填" if i.required else "可选"
                print(f"    · {i.key:<14} {i.label or i.key:<14} {i.type:<10} {req}")
        if entry.manifest.context:
            print("\n  上下文预取：")
            for c in entry.manifest.context:
                print(f"    · {c.capability}  {c.args or ''}")
        print()
        print(format_validation(entry.validation))
        return 0

    if args.action == "validate":
        title("校验 Skill")
        entries = reg.reload()
        if args.skill_id:
            entries = [e for e in entries if e.id == args.skill_id]
            if not entries:
                err(f"未找到 Skill {args.skill_id!r}")
                return 1
        if not entries:
            print("  （没有可校验的 Skill）")
            return 0
        bad = 0
        for e in entries:
            print(format_validation(e.validation))
            if not e.validation.ok:
                bad += 1
            print()
        ok(f"{len(entries) - bad} / {len(entries)} 通过校验")
        return 0 if bad == 0 else 2

    if args.action == "run":
        if not args.skill_id:
            err("请提供 skill id")
            return 1
        inputs: dict = {}
        for pair in args.input or []:
            if "=" not in pair:
                err(f"--input 需要 key=value 形式，收到 {pair!r}")
                return 1
            k, v = pair.split("=", 1)
            inputs[k.strip()] = v.strip()

        reg.discover()
        entry = reg.get(args.skill_id)
        if entry is None:
            err(f"未找到 Skill {args.skill_id!r}")
            return 1
        title(f"执行 Skill · {entry.name}")
        runner = SkillRunner(ctx.kb, ctx.cfg, llm=None if args.dry_run else ctx.llm, registry=reg)
        result = runner.run(args.skill_id, inputs, dry_run=args.dry_run)
        print(format_result(result))
        if result.answer and not args.dry_run:
            print()
            print(result.answer)
        return 0 if result.ok else 2

    # 默认：列表
    title("Skill 列表")
    entries = reg.discover()
    st = reg.stats()
    if st["empty"]:
        print("  " + reg.empty_state_text().replace("\n", "\n  "))
        return 0
    print(f"  共 {st['total']} 个（可用 {st['usable']}，异常 {st['failed']}）")
    print()
    print(format_entries(entries))
    return 0


def cmd_deliverables(ctx: Ctx, args) -> int:
    from .deliverables import TYPE_LABELS, DeliverableStore, format_deliverables

    store = DeliverableStore(ctx.kb)

    if args.action == "show":
        d = store.resolve(args.ref or "")
        if d is None:
            err("未找到交付物")
            return 1
        title(f"交付物 · {d.title}")
        kv("id", d.id)
        kv("类型", d.type_label)
        kv("状态", d.status_label)
        kv("Skill", d.skill_id or "（内核能力生成）")
        kv("引用", f"{d.citation_count} 条")
        kv("字数", str(d.word_count))
        kv("更新时间", d.updated_at[:19])
        print()
        print(d.content.strip() or "（空）")
        if d.citations:
            print()
            print("引用来源：")
            print(d.citation_summary())
        return 0

    if args.action == "export":
        d = store.resolve(args.ref or "")
        if d is None:
            err("未找到交付物")
            return 1
        if args.all:
            out_dir = Path(args.output or ctx.cfg.data_path / "notes" / "deliverables")
            paths = store.export_all(out_dir, fmt=args.format)
            ok(f"已导出 {len(paths)} 个交付物到 {out_dir}")
            return 0
        out = Path(args.output) if args.output else (
            Path(ctx.cfg.data_path) / "notes" / "deliverables" / f"{d.id}.{args.format}"
        )
        p = store.export(d.id, out, fmt=args.format)
        ok(f"已导出到 {p}")
        if d.citations:
            print(f"  含 {d.citation_count} 条引用来源")
        return 0

    if args.action == "delete":
        d = store.resolve(args.ref or "")
        if d is None:
            err("未找到交付物")
            return 1
        ok(f"已移入回收站 {d.id}") if store.delete(d.id) else err("删除失败")
        return 0

    if args.action == "status":
        d = store.resolve(args.ref or "")
        if d is None:
            err("未找到交付物")
            return 1
        if not args.to:
            err("请用 --to 指定新状态（draft / final / exported）")
            return 1
        store.update(d.id, status=args.to)
        ok(f"{d.id} 状态 → {args.to}")
        return 0

    title("交付物")
    items = store.list(type=args.type, status=args.status, limit=args.limit)
    st = store.stats()
    print(f"  共 {st['total']} 个")
    if st["by_type"]:
        print("  按类型：" + "、".join(f"{TYPE_LABELS.get(k, k)} {v}" for k, v in st["by_type"].items()))
    if st["by_status"]:
        print("  按状态：" + "、".join(f"{k} {v}" for k, v in st["by_status"].items()))
    print()
    print(format_deliverables(items))
    if not items:
        print("\n  交付物由 Skill 的 output.type=deliverable 生成，")
        print("  或由综述/对比等内核能力产出。导出时一并带出引用来源。")
    return 0


def cmd_mcp(ctx: Ctx, args) -> int:
    from .mcp.tools import describe_tools, dump_schema

    if args.action == "serve":
        from .mcp.server import serve
        # stdout belongs exclusively to the MCP transport. Ctx owns this DB.
        serve(kb=ctx.kb,cfg=ctx.cfg)
        return 0

    if args.action == "tools":
        title("MCP 工具与资源")
        print(describe_tools())
        print()
        kv("当前模式", "只读" if ctx.cfg.mcp.readonly else "读写")
        if ctx.cfg.mcp.readonly:
            print("  提示：写入工具在只读模式下不对外暴露。")
        return 0

    if args.action == "schema":
        print(json.dumps(dump_schema(), ensure_ascii=False, indent=2))
        return 0

    if args.action == "audit":
        from .mcp.audit import AuditLog, format_audit

        log = AuditLog(ctx.kb, ctx.cfg.mcp.server_id)
        title("MCP 调用审计")
        st = log.stats()
        print(f"  共 {st['total']} 次（成功 {st['ok']}，失败 {st['failed']}）")
        if st["by_tool"]:
            print("  按工具：" + "、".join(f"{k} {v}" for k, v in st["by_tool"].items()))
        print()
        print(format_audit(log.list(limit=args.limit)))
        return 0

    if args.action == "audit-clear":
        from .mcp.audit import AuditLog

        n = AuditLog(ctx.kb, ctx.cfg.mcp.server_id).clear()
        ok(f"已清空 {n} 条审计记录")
        return 0

    title("MCP Server")
    kv("状态", "已启用" if ctx.cfg.mcp.enabled else "未启用（不影响手动 serve）")
    kv("模式", "只读" if ctx.cfg.mcp.readonly else "读写")
    kv("server_id", ctx.cfg.mcp.server_id)
    print()
    print("  一期只做**对外**的 MCP Server，不接入外部 MCP 服务。")
    print("  常用命令：")
    print("    mcp tools    查看工具清单")
    print("    mcp serve    启动 stdio 服务（阻塞）")
    print("    mcp audit    查看调用记录")
    return 0


def cmd_rules(ctx: Ctx, args) -> int:
    from .invariants import describe_rules

    title("压缩不变量")
    for code, text in describe_rules():
        print(f"  [{code}] {text}")
    print()
    print("  失败码闭集：")
    for code, msg in FAILURE_CODES.items():
        print(f"    {code:<15} {msg}")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="litprism",
        description="文献棱镜 LitPrism —— 论文知识解读 Agent（结构化解读 · 真实性核验 · 问答 · 对比 · 推荐）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"LitPrism {__version__}")
    p.add_argument("--config", help="指定配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化并查看状态").set_defaults(func=cmd_init)

    a = sub.add_parser("add", help="导入 PDF/HTML/DOCX/TXT/MD/TeX")
    a.add_argument("paths", nargs="+", help="文件或目录")
    a.add_argument("--tags", default="", help="逗号分隔标签")
    a.add_argument("--verify", action="store_true", help="导入时做真实性核验")
    a.add_argument("--no-enrich", action="store_true", help="不做题录补全（保留 PDF 解析出的元数据）")
    a.add_argument("--no-analyze", action="store_true", help="跳过后台结构化解读")
    a.set_defaults(func=cmd_add)

    a = sub.add_parser("list", help="列出论文")
    a.add_argument("--status", choices=["verified", "unverified", "not_found"], help="按核验状态过滤")
    a.add_argument("--query", help="标题/摘要关键字")
    a.add_argument("--limit", type=int, default=200)
    a.set_defaults(func=cmd_list)

    a = sub.add_parser("show", help="查看某篇论文的结构化解读")
    a.add_argument("ref", help="论文 id / id 前缀 / DOI / 标题片段")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("analyze", help="生成结构化解读")
    a.add_argument("ref", nargs="?", help="论文 id / 标题片段")
    a.add_argument("--all", action="store_true")
    a.add_argument("--force", action="store_true", help="忽略缓存重新解读")
    a.set_defaults(func=cmd_analyze)

    a = sub.add_parser("search", help="检索本地知识库（可选外部数据库）")
    a.add_argument("query")
    a.add_argument("--external", action="store_true", help="同时检索 Crossref/OpenAlex/arXiv")
    a.add_argument("--limit", type=int, default=8)
    a.set_defaults(func=cmd_search)

    a = sub.add_parser("ask", help="针对论文内容提问")
    a.add_argument("question")
    a.add_argument("-p", "--paper", action="append", help="限定论文（可重复）")
    a.add_argument("-k", "--top-k", type=int, default=None, help="召回片段数")
    a.add_argument("--show-evidence", action="store_true", help="打印引用片段全文")
    a.set_defaults(func=cmd_ask)

    a = sub.add_parser("compare", help="多篇论文对比")
    a.add_argument("refs", nargs="+", help="至少两篇：id / 标题片段")
    a.add_argument("--aspects", nargs="*", help="重点对比维度")
    a.set_defaults(func=cmd_compare)

    a = sub.add_parser("recommend", help="推荐文献（全部经真实性核验）")
    a.add_argument("--topic", help="研究方向关键词")
    a.add_argument("--paper", help="种子论文（基于它找相关工作/引用）")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--strategies", help="逗号分隔：openalex_related,citing,keyword,local")
    a.add_argument("--import", dest="import_", action="store_true", help="把核验通过的推荐项导入本地库")
    a.add_argument("--no-import", action="store_true")
    a.add_argument("--no-explain", action="store_true", help="不生成推荐理由（省 token）")
    a.set_defaults(func=cmd_recommend)

    a = sub.add_parser("verify", help="核验一篇论文是否真实存在")
    a.add_argument("query", nargs="?", help="论文标题")
    a.add_argument("--doi", help="按 DOI 核验（最可靠）")
    a.add_argument("--auto-doi", action="store_true", help="从标题参数里自动抽取 DOI")
    a.add_argument(
        "--confirm",
        metavar="依据",
        help="核验未通过时人工确认放行。依据必填，会留痕并保留原判定",
    )
    a.add_argument("--json", action="store_true", help="输出完整 JSON")
    a.set_defaults(func=cmd_verify)

    a = sub.add_parser("whitelist", help="人工确认白名单（记录在案的例外）")
    a.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    a.add_argument("title", nargs="?", help="论文标题（action=add）")
    a.add_argument("--reason", help="确认依据（必填）：为什么认为这篇文献真实存在")
    a.add_argument("--doi")
    a.add_argument("--id", help="白名单条目 id（action=remove）")
    a.add_argument("--level", default="not_found", choices=list(LEVELS),
                   help="确认时的自动核验判定（默认 not_found）")
    a.add_argument("--limit", type=int, default=100)
    a.set_defaults(func=cmd_whitelist)

    a = sub.add_parser("import", help="核验外部文献并导入本地知识库")
    a.add_argument("query", nargs="?", help="标题")
    a.add_argument("--doi")
    a.set_defaults(func=cmd_import)

    a = sub.add_parser("concepts", help="查看专业术语表")
    a.add_argument("--paper")
    a.add_argument("--term")
    a.add_argument("--limit", type=int, default=200)
    a.set_defaults(func=cmd_concepts)

    a = sub.add_parser("relations", help="跨论文关联分析")
    a.add_argument("--paper", help="只看某篇论文的关联")
    a.add_argument("--threshold", type=float, default=0.18)
    a.add_argument("--limit", type=int, default=30)
    a.set_defaults(func=cmd_relations)

    a = sub.add_parser("export", help="导出 Markdown 解读笔记")
    a.add_argument("--paper")
    a.set_defaults(func=cmd_export)

    sub.add_parser("stats", help="知识库统计").set_defaults(func=cmd_stats)
    sub.add_parser("config", help="显示生效配置").set_defaults(func=cmd_config)

    # ---- 会话与上下文管理 ----
    a = sub.add_parser("conv", help="会话管理")
    a.add_argument("action", nargs="?", default="list", choices=["list", "new", "say"])
    a.add_argument("--title", help="新会话标题（action=new）")
    a.add_argument("--conversation", help="会话 id / 标题片段（action=say）")
    a.add_argument("--role", default="user", choices=["user", "assistant", "system"])
    a.add_argument("--text", default="", help="消息内容（action=say）")
    a.set_defaults(func=cmd_conv)

    a = sub.add_parser("context", help="查看上下文状态（三层投影 + 压缩账本）")
    a.add_argument("conversation", nargs="?", help="会话 id / 标题片段，留空取最近一个")
    a.set_defaults(func=cmd_context)

    a = sub.add_parser("compact", help="压缩上下文（在新增量上动手）")
    a.add_argument("conversation", nargs="?", help="会话 id / 标题片段")
    a.add_argument("--dry-run", action="store_true", help="只报告会压哪段，不写任何数据")
    a.add_argument("--force", action="store_true", help="绕过增长门控强制执行")
    a.set_defaults(func=cmd_compact)

    a = sub.add_parser("blocks", help="列出压缩块")
    a.add_argument("conversation", nargs="?", help="会话 id / 标题片段")
    a.set_defaults(func=cmd_blocks)

    a = sub.add_parser("restore", help="还原压缩块（正文可折叠，句柄永不折叠）")
    a.add_argument("block_id")
    a.add_argument("--to-file", help="写盘而非回填上下文")
    a.add_argument("--no-citations", action="store_true", help="不附引用原文")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_restore)

    sub.add_parser("rules", help="列出压缩不变量与失败码").set_defaults(func=cmd_rules)

    # ---- 记忆系统 ----
    a = sub.add_parser("memory", help="记忆管理（一期只支持显式记忆）")
    a.add_argument("action", nargs="?", default="list",
                   choices=["list", "add", "remove", "restore", "search", "budget"])
    a.add_argument("text", nargs="?", help="记忆内容（action=add）")
    a.add_argument("query", nargs="?", help="检索词（action=search）")
    a.add_argument("--id", help="记忆 id（action=remove/restore）")
    a.add_argument(
        "--kind", default="all",
        help="记忆类型：research_interest / term_preference / style_preference / misc / all",
    )
    a.add_argument("--key", help="可选键，如术语原名")
    a.add_argument("--no-pin", action="store_true", help="不常驻（默认常驻）")
    a.add_argument("--priority", type=int, default=100, help="常驻层裁减优先级（小者先保）")
    a.add_argument("--limit", type=int, default=100)
    a.set_defaults(func=cmd_memory)

    a = sub.add_parser("turns", help="查看不可变轮次视图（turn memory bar）")
    a.add_argument("conversation", nargs="?", help="会话 id / 标题片段")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(func=cmd_turns)

    # ---- Skill 框架 ----
    a = sub.add_parser("skill", help="Skill 管理（一期只建框架，不内置任何 Skill）")
    a.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "validate", "run", "dir"])
    a.add_argument("skill_id", nargs="?", help="Skill id")
    a.add_argument("--input", action="append", metavar="key=value",
                   help="执行输入，可重复；如 --input topic=注意力机制")
    a.add_argument("--dry-run", action="store_true", help="只组装提示词，不调用模型")
    a.set_defaults(func=cmd_skill)

    # ---- 交付物 ----
    a = sub.add_parser("deliverables", help="交付物管理（综述 / 对比 / 笔记 / 审稿意见）")
    a.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "export", "delete", "status"])
    a.add_argument("ref", nargs="?", help="交付物 id / 前缀 / 标题片段，留空取最近一个")
    a.add_argument("--type", choices=["review", "comparison", "notes", "peer_review"])
    a.add_argument("--status", choices=["draft", "final", "exported"])
    a.add_argument("--to", help="action=status 时的目标状态")
    a.add_argument("--output", help="导出路径（文件或目录）")
    a.add_argument("--format", default="md", choices=["md", "json"])
    a.add_argument("--all", action="store_true", help="导出全部")
    a.add_argument("--limit", type=int, default=100)
    a.set_defaults(func=cmd_deliverables)

    # ---- MCP ----
    a = sub.add_parser("mcp", help="MCP Server（对外暴露，默认只读）")
    a.add_argument("action", nargs="?", default="status",
                   choices=["status", "tools", "serve", "schema", "audit", "audit-clear"])
    a.add_argument("--limit", type=int, default=30)
    a.set_defaults(func=cmd_mcp)

    a = sub.add_parser("serve", help="启动 Web 界面")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8765)
    a.set_defaults(func=cmd_serve)

    a = sub.add_parser('backup', help='整库备份、校验、恢复与目录迁移（不包含密钥）')
    actions = a.add_subparsers(dest='action', required=True)
    b = actions.add_parser('create', help='导出当前库的一致快照')
    b.add_argument('--output', required=True)
    b.add_argument('--include-originals', action='store_true', help='包含托管 papers 目录中的原文文件')
    b = actions.add_parser('inspect', help='只读校验备份，不打开当前库')
    b.add_argument('archive')
    b = actions.add_parser('restore', help='恢复到不存在的新目录，不自动切换')
    b.add_argument('archive')
    b.add_argument('--target', required=True)
    b = actions.add_parser('migrate', help='将当前库与托管文件复制到新目录，保留原目录')
    b.add_argument('--target', required=True)
    b = actions.add_parser('activate', help='退出应用后执行：切换到已恢复目录及非密钥设置')
    b.add_argument('--target', required=True)
    a.set_defaults(func=cmd_backup)

    return p


def cmd_backup(ctx, args) -> int:
    from .backup import activate_directory, create_backup, inspect_backup, migrate_data, restore_backup
    cfg = ctx.cfg if ctx else None
    if args.action in ('create', 'migrate'):
        if args.action == 'create':
            result = create_backup(ctx.kb, cfg, args.output, include_originals=args.include_originals)
        else:
            result = migrate_data(ctx.kb, cfg, args.target)
    elif args.action == 'inspect':
        result = inspect_backup(args.archive)
    elif args.action == 'restore':
        result = restore_backup(args.archive, args.target)
    else:
        cfg = load_config(getattr(args, 'config', None))
        switched = activate_directory(cfg, args.target)
        result = {'data_dir': switched['data_dir'], 'requires_restart': True,
                  'note': '配置已切换；请重启应用，密钥需要重新配置。原目录保留。'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Inspection/restoration must not instantiate Ctx or migrate the current user DB.
    if args.cmd == 'backup' and args.action in ('inspect', 'restore', 'activate'):
        try:
            return cmd_backup(None, args)
        except (ValueError, OSError) as exc:
            err(str(exc))
            return 1
    try:
        ctx = Ctx(getattr(args, "config", None))
    except Exception as e:  # noqa: BLE001
        err(f"加载配置失败：{e}")
        return 1
    try:
        return args.func(ctx, args)
    except KeyboardInterrupt:
        print("\n  已中断",file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        err(f"{type(e).__name__}: {e}")
        if __debug__ and getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        return 1
    finally:
        ctx.kb.close()


if __name__ == "__main__":
    raise SystemExit(main())
