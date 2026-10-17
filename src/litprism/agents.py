"""三个交互 Agent：

* :class:`QAAgent`        —— 基于本地知识库的带引用问答（含虚构引用检测）
* :class:`CompareAgent`   —— 多篇论文对比分析
* :class:`RecommendAgent` —— 依据研究方向推荐文献（推荐项全部经真实性核验，可选导入本地库）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config
from .db import KnowledgeBase
from .prompts import COMPARE_SYSTEM, QA_SYSTEM, RECOMMEND_SYSTEM
from .search import citing_works, related_works, search_external, search_local
from .textutil import parse_citations, title_similarity, truncate
from .verify import PaperVerifier
from .whitelist import badge, is_citable


# ===========================================================================
# 问答
# ===========================================================================
@dataclass
class QAAnswer:
    question: str
    answer: str = ""
    confidence: str = "unknown"
    insufficient_evidence: bool = False
    open_questions: list[str] = field(default_factory=list)
    evidence_map: list[dict] = field(default_factory=list)
    excerpts: list[dict] = field(default_factory=list)
    citation_issues: list[str] = field(default_factory=list)
    citation_check_passed: bool = True
    model: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "confidence": self.confidence,
            "insufficient_evidence": self.insufficient_evidence,
            "open_questions": self.open_questions,
            "evidence_map": self.evidence_map,
            "excerpts": [
                {
                    "tag": e["tag"], "chunk_id": e.get("chunk_id"),
                    "paper_id": e["paper_id"], "title": e["title"],
                    "page": e["page"], "section": e["section"], "score": e["score"],
                    "text": truncate(e["text"], 700),
                }
                for e in self.excerpts
            ],
            "citation_issues": self.citation_issues,
            "citation_check_passed": self.citation_check_passed,
            "model": self.model,
            "warnings": self.warnings,
        }


class QAAgent:
    """检索增强问答。三道防幻觉闸门：

    1. 只把检索到的原文片段交给模型，提示词禁止使用片段外知识；
    2. 无召回时直接拒答（``refuse_when_no_evidence``）；
    3. 回答产出后，逐条校验 ``[P# p.##]`` 引用是否指向真实片段（``strict_citation_check``）。
    """

    def __init__(self, kb: KnowledgeBase, cfg: Config, llm):
        self.kb = kb
        self.cfg = cfg
        self.llm = llm

    def ask(
        self,
        question: str,
        *,
        paper_ids: list[str] | None = None,
        top_k: int | None = None,
    ) -> QAAnswer:
        top_k = top_k or self.cfg.retrieval.top_k
        hits = search_local(self.kb, question, top_k=top_k, paper_ids=paper_ids, llm=self.llm)
        res = QAAnswer(question=question, model=getattr(self.llm, "name", "unknown"))

        if not hits:
            res.insufficient_evidence = True
            res.confidence = "none"
            res.answer = (
                "本地知识库中没有检索到与该问题相关的原文片段。\n\n"
                "可能原因：① 尚未导入相关论文（可用 `add` 导入 PDF，或 `recommend` 检索并导入）；"
                "② 问题用词与论文表述差异较大，可换用论文中的术语再问。"
            )
            return res

        excerpts = []
        for i, h in enumerate(hits, 1):
            excerpts.append(
                {
                    "tag": f"P{i}",
                    "chunk_id": h.get("id"),
                    "paper_id": h["paper_id"],
                    "title": h.get("paper_title") or "",
                    "year": h.get("paper_year"),
                    "page": h.get("page"),
                    "section": h.get("section") or "",
                    "score": h.get("score"),
                    "text": h["text"],
                }
            )
        res.excerpts = excerpts

        blocks = []
        for e in excerpts:
            head = f"[{e['tag']}] 论文：《{e['title']}》"
            if e.get("year"):
                head += f"（{e['year']}）"
            loc = []
            if e.get("page"):
                loc.append(f"第 {e['page']} 页")
            if e.get("section"):
                loc.append(f"章节：{e['section']}")
            if loc:
                head += "  " + " · ".join(loc)
            blocks.append(f"{head}\n{e['text']}")
        material = "\n\n---\n\n".join(blocks)

        scope = "本地知识库全部论文"
        if paper_ids:
            titles = [self.kb.get_paper(p) for p in paper_ids]
            scope = "指定论文：" + "、".join(truncate(t["title"], 40) for t in titles if t)

        messages = [
            {"role": "system", "content": QA_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"检索范围：{scope}\n\n"
                    f"【片段】\n{material}\n\n"
                    f"【问题】{question}\n\n"
                    "请仅依据上述片段作答，并在每条结论后标注来源（如 [P1 p.3]）。"
                ),
            },
        ]

        try:
            data = self.llm.chat_json(messages, task="qa")
        except Exception as e:  # noqa: BLE001
            res.answer = f"（模型调用失败：{e}）"
            res.confidence = "error"
            res.warnings.append(str(e))
            return res

        res.answer = str(data.get("answer") or "").strip()
        res.confidence = str(data.get("confidence") or "unknown")
        res.insufficient_evidence = bool(data.get("insufficient_evidence"))
        res.open_questions = list(data.get("open_questions") or [])
        res.evidence_map = list(data.get("evidence_map") or [])

        if self.cfg.qa.strict_citation_check:
            self._check_citations(res)
        return res

    def _check_citations(self, res: QAAnswer) -> None:
        """校验回答中的引用标记是否都能对应到真实片段。"""
        valid = {e["tag"]: e for e in res.excerpts}
        cited = parse_citations(res.answer)
        if not cited:
            if not res.insufficient_evidence and len(res.answer) > 40:
                res.citation_issues.append("回答中没有任何来源标记，无法追溯依据。")
                res.citation_check_passed = False
            return
        for tag, page in cited:
            if tag not in valid:
                res.citation_issues.append(f"引用了不存在的片段 [{tag}]（提供的片段只有 {'、'.join(valid)}）")
                res.citation_check_passed = False
                continue
            real_page = valid[tag].get("page")
            if page and real_page and str(page).strip() != str(real_page).strip():
                res.citation_issues.append(
                    f"[{tag}] 标注页码为 {page}，但该片段实际位于第 {real_page} 页"
                )
                res.citation_check_passed = False
        if not res.citation_check_passed:
            res.warnings.append(
                "⚠️ 引用校验未通过：回答中可能存在无法追溯或虚构的来源标记，请以原文片段为准。"
            )


# ===========================================================================
# 对比
# ===========================================================================
class CompareAgent:
    def __init__(self, kb: KnowledgeBase, cfg: Config, llm):
        self.kb = kb
        self.cfg = cfg
        self.llm = llm

    def compare(self, refs: list[str], *, aspects: list[str] | None = None) -> dict:
        papers = []
        missing = []
        for r in refs:
            p = self.kb.resolve_paper(r)
            if p:
                papers.append(p)
            else:
                missing.append(r)
        if len(papers) < 2:
            raise ValueError(f"需要至少 2 篇可识别的论文，当前识别到 {len(papers)} 篇；未找到：{missing}")

        blocks = []
        labels = {}
        for i, p in enumerate(papers, 1):
            tag = f"P{i}"
            labels[tag] = p
            analysis = self.kb.get_analysis(p["id"])
            payload = (analysis or {}).get("payload") or {}
            lines = [f"[{tag}] 《{p['title']}》" + (f"（{p['year']}）" if p.get("year") else "")]
            lines.append(f"核验状态：{p.get('verify_level') or p.get('verify_status')}")
            for key, label in (
                ("research_question", "研究问题"), ("methodology", "方法论"),
                ("experiment_design", "实验设计"), ("key_conclusions", "关键结论"),
                ("innovations", "创新点"), ("limitations", "局限"),
            ):
                val = payload.get(key)
                lines.append(f"{label}：{val if val else '（未提取，需先运行 analyze）'}")
            if not payload:
                # 没有结构化解读时退回摘要
                lines.append(f"摘要：{truncate(p.get('abstract') or '（无摘要，建议先运行 analyze）', 1200)}")
            blocks.append("\n".join(lines))

        focus = ""
        if aspects:
            focus = "\n请特别关注以下维度：" + "、".join(aspects) + "\n"

        messages = [
            {"role": "system", "content": COMPARE_SYSTEM},
            {"role": "user", "content": "请对比以下论文：\n\n" + "\n\n---\n\n".join(blocks) + focus},
        ]
        data = self.llm.chat_json(messages, task="compare")
        data["_papers"] = [{"tag": f"P{i}", "id": p["id"], "title": p["title"]} for i, p in enumerate(papers, 1)]
        if missing:
            data["_missing"] = missing
        return data


def render_comparison(data: dict) -> str:
    papers = data.get("_papers") or []
    lines = ["# 论文对比分析", ""]
    if data.get("summary"):
        lines += ["## 总体结论", str(data["summary"]), ""]

    dims = data.get("dimensions") or []
    if dims:
        tags = [p["tag"] for p in papers] or sorted(
            {k for d in dims for k in (d.get("values") or {})}
        )
        header = "| 维度 | " + " | ".join(tags) + " |"
        sep = "|" + "---|" * (len(tags) + 1)
        lines += [header, sep]
        for d in dims:
            vals = d.get("values") or {}
            row = [str(d.get("dimension", ""))]
            for t in tags:
                row.append(str(vals.get(t, "未提及")).replace("\n", " "))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    for key, title in (("convergence", "一致点"), ("divergence", "分歧点")):
        items = data.get(key) or []
        if items:
            lines.append(f"## {title}")
            lines += [f"- {x}" for x in items]
            lines.append("")

    em = data.get("evidence_map") or []
    if em:
        lines.append("## 结论与来源")
        for e in em:
            lines.append(f"- {e.get('claim')} —— 来源：{', '.join(e.get('sources') or [])}")
        lines.append("")

    if papers:
        lines.append("## 对比对象")
        for p in papers:
            lines.append(f"- [{p['tag']}] {p['title']}（`{p['id']}`）")
    if data.get("_missing"):
        lines.append("")
        lines.append(f"> 未识别的输入：{data['_missing']}")
    return "\n".join(lines)


# ===========================================================================
# 推荐
# ===========================================================================
class RecommendAgent:
    """文献推荐。**所有推荐项都来自权威数据库检索结果或本地已核验文献**，
    模型只负责写推荐理由，不允许新增候选。"""

    def __init__(self, kb: KnowledgeBase, cfg: Config, llm, verifier: PaperVerifier):
        self.kb = kb
        self.cfg = cfg
        self.llm = llm
        self.verifier = verifier

    def recommend(
        self,
        *,
        topic: str | None = None,
        paper_ref: str | None = None,
        limit: int | None = None,
        strategies: list[str] | None = None,
        explain: bool = True,
        import_to_kb: bool | None = None,
    ) -> dict:
        limit = limit or self.cfg.recommend.default_limit
        strategies = strategies or self.cfg.recommend.strategies
        seed = None
        query = topic or ""

        if paper_ref:
            seed = self.kb.resolve_paper(paper_ref)
            if not seed:
                raise ValueError(f"未找到论文：{paper_ref}")
            query = query or seed["title"]

        if not query:
            raise ValueError("请提供研究方向（topic）或一篇种子论文（paper_ref）")

        known = {p["id"] for p in self.kb.list_papers(limit=2000)}
        known_keys = set()
        for p in self.kb.list_papers(limit=2000):
            from .textutil import normalize_doi, normalize_title
            known_keys.add(normalize_doi(p.get("doi")) or normalize_title(p["title"]))

        candidates: list[dict] = []
        used: list[str] = []

        if seed and "openalex_related" in strategies:
            rel = related_works(self.cfg, self.verifier, seed, limit=limit)
            for r in rel:
                r["origin"] = "openalex_related"
                r["verification"] = {
                    "level": "verified", "exists": True, "confidence": 1.0,
                    "basis": "OpenAlex related_works 返回的真实记录", "sources": ["openalex"],
                }
            candidates += rel
            used.append("openalex_related")

        if seed and "citing" in strategies:
            cit = citing_works(self.cfg, self.verifier, seed, limit=limit)
            for r in cit:
                r["origin"] = "openalex_citing"
                r["verification"] = {
                    "level": "verified", "exists": True, "confidence": 1.0,
                    "basis": "OpenAlex cites: 过滤返回的真实记录", "sources": ["openalex"],
                }
            candidates += cit
            used.append("citing")

        if "keyword" in strategies:
            ext = search_external(self.cfg, self.verifier, query, limit=limit)
            candidates += ext
            used.append("keyword")

        if "local" in strategies:
            local = search_local(self.kb, query, top_k=limit * 2, llm=self.llm)
            seen_local = set()
            for h in local:
                pid = h["paper_id"]
                if pid in seen_local:
                    continue
                seen_local.add(pid)
                p = self.kb.get_paper(pid)
                if not p:
                    continue
                candidates.append(
                    {
                        "title": p["title"], "authors": p["authors"], "year": p.get("year"),
                        "venue": p.get("venue"), "doi": p.get("doi"), "url": p.get("url"),
                        "abstract": p.get("abstract"), "citation_count": None,
                        "sources": ["local"], "origin": "local_library",
                        "verification": {
                            "level": p.get("verify_level") or "unchecked",
                            # 人工确认的文献同样算「存在」，但 level 保留 manual_confirmed，
                            # 让调用方能区分「机器核到的」与「人确认的」
                            "exists": is_citable(p.get("verify_level")),
                            "confidence": p.get("verify_confidence") or 0.0,
                            "basis": "本地知识库中的文献", "sources": ["local"],
                        },
                        "_paper_id": pid,
                    }
                )
            used.append("local")

        # 去重 + 排除已在库中的
        from .textutil import normalize_doi, normalize_title
        merged: dict[str, dict] = {}
        for c in candidates:
            key = normalize_doi(c.get("doi")) or normalize_title(c.get("title", ""))
            if not key:
                continue
            if key in merged:
                merged[key]["sources"] = sorted(set(merged[key]["sources"]) | set(c.get("sources", [])))
                continue
            merged[key] = c

        fresh = []
        for key, c in merged.items():
            if key in known_keys and c.get("origin") != "local_library":
                c["already_in_library"] = True
            fresh.append(c)

        fresh.sort(
            key=lambda x: (
                -(1 if x.get("verification", {}).get("level") == "verified" else 0),
                -(x.get("citation_count") or 0),
                -(x.get("year") or 0),
            )
        )
        fresh = fresh[:limit]

        # 只让模型写理由，候选列表本身不可被模型改动
        if explain and fresh:
            brief = "\n".join(
                f"{i}. 《{c['title']}》" + (f"（{c.get('year')}）" if c.get("year") else "")
                + (f" 期刊：{c['venue']}" if c.get("venue") else "")
                + (f" 摘要：{truncate(c.get('abstract') or '', 400)}" if c.get("abstract") else "（无摘要）")
                for i, c in enumerate(fresh, 1)
            )
            messages = [
                {"role": "system", "content": RECOMMEND_SYSTEM},
                {"role": "user", "content": f"用户研究方向：{query}\n\n候选文献：\n{brief}"},
            ]
            try:
                data = self.llm.chat_json(messages, task="recommend")
                for it in data.get("items") or []:
                    idx = int(it.get("index") or 0) - 1
                    if 0 <= idx < len(fresh):
                        fresh[idx]["relevance"] = it.get("relevance")
                        fresh[idx]["reason"] = it.get("reason")
                        fresh[idx]["suggested_reading"] = it.get("suggested_reading")
                note = data.get("note", "")
            except Exception as e:  # noqa: BLE001
                note = f"（推荐理由生成失败：{e}；候选文献列表本身来自数据库检索，未受影响）"
        else:
            note = ""

        imported = []
        do_import = self.cfg.recommend.auto_import if import_to_kb is None else import_to_kb
        if do_import:
            from .search import import_record
            for c in fresh:
                if c.get("_paper_id"):
                    continue
                if not is_citable((c.get("verification") or {}).get("level")):
                    continue
                try:
                    pid, _ = import_record(self.kb, c, tags=["推荐导入", f"seed:{query[:40]}"])
                    c["_paper_id"] = pid
                    imported.append({"title": c["title"], "paper_id": pid})
                except Exception as e:  # noqa: BLE001
                    c["import_error"] = str(e)

        return {
            "query": query,
            "seed": {"id": seed["id"], "title": seed["title"]} if seed else None,
            "strategies": used,
            "count": len(fresh),
            "items": fresh,
            "note": note,
            "imported": imported,
            "disclaimer": (
                "以上候选均来自 Crossref / OpenAlex / arXiv 等权威数据库的实时检索结果，"
                "或本地知识库中已核验的文献；模型仅生成推荐理由，未新增任何条目。"
                "检索结果反映数据库收录情况，不代表该文献质量或与您方向的绝对匹配度。"
            ),
        }


def render_recommendation(data: dict) -> str:
    lines = [f"# 文献推荐 · {data.get('query', '')}", ""]
    if data.get("seed"):
        lines.append(f"种子论文：{data['seed']['title']}")
    lines.append(f"检索策略：{'、'.join(data.get('strategies') or [])}    候选数：{data.get('count')}")
    lines.append("")
    for i, c in enumerate(data.get("items") or [], 1):
        v = c.get("verification") or {}
        mark = badge(v.get("level"))
        lines.append(f"### {i}. {c['title']}")
        meta = []
        if c.get("authors"):
            meta.append("、".join(c["authors"][:4]) + (" 等" if len(c["authors"]) > 4 else ""))
        if c.get("year"):
            meta.append(str(c["year"]))
        if c.get("venue"):
            meta.append(c["venue"])
        meta.append(mark)
        if c.get("citation_count") is not None:
            meta.append(f"被引 {c['citation_count']}")
        if c.get("already_in_library"):
            meta.append("已在本地库")
        lines.append("> " + " · ".join(str(m) for m in meta))
        if c.get("doi"):
            lines.append(f"> DOI: {c['doi']}")
        if c.get("reason"):
            lines.append(f"- **相关性**（{c.get('relevance') or '-'}）：{c['reason']}")
        if c.get("suggested_reading"):
            lines.append(f"- **建议重点**：{c['suggested_reading']}")
        if c.get("url"):
            lines.append(f"- 链接：{c['url']}")
        lines.append("")
    if data.get("imported"):
        lines.append(f"已导入本地知识库 {len(data['imported'])} 篇：" + "、".join(
            truncate(x["title"], 40) for x in data["imported"]
        ))
        lines.append("")
    if data.get("note"):
        lines.append(f"> {data['note']}")
        lines.append("")
    if data.get("disclaimer"):
        lines.append("---")
        lines.append(f"> {data['disclaimer']}")
    return "\n".join(lines)
