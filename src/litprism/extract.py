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


def paper_context(kb: KnowledgeBase, paper: dict, budget: int | None = None) -> str:
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
    return blob if budget is None else truncate(blob, budget, ellipsis="\n…（正文过长，已截断）")


def analyze_paper(kb: KnowledgeBase, cfg: Config, llm, paper_id: str, *,
                  force: bool = False, with_concepts: bool = True) -> dict:
    from .evidence import citation_issues, require_citable
    from .reading import excerpt_text, reading_batches
    paper = kb.get_paper(paper_id)
    if paper is None:
        raise ValueError(f'未找到论文：{paper_id}')
    require_citable(paper.get('verify_level'))
    if not force:
        cached = kb.get_analysis(paper_id)
        if cached and cached.get('payload'):
            return cached['payload']
    chunks = kb.get_chunks(paper_id)
    if sum(len(c['text']) for c in chunks) < 150:
        raise ValueError('正文过少，无法解读；扫描件需先转写。')
    extract_system = EXTRACT_SYSTEM + '\n每个非空字段必须在 field_evidence 提供原文标记列表；无依据留空。'
    extract_system += '\n每个独立句子就地附其直接依据，不把多个分号连接的结论统一在段末附一串引用；每句最多 3 个直接来源，复杂内容分成多句。'
    concept_system = CONCEPTS_SYSTEM + '\n每条 definition 必须含本批原文引用 [P# p.N]；无页码不写页码，无依据不生成。'
    # 较长的指令作为分批预算基准，确保两类请求都容纳得下。
    system = max([extract_system, concept_system], key=len)
    instruction = '仅分析本批原文。未读取部分不能称为原文未提及。'
    batches, all_excerpts, coverage = reading_batches(kb, cfg, [paper], system, instruction)
    results, all_issues, all_concepts, concept_errors, semantic_inputs, batch_notes = [], [], [], [], [], []
    from .tasks import checkpoint
    for index, excerpts in enumerate(batches, 1):
        checkpoint(f'解读原文第 {index}/{len(batches)} 批')
        ctx = instruction+'\n\n'+excerpt_text(excerpts)
        data = llm.chat_json([{'role': 'system', 'content': extract_system},
                              {'role': 'user', 'content': ctx}], task='extract')
        if not isinstance(data, dict):
            raise ValueError('解读必须返回 JSON 对象。')
        data, issues = _validate_fields(data, excerpts)
        all_issues.extend(issues)
        results.append(data)
        semantic_inputs.append(([(field, data.get(field) or '', (data.get('field_evidence') or {}).get(field) or [])
                                 for field, _ in _FIELDS], excerpts))
        if isinstance(data.get('field_notes'), str) and data['field_notes'].strip():
            batch_notes.append({'batch': index, 'text': data['field_notes'], 'status': 'unreviewed_model_note'})
        if with_concepts:
            try:
                cdata = llm.chat_json([{'role': 'system', 'content': concept_system},
                                       {'role': 'user', 'content': ctx}], task='concepts')
                concepts = cdata.get('concepts') or []
                if not isinstance(concepts, list):
                    raise ValueError('concepts 必须是数组。')
                all_concepts.extend(c for c in concepts if isinstance(c, dict)
                                    and isinstance(c.get('term'), str) and isinstance(c.get('definition'), str)
                                    and not citation_issues(c['definition'], excerpts))
            except Exception as exc:
                concept_errors.append(f'{type(exc).__name__}: {exc}')
    merged = dict(results[0])
    evidence = {}
    for field, _ in _FIELDS:
        parts = []
        for result in results:
            value = result.get(field) or ''
            refs = (result.get('field_evidence') or {}).get(field) or []
            if value:
                # 不做二次生成；每段保留本批字段已检查的依据。
                part = value+'\n依据：'+' '.join('['+str(r).strip('[]')+']' for r in refs)
                if part not in parts:
                    parts.append(part)
                evidence.setdefault(field, []).extend(refs)
        merged[field] = '\n\n'.join(parts)
    merged['field_evidence'] = {k: list(dict.fromkeys(v)) for k, v in evidence.items()}
    from .semantics import run_review, supported_fields
    semantic = run_review(semantic_inputs, all_excerpts, cfg, llm)
    merged['_semantic_review'] = semantic
    merged['_batch_notes'] = batch_notes
    if semantic['status'] != 'not_run':
        safe = supported_fields(semantic)
        for field, _ in _FIELDS:
            merged[field] = safe.get(field, '')
        # Only retained claims may contribute displayed evidence.
        merged['field_evidence'] = {
            field: list(dict.fromkeys(q['tag']+(f" p.{q['page']}" if q['page'] is not None else '')
                                     for item in semantic['items'] if item['field'] == field
                                     and item['status'] == 'machine_supported' for q in item['quotes']))
            for field, _ in _FIELDS if merged[field]}
    merged['keywords'] = list(dict.fromkeys(k for r in results for k in r.get('keywords', []) if isinstance(k, str)))
    missing_fields = [label for field, label in _FIELDS if not merged[field]]
    merged['field_notes'] = ('本次未获得可校验依据的字段：'+'、'.join(missing_fields)+'。请核对原文，不能据此断言全文未提及。') if missing_fields else '所有展示字段均保留本批原文引用；内容仍需结合原文核对。'
    merged['_coverage'] = coverage
    merged['_coverage']['reading_mode'] = 'whole' if len(batches) == 1 else 'batched'
    merged['_citation_check'] = {'passed': not all_issues, 'issues': list(dict.fromkeys(all_issues))}
    merged['_citations'] = [{'tag': e['tag'], 'chunk_id': e['id'], 'paper_id': paper_id, 'page': e['page']} for e in all_excerpts]
    merged['_model'] = getattr(llm, 'name', 'unknown')
    if not isinstance(merged.get('field'), str) or citation_issues(merged.get('field') or '', all_excerpts):
        merged['field'] = ''
    unique = {(c['term'], c['definition']): c for c in all_concepts}
    merged['concepts'] = list(unique.values())
    if concept_errors:
        merged['_concepts_error'] = '；'.join(concept_errors)
    # 所有批次成功后原子替换，失败时保留上一版。
    checkpoint('保存已校验解读')
    from .evidence import stored_excerpts
    stored_excerpts(kb, merged['_citations'])
    with kb.tx() as con:
        con.execute('DELETE FROM concepts WHERE paper_id=?', (paper_id,))
        kb.add_concepts(paper_id, merged['concepts'])
        kb.save_analysis(paper_id, merged, model=merged['_model'])
    return merged


def _validate_fields(data: dict, excerpts: list[dict]):
    from .evidence import citation_issues
    issues = []
    allowed = {field for field, _ in _FIELDS} | {'field', 'field_notes', 'field_evidence', 'keywords'}
    if any(k not in allowed and not k.startswith('_') for k in data):
        # A loose JSON decoder may recover an object whose broken string became
        # a giant key. Citation syntax alone must not release that batch.
        clean = {field: '' for field, _ in _FIELDS}
        clean.update(field_evidence={}, keywords=[], field_notes='模型返回未知字段或结构损坏，本批字段未放行。')
        return clean, ['模型返回未知字段或结构损坏，本批字段未放行；请核对原始响应。']
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
    keywords = data.get('keywords')
    data['keywords'] = [v for v in keywords if isinstance(v, str)] if isinstance(keywords, list) else []
    return data, issues


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
        lines.append(val if val else "（当前提取未找到可校验依据，已留空；请核对原文）")
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
    semantic = analysis.get('_semantic_review') or {}
    if semantic:
        lines.append('> '+semantic.get('note', ''))
        if semantic.get('pending'):
            lines.append(f"> {semantic['pending']} 条陈述待人工核对，未列入上述结论；请在界面查看双方来源。")
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
