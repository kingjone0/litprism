"""结构化解读：把论文正文交给大模型，产出研究问题 / 方法论 / 实验设计 / 关键结论 / 创新点 / 局限，
并提取专业术语。所有字段都要求"仅依据原文"，缺失即留空并说明。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .db import KnowledgeBase
from .prompts import CONCEPTS_SYSTEM, EXTRACT_SYSTEM
from .textutil import truncate
from .whitelist import label

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
    for i, c in enumerate(chunks, 1):
        head = f"[P{i}] chunk_id={c['id']} " + (f"p.{c['page']}" if c['page'] is not None else '无页码')
        by_sec.setdefault(c.get("section") or "正文", []).append(head+'\n'+c["text"])

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
    from .evidence import citation_issues, require_citable
    require_citable(paper.get('verify_level'))
    if not force:
        cached = kb.get_analysis(paper_id)
        if cached and cached.get("payload"):
            return cached["payload"]

    ctx = paper_context(kb, paper)
    if len(ctx) < 200:
        raise ValueError(f"论文 {paper_id} 正文过少，无法解读（可能是扫描版 PDF，需先 OCR）")

    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM + '\n每个非空解读字段必须在 field_evidence 中提供来源标记列表，例如 {"methodology":["P1 p.3"]}。只用本次给定的 P 编号和页码；没有依据的字段留空。'},
        {"role": "user", "content": f"请解析下面这篇论文：\n\n{ctx}"},
    ]
    from .context.meter import Meter
    if sum(Meter(cfg).estimate_message(m) for m in messages) > cfg.context.available_tokens:
        raise ValueError('解读原文超过上下文预算，请调整窗口或分块。')
    data = llm.chat_json(messages, task="extract")
    chunks = kb.get_chunks(paper_id)
    excerpts = [dict(c, tag=f'P{i}') for i, c in enumerate(chunks, 1)
                if f"chunk_id={c['id']} " in ctx]
    issues = []
    evidence = data.get('field_evidence') or {}
    if not isinstance(evidence, dict):
        evidence = {}
    for field, _ in _FIELDS:
        raw = data.get(field)
        if raw is not None and not isinstance(raw, str):
            data[field] = ''
            issues.append(f'{field}: 字段必须是文本。')
            continue
        value = (raw or '').strip()
        data[field] = value
        if not value:
            continue
        refs = evidence.get(field) or []
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            refs = []
        if data.get('_mock'):
            refs = [e['tag'] + (f" p.{e['page']}" if e['page'] is not None else '')
                    for e in excerpts if any(part.strip()[:50] in e['text'] for part in value.split('\n') if len(part.strip()) >= 15)]
        cited = value + '\n' + ' '.join('['+str(r).strip('[]')+']' for r in refs)
        errors = citation_issues(cited, excerpts)
        if errors:
            data[field] = ''
            issues.extend(f'{field}: {error}' for error in errors)
        else:
            evidence[field] = refs
    data['field_evidence'] = evidence
    data['_citation_check'] = {'passed': not issues, 'issues': issues}
    data['_citations'] = [{'tag': e['tag'], 'chunk_id': e['id'], 'paper_id': paper_id, 'page': e['page']} for e in excerpts]
    keywords = data.get('keywords')
    data['keywords'] = [v for v in keywords if isinstance(v, str)] if isinstance(keywords, list) else []
    # 术语只能来自本次独立校验，不能信任 extract 顺带返回的 concepts。
    data['concepts'] = []
    data["_model"] = getattr(llm, "name", "unknown")

    kb.save_analysis(paper_id, data, model=getattr(llm, "name", "unknown"))
    with kb.tx() as con:
        con.execute('DELETE FROM concepts WHERE paper_id = ?', (paper_id,))

    if with_concepts:
        try:
            cm = [
                {"role": "system", "content": CONCEPTS_SYSTEM + '\n每条 definition 必须带本次原文的 [P# p.N] 引用；无页码不写页码。没有原文依据时不要生成该术语。'},
                {"role": "user", "content": f"论文标题：{paper['title']}\n\n{ctx}"},
            ]
            cdata = llm.chat_json(cm, task="concepts")
            concepts = [c for c in cdata.get('concepts') or []
                        if isinstance(c, dict) and isinstance(c.get('term'), str)
                        and isinstance(c.get('definition'), str)
                        and not citation_issues(c['definition'], excerpts)]
            if concepts:
                kb.add_concepts(paper_id, concepts)
                data["concepts"] = concepts
        except Exception as e:  # noqa: BLE001
            data["_concepts_error"] = f"{type(e).__name__}: {e}"

    kb.save_analysis(paper_id, data, model=getattr(llm, 'name', 'unknown'))
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

    for key, field_label in _FIELDS:
        val = (analysis.get(key) or "").strip()
        lines.append(f"## {field_label}")
        lines.append(val if val else "（原文未明确给出，已留空）")
        refs = (analysis.get('field_evidence') or {}).get(key) or []
        if refs:
            lines.append('依据：' + ' '.join('['+str(r).strip('[]')+']' for r in refs))
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
    if (analysis.get('_citation_check') or {}).get('passed') is False:
        lines.append('> 引用校验未全部通过，缺少依据的字段已留空。')
    return "\n".join(lines)


def _verify_badge(paper: dict) -> str:
    level = paper.get("verify_level") or paper.get("verify_status")
    conf = paper.get("verify_confidence") or 0
    if level == "verified":
        return f"✅ 已核验（置信度 {conf:.2f}）"
    if level == "manual_confirmed":
        # 人工确认与自动核验**必须区分** —— 前者是人的判断，后者是机器的结论
        return (
            f"🔵 人工确认放行（确认时自动判定为「{label(paper.get('original_level') or '')}」）。"
            "这是记录在案的例外，不是自动核验通过。"
        )
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
        from .deliverables import Deliverable, DeliverableStore
        if not (analysis.get('_citation_check') or {}).get('passed'):
            raise ValueError('解读未通过引用校验，请重新解读后导出。')
        DeliverableStore(kb).validate_export(Deliverable(id='', type='notes', title=p['title'],
            content=format_analysis(p, analysis), citations=analysis.get('_citations') or []))
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
