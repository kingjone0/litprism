"""结构化解读：把论文正文交给大模型，产出研究问题 / 方法论 / 实验设计 / 关键结论 / 创新点 / 局限，
并提取专业术语。所有字段都要求"仅依据原文"，缺失即留空并说明。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .config import Config
from .db import KnowledgeBase
from .llm import LLMError
from .prompts import CONCEPTS_SYSTEM, EXTRACT_SYSTEM
from .textutil import truncate

# 优先纳入上下文的章节（按重要性排序）
_PRIORITY_SECTIONS = ["摘要", "引言", "相关工作", "方法", "实验", "结果", "消融实验", "讨论", "结论", "局限", "未来工作", "正文"]

_FIELDS = [
    ("research_question", "研究问题"),
    ("methodology", "方法论"),
    ("experiment_design", "实验设计"),
    ("key_conclusions", "关键结论"),
    ("innovations", "创新点"),
    ("limitations", "局限与未来工作"),
]


def paper_context(kb: KnowledgeBase, paper: dict, budget: int = 24000) -> str:
    """为 LLM 拼装论文上下文：按章节重要性取原文，超预算时按比例截断。"""
    chunks = kb.get_chunks(paper["id"])
    by_sec: dict[str, list[str]] = {}
    for c in chunks:
        by_sec.setdefault(c.get("section") or "正文", []).append(c["text"])

    parts: list[str] = [f"【标题】{paper['title']}"]
    if paper.get("authors"):
        parts.append(f"【作者】{', '.join(paper['authors'][:10])}")
    if paper.get("year"):
        parts.append(f"【年份】{paper['year']}")
    if paper.get("abstract"):
        parts.append(f"【摘要】{paper['abstract']}")

    ordered = [s for s in _PRIORITY_SECTIONS if s in by_sec] + [s for s in by_sec if s not in _PRIORITY_SECTIONS]
    body: list[str] = []
    for sec in ordered:
        text = "\n".join(by_sec[sec])
        body.append(f"\n### {sec}\n{text}")
    blob = "\n".join(parts) + "\n" + "\n".join(body)
    return truncate(blob, budget, ellipsis="\n…（正文过长，已截断）")


def analyze_paper(
    kb: KnowledgeBase,
    cfg: Config,
    llm,
    paper_id: str,
    *,
    force: bool = False,
    with_concepts: bool = True,
) -> dict:
    """对单篇论文做结构化解读，写入 analyses / concepts 表。"""
    paper = kb.get_paper(paper_id)
    if not paper:
        raise ValueError(f"未找到论文：{paper_id}")
    if not force:
        cached = kb.get_analysis(paper_id)
        if cached and cached.get("payload"):
            return cached["payload"]

    ctx = paper_context(kb, paper)
    if len(ctx) < 200:
        raise ValueError(f"论文 {paper_id} 正文过少，无法解读（可能是扫描版 PDF，需先 OCR）")

    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": f"请解析下面这篇论文：\n\n{ctx}"},
    ]
    data = llm.chat_json(messages, task="extract")
    data.setdefault("keywords", [])
    data["_model"] = getattr(llm, "name", "unknown")

    kb.save_analysis(paper_id, data, model=getattr(llm, "name", "unknown"))

    if with_concepts:
        try:
            cm = [
                {"role": "system", "content": CONCEPTS_SYSTEM},
                {"role": "user", "content": f"论文标题：{paper['title']}\n\n{ctx}"},
            ]
            cdata = llm.chat_json(cm, task="concepts")
            concepts = cdata.get("concepts") or []
            if concepts:
                # 覆盖式更新：先清旧
                with kb.tx() as con:
                    con.execute("DELETE FROM concepts WHERE paper_id = ?", (paper_id,))
                kb.add_concepts(paper_id, concepts)
                data["concepts"] = concepts
        except Exception as e:  # noqa: BLE001
            data["_concepts_error"] = f"{type(e).__name__}: {e}"

    return data


def analyze_all(
    kb: KnowledgeBase,
    cfg: Config,
    llm,
    *,
    force: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[tuple[str, str]]:
    papers = kb.list_papers(limit=1000)
    results: list[tuple[str, str]] = []
    for i, p in enumerate(papers, 1):
        if progress:
            progress(i, len(papers), truncate(p["title"], 50))
        if not force and kb.get_analysis(p["id"]):
            results.append((p["id"], "skipped"))
            continue
        try:
            analyze_paper(kb, cfg, llm, p["id"], force=force)
            results.append((p["id"], "ok"))
        except Exception as e:  # noqa: BLE001
            results.append((p["id"], f"error: {e}"))
    return results


# ---------------------------------------------------------------------------
# 呈现与导出
# ---------------------------------------------------------------------------
def format_analysis(paper: dict, analysis: dict, *, include_concepts: bool = True) -> str:
    lines: list[str] = []
    lines.append(f"# {paper['title']}")
    meta = []
    if paper.get("authors"):
        meta.append("、".join(paper["authors"][:6]))
    if paper.get("year"):
        meta.append(str(paper["year"]))
    if paper.get("venue"):
        meta.append(paper["venue"])
    if paper.get("doi"):
        meta.append(f"DOI: {paper['doi']}")
    if meta:
        lines.append("> " + " · ".join(meta))
    lines.append("")
    lines.append(f"**核验状态**：{_verify_badge(paper)}")
    lines.append("")

    for key, label in _FIELDS:
        val = (analysis.get(key) or "").strip()
        lines.append(f"## {label}")
        lines.append(val if val else "（原文未明确给出，已留空）")
        lines.append("")

    kw = analysis.get("keywords") or []
    if kw:
        lines.append("## 关键词")
        lines.append("、".join(kw))
        lines.append("")

    if include_concepts and analysis.get("concepts"):
        lines.append("## 专业术语")
        for c in analysis["concepts"]:
            lines.append(f"- **{c.get('term')}**（{c.get('category') or '术语'}）：{c.get('definition')}")
        lines.append("")

    notes = analysis.get("field_notes")
    if notes:
        lines.append("## 提取说明")
        lines.append(str(notes))
        lines.append("")

    if analysis.get("_mock"):
        lines.append("---")
        lines.append("> ⚠️ 本次解读由**离线占位模型（mock）**按关键词启发式生成，"
                     "仅用于演示流程，不构成对该论文的学术解读。配置真实模型后请用 `analyze --force` 重跑。")
    return "\n".join(lines)


def _verify_badge(paper: dict) -> str:
    level = paper.get("verify_level") or paper.get("verify_status")
    conf = paper.get("verify_confidence") or 0
    if level == "verified":
        return f"✅ 已核验（置信度 {conf:.2f}）"
    if level == "likely":
        return f"⚠️ 疑似匹配，需人工确认（相似度 {conf:.2f}）"
    if level == "not_found":
        return "❌ 未能核到：权威数据库中无匹配记录，**引用前必须人工复核**"
    return "❔ 未核验（本地导入的文献默认未核验，可用 `verify` 命令核验）"


def export_notes(kb: KnowledgeBase, cfg: Config, paper_id: str | None = None) -> list[Path]:
    """把结构化解读导出为 Markdown 笔记。"""
    cfg.ensure_dirs()
    papers = [kb.get_paper(paper_id)] if paper_id else kb.list_papers(limit=1000)
    written: list[Path] = []
    for p in papers:
        if not p:
            continue
        analysis = kb.get_analysis(p["id"])
        if not analysis:
            continue
        analysis = dict(analysis.get("payload") or analysis)
        analysis["concepts"] = kb.list_concepts(p["id"])
        md = format_analysis(p, analysis)
        safe = "".join(ch if ch.isalnum() or ch in " -_（）()" else "_" for ch in p["title"])[:80].strip()
        path = cfg.notes_path / f"{safe or p['id']}.md"
        path.write_text(md, encoding="utf-8")
        written.append(path)
        if "json" in cfg.export.formats:
            jpath = path.with_suffix(".json")
            jpath.write_text(
                json.dumps({"paper": p, "analysis": analysis}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            written.append(jpath)
    return written
