"""MCP 工具与资源的纯实现。

**不依赖 MCP SDK** —— 只依赖内核库。这样 SDK 不可用时内核逻辑仍可测试。

设计原则
--------
1. 每个工具只做一件事，返回值是**可直接序列化**的 dict；
2. 错误不抛异常，返回 ``{"ok": false, "error": "..."}`` —— MCP 客户端需要可读的错误；
3. 只读工具绝不产生副作用；唯一的写工具需显式开启。
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..db import KnowledgeBase
from ..whitelist import is_citable

# ---------------------------------------------------------------------------
# 工具清单（供文档与自检使用）
# ---------------------------------------------------------------------------
TOOLS: dict[str, dict] = {
    "search_papers": {
        "description": "按关键词检索本地知识库中的论文题录。返回标题、作者、年份、核验状态。",
        "readonly": True,
    },
    "get_paper": {
        "description": "取单篇论文的完整信息：题录、核验状态、结构化解读、术语。",
        "readonly": True,
    },
    "search_passages": {
        "description": "检索论文原文片段（带页码与章节），用于精确引用。",
        "readonly": True,
    },
    "verify_paper": {
        "description": "核验一篇论文是否真实存在（Crossref/OpenAlex/arXiv 多源交叉验证）。",
        "readonly": True,
    },
    "list_collections": {
        "description": "列出知识库的分组树与每组所含论文。",
        "readonly": True,
    },
    "get_graph": {
        "description": "取跨论文关联图谱（节点 + 边），用于发现主题簇。",
        "readonly": True,
    },
    "compare_papers": {
        "description": "多篇论文的六维度对比矩阵（研究问题/方法/实验/结论/创新点/局限）。",
        "readonly": True,
    },
    "ask": {
        "description": "基于知识库回答问题，每条结论带原文引用并做回溯校验。",
        "readonly": True,
    },
    "import_paper": {
        "description": "导入一篇外部文献。**强制走核验流程**，未核验的一律拒绝。",
        "readonly": False,
    },
}

RESOURCES: dict[str, dict] = {
    "litprism://library": {"description": "知识库概览：统计、论文清单、分组结构。"},
    "litprism://paper/{id}": {"description": "单篇论文的完整解读（Markdown）。"},
}


def _err(msg: str, **extra: Any) -> dict:
    return {"ok": False, "error": msg, **extra}


def _ok(**payload: Any) -> dict:
    return {"ok": True, **payload}


class LitPrismTools:
    """工具的纯实现。"""

    def __init__(self, kb: KnowledgeBase, cfg: Config, *, llm: Any = None, readonly: bool = True):
        self.kb = kb
        self.cfg = cfg
        self.llm = llm
        self.readonly = readonly

    # ------------------------------------------------------------------
    # 只读工具
    # ------------------------------------------------------------------
    def search_papers(self, query: str = "", limit: int = 10, *, verified_only: bool = False) -> dict:
        try:
            papers = self.kb.list_papers()
            q = (query or "").strip().lower()
            if q:
                papers = [
                    p for p in papers
                    if q in (p.get("title") or "").lower()
                    or q in (p.get("display_title") or "").lower()
                    or any(q in (a or "").lower() for a in (p.get("authors") or []))
                ]
            if verified_only:
                papers = [p for p in papers if is_citable(p.get("verify_level"))]
            items = [_paper_brief(p) for p in papers[: max(1, int(limit))]]
            return _ok(query=query, count=len(items), items=items)
        except Exception as e:  # noqa: BLE001
            return _err(f"检索失败：{type(e).__name__}: {e}")

    def get_paper(self, paper_id: str = "", title: str = "") -> dict:
        try:
            paper = self.kb.get_paper(paper_id) if paper_id else None
            if paper is None and title:
                for p in self.kb.list_papers():
                    if title.lower() in (p.get("title") or "").lower():
                        paper = p
                        break
            if paper is None:
                return _err("未找到该论文。可先用 search_papers 查找 id。")

            pid = paper["id"]
            analysis = self.kb.get_analysis(pid)
            concepts = [
                dict(r) for r in self.kb._con.execute(
                    "SELECT term, definition, category FROM concepts WHERE paper_id = ?", (pid,)
                ).fetchall()
            ]
            return _ok(
                paper=_paper_brief(paper),
                analysis=_analysis_brief(analysis),
                concepts=concepts,
                full_text_available=bool(paper.get("full_text")),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"读取失败：{type(e).__name__}: {e}")

    def search_passages(self, query: str = "", top_k: int = 8, paper_id: str = "") -> dict:
        try:
            from ..search import search_local

            if not (query or "").strip():
                return _err("query 不能为空")
            hits = search_local(self.kb, query, top_k=max(1, int(top_k)), paper_ids=[paper_id] if paper_id else None, citable_only=True)
            if paper_id:
                hits = [h for h in hits if h.get("paper_id") == paper_id]
            items = [
                {
                    "chunk_id": h.get("id"),
                    "paper_id": h.get("paper_id"),
                    "paper_title": h.get("paper_title"),
                    "page": h.get("page"),
                    "section": h.get("section"),
                    "text": h.get("text"),
                    "score": h.get("score"),
                }
                for h in hits
            ]
            return _ok(query=query, count=len(items), items=items)
        except Exception as e:  # noqa: BLE001
            return _err(f"片段检索失败：{type(e).__name__}: {e}")

    def verify_paper(self, title: str = "", doi: str = "") -> dict:
        try:
            from ..verify import PaperVerifier
            from ..whitelist import Whitelist

            if not (title or doi):
                return _err("title 与 doi 至少提供一个")
            v = PaperVerifier(self.cfg, Whitelist(self.kb))
            r = v.verify(title=title, doi=doi or None)
            return _ok(verification=r.to_dict(), message=r.message, citable=is_citable(r.level))
        except Exception as e:  # noqa: BLE001
            return _err(f"核验失败：{type(e).__name__}: {e}")

    def list_collections(self) -> dict:
        try:
            rows = self.kb._con.execute(
                "SELECT id, name, parent_id, sort_order FROM collections"
                " WHERE deleted_at IS NULL ORDER BY sort_order, name"
            ).fetchall()
            groups: dict[str | None, list[dict]] = {}
            for r in rows:
                d = dict(r)
                groups.setdefault(d.get("parent_id"), []).append(d)
            papers = [
                {"id": p["id"], "title": p.get("display_title") or p.get("title"),
                 "collection_id": p.get("collection_id")}
                for p in self.kb.list_papers()
            ]
            return _ok(
                collections=[dict(r) for r in rows],
                tree=groups.get(None, []),
                papers=papers,
                ungrouped=sum(1 for p in papers if not p.get("collection_id")),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"读取分组失败：{type(e).__name__}: {e}")

    def get_graph(self, threshold: float = 0.25) -> dict:
        try:
            from ..relations import compute_relations

            rels = compute_relations(self.kb, threshold=float(threshold), persist=False)
            papers = {p["id"]: (p.get("display_title") or p.get("title")) for p in self.kb.list_papers()}
            nodes = [{"id": pid, "title": t} for pid, t in papers.items()]
            edges = [
                {
                    "source": r.get("paper_a"), "target": r.get("paper_b"),
                    "type": r.get("rel_type"), "score": r.get("score"),
                }
                for r in rels
            ]
            return _ok(nodes=nodes, edges=edges, node_count=len(nodes), edge_count=len(edges))
        except Exception as e:  # noqa: BLE001
            return _err(f"图谱计算失败：{type(e).__name__}: {e}")

    def compare_papers(self, paper_ids: list[str] | None = None) -> dict:
        try:
            ids = [str(x) for x in (paper_ids or []) if str(x).strip()]
            if len(ids) < 2:
                return _err("至少需要两篇论文的 id")
            rows = []
            for pid in ids:
                p = self.kb.get_paper(pid)
                if p is None:
                    return _err(f"未找到论文 {pid}")
                a = self.kb.get_analysis(pid) or {}
                rows.append(
                    {
                        "paper_id": pid,
                        "title": p.get("title"),
                        "verify_level": p.get("verify_level"),
                        "research_question": a.get("research_question"),
                        "methodology": a.get("methodology"),
                        "experiment_design": a.get("experiment_design"),
                        "key_conclusions": a.get("key_conclusions"),
                        "innovations": a.get("innovations"),
                        "limitations": a.get("limitations"),
                    }
                )
            return _ok(dimensions=["研究问题", "方法", "实验", "结论", "创新点", "局限"], papers=rows)
        except Exception as e:  # noqa: BLE001
            return _err(f"对比失败：{type(e).__name__}: {e}")

    def ask(self, question: str = "", top_k: int = 8) -> dict:
        try:
            if not (question or "").strip():
                return _err("question 不能为空")
            if self.llm is None:
                return _err("未配置模型，无法回答。请先在配置里设置 llm。")
            from ..agents import QAAgent

            ans = QAAgent(self.kb, self.cfg, self.llm).ask(question, top_k=max(1, int(top_k)))
            return _ok(**ans.to_dict(), citations=[{
                'tag': e['tag'], 'chunk_id': e.get('chunk_id'), 'paper_id': e['paper_id'], 'page': e['page'],
            } for e in ans.excerpts], citation_check={'passed': ans.citation_check_passed, 'issues': ans.citation_issues})
        except Exception as e:  # noqa: BLE001
            return _err(f"回答失败：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # 写工具（需显式开启）
    # ------------------------------------------------------------------
    def import_paper(self, title: str = "", doi: str = "", confirm_reason: str = "") -> dict:
        try:
            if self.readonly:
                return _err(
                    "当前 MCP 服务为只读模式，拒绝写入。"
                    "如需写入，请在配置中显式设置 mcp.readonly = false。"
                )
            if not (title or doi):
                return _err("title 与 doi 至少提供一个")

            from ..search import import_record
            from ..verify import PaperVerifier
            from ..whitelist import Whitelist

            wl = Whitelist(self.kb)
            if confirm_reason:
                # 走人工确认放行路径
                vr = PaperVerifier(self.cfg, wl).verify(title=title, doi=doi or None)
                if not is_citable(vr.level):
                    wl.add(
                        title=title or doi,
                        reason=confirm_reason,
                        doi=doi or None,
                        level_at_confirm=vr.level,
                        verify_evidence=vr.to_dict(),
                        confirmed_by="mcp_client",
                    )
                    vr = PaperVerifier(self.cfg, wl).verify(title=title, doi=doi or None)

            vr = PaperVerifier(self.cfg, wl).verify(title=title, doi=doi or None)
            if not is_citable(vr.level):
                return _err(
                    f"拒绝导入：该文献未通过核验（{vr.level}）。"
                    "MCP 写入同样受核验约束 —— 不接受无法确认真实性的文献。",
                    verification=vr.to_dict(),
                )

            best = vr.best_match
            rec = {
                "title": best.title if best else title,
                "authors": best.authors if best else [],
                "year": best.year if best else None,
                "venue": best.venue if best else None,
                "doi": (best.doi if best else doi) or None,
                "url": best.url if best else None,
                "abstract": best.abstract if best else None,
                "origin": "mcp_import",
            }
            pid, created = import_record(self.kb, rec, verification=vr, tags=["MCP 导入"])
            return _ok(paper_id=pid, created=created, verify_level=vr.level, title=rec["title"])
        except Exception as e:  # noqa: BLE001
            return _err(f"导入失败：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def resource_library(self) -> str:
        st = self.kb.stats()
        papers = self.kb.list_papers()
        lines = ["# 文献棱镜 · 知识库概览", ""]
        lines.append(f"- 论文 {st['papers']} 篇 / 分块 {st['chunks']} 个 / 已解读 {st['analyses']} 篇")
        lines.append(f"- 术语 {st['concepts']} 条 / 关联 {st['relations']} 条")
        verified = sum(1 for p in papers if p.get("verify_level") == "verified")
        manual = sum(1 for p in papers if p.get("verify_level") == "manual_confirmed")
        lines.append(f"- 已核验 {verified} 篇 / 人工确认 {manual} 篇")
        lines += ["", "## 论文清单", ""]
        for p in papers:
            from ..whitelist import badge

            lines.append(
                f"- `{p['id']}` {p.get('display_title') or p.get('title')}"
                f"（{p.get('year') or '年份未知'}）{badge(p.get('verify_level'))}"
            )
        return "\n".join(lines)

    def resource_paper(self, paper_id: str) -> str:
        p = self.kb.get_paper(paper_id)
        if p is None:
            return f"# 未找到论文 {paper_id}"
        from ..whitelist import badge

        a = self.kb.get_analysis(paper_id) or {}
        lines = [
            f"# {p.get('title')}",
            "",
            f"- ID：`{paper_id}`",
            f"- 作者：{', '.join(p.get('authors') or []) or '未知'}",
            f"- 年份：{p.get('year') or '未知'}",
            f"- 核验：{badge(p.get('verify_level'))}",
        ]
        if p.get("doi"):
            lines.append(f"- DOI：{p['doi']}")
        for key, label in (
            ("research_question", "研究问题"),
            ("methodology", "方法"),
            ("experiment_design", "实验设计"),
            ("key_conclusions", "主要结论"),
            ("innovations", "创新点"),
            ("limitations", "局限"),
        ):
            if a.get(key):
                lines += ["", f"## {label}", "", str(a[key])]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def _paper_brief(p: dict) -> dict:
    from ..whitelist import badge

    return {
        "id": p["id"],
        "title": p.get("title"),
        "display_title": p.get("display_title"),
        "authors": p.get("authors") or [],
        "year": p.get("year"),
        "venue": p.get("venue"),
        "doi": p.get("doi"),
        "verify_level": p.get("verify_level"),
        "verify_badge": badge(p.get("verify_level")),
        "citable": is_citable(p.get("verify_level")),
    }


def _analysis_brief(a: dict | None) -> dict | None:
    if not a:
        return None
    return {
        k: a.get(k)
        for k in (
            "research_question", "methodology", "experiment_design",
            "key_conclusions", "innovations", "limitations", "field",
        )
    }


def describe_tools() -> str:
    lines = ["MCP 工具（8 个只读 + 1 个写入）", ""]
    for name, meta in TOOLS.items():
        flag = "读" if meta["readonly"] else "写（需显式开启）"
        lines.append(f"  {name:<18} [{flag}]  {meta['description']}")
    lines += ["", "MCP 资源", ""]
    for name, meta in RESOURCES.items():
        lines.append(f"  {name:<28} {meta['description']}")
    return "\n".join(lines)


def dump_schema() -> dict:
    """导出工具与资源清单，供客户端自检。"""
    return {
        "tools": [{"name": k, **v} for k, v in TOOLS.items()],
        "resources": [{"uri": k, **v} for k, v in RESOURCES.items()],
    }


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
