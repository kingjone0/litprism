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
                  force: bool = False, with_concepts: bool = True, finalize=None,
                  semantic_review: bool = True) -> dict:
    from .evidence import citation_issues, require_citable
    from .reading import build_messages, excerpt_context, reading_batches
    paper = kb.get_paper(paper_id)
    if paper is None:
        raise ValueError(f'未找到论文：{paper_id}')
    require_citable(paper.get('verify_level'))
    if not force:
        cached = kb.get_analysis(paper_id)
        if cached and cached.get('payload') and cached.get('material_current'):
            return cached['payload']
    chunks = kb.get_chunks(paper_id)
    material_id = chunks[0]['material_id'] if chunks else None
    if sum(len(c['text']) for c in chunks) < 150:
        raise ValueError('正文过少，无法解读；扫描件需先转写。')

    from copy import deepcopy
    cfg = deepcopy(getattr(llm, 'runtime_config', cfg))
    # 窗口跟着当前模型走：不这样做，换了大窗口模型也不会用上。
    from .providers import apply_active_window
    window_info = apply_active_window(cfg, kb)
    extract_system = EXTRACT_SYSTEM + '\n每个非空字段必须在 field_evidence 提供原文标记列表；无依据留空。'
    extract_system += '\n每个独立句子就地附其直接依据，不把多个分号连接的结论统一在段末附一串引用；每句最多 3 个直接来源，复杂内容分成多句。'
    concept_system = CONCEPTS_SYSTEM + '\n每条 definition 必须含本批原文引用 [P# p.N]；无页码不写页码，无依据不生成。'
    # 较长的指令作为分批预算基准，确保两类请求都容纳得下。
    system = max([extract_system, concept_system], key=len)
    instruction = '仅分析本批原文。未读取部分不能称为原文未提及。'

    # ---- 计划：整篇装得下就共享一份上下文前缀，否则退回分批 ----
    from .context.meter import Meter
    from .plan import build_plan

    meter = Meter(cfg)
    plan = build_plan(
        paper, chunks, meter,
        available_tokens=cfg.context.available_tokens,
        max_tokens=min(cfg.llm.max_tokens, cfg.context.output_reserve_tokens),
        overhead_tokens=meter.estimate_text(system) + 200,
    )

    all_excerpts = [dict(c, tag=f'P{i}', paper_title=paper['title'], document_tag='D1')
                    for i, c in enumerate(chunks, 1)]
    full_ctx = excerpt_context(all_excerpts)
    if plan.mode == 'whole':
        from .gates import repair_text
        # Include real source labels, partial target copies, repair instructions,
        # and both message envelopes; raw body size alone is insufficient.
        texts = [repair_text(d, 0) for d in plan.directives]
        candidates = [(extract_system, text) for text in texts]
        if with_concepts:
            candidates.append((concept_system, '只抽取本批原文出现的术语，逐条给出定义并附原文引用。'))
        if any(sum(meter.estimate_message(m) for m in build_messages(sys, full_ctx, text))
               > cfg.context.available_tokens for sys, text in candidates):
            plan.mode, plan.directives = 'batched', []
            plan.note = '完整请求（原文标签、任务范围及修复预留）超过窗口，退回分批；不能共享全文缓存。'
    if plan.mode == 'whole':
        shared_ctx = full_ctx
        units = [(d, all_excerpts) for d in plan.directives]
        coverage = {
            'complete': True, 'batches': len(plan.directives),
            'total_chunks': len(chunks), 'read_chunks': len(chunks), 'omitted_chunks': [],
            'sections': sorted({c.get('section') or '正文' for c in chunks}),
            'basis': 'indexed_chunks', 'reading_mode': 'whole',
            'note': plan.note + ' 每条指令只提取指定章节；其余章节仅作上下文参考。',
        }
    else:
        batches, all_excerpts, coverage = reading_batches(kb, cfg, [paper], system, instruction)
        shared_ctx = None
        units = [(None, b) for b in batches]
        coverage['reading_mode'] = 'batched'
    coverage['plan'] = plan.to_dict()
    coverage['window'] = window_info

    results, all_issues, all_concepts, concept_errors, semantic_inputs, batch_notes = [], [], [], [], [], []
    gate_log: list[dict] = []
    uncovered: list[str] = []
    unit_claims: list[tuple[str, list[str]]] = []
    from .usage import UsageTracker

    tracker = UsageTracker()
    from .tasks import checkpoint
    for index, (directive, excerpts) in enumerate(units, 1):
        unit_label = directive.id if directive is not None else f'b{index}'
        checkpoint(f'解读原文第 {index}/{len(units)} 段')
        # 原文在前、指令在后：同一篇论文的多次调用共享这段前缀，才能命中缓存。
        ctx = shared_ctx if shared_ctx is not None else excerpt_context(excerpts)
        # 整篇模式下引用可以落在任何章节（结论引用方法细节是正常的），
        # 所以按全篇校验，而不是按本条指令的范围。
        scope = all_excerpts if shared_ctx is not None else excerpts

        data, issues, gates = _run_with_repair(
            llm, extract_system, ctx, directive, instruction, scope, unit_label, tracker,
            meter=meter, available_tokens=cfg.context.available_tokens,
        )
        gate_log.append({'unit': unit_label, **gates.to_dict()})
        if gates.failed and data is not None:
            uncovered.append(unit_label)
        if data is None:
            # 完全没拿到可用结果（例如输出被截断）：如实记未覆盖，不伪造空字段。
            all_issues.extend(issues)
            uncovered.append(unit_label)
            continue
        all_issues.extend(issues)
        results.append(data)
        from .gates import claim_sentences
        unit_claims.append((unit_label, claim_sentences(data, _FIELDS)))
        semantic_inputs.append(([(field, data.get(field) or '', (data.get('field_evidence') or {}).get(field) or [])
                                 for field, _ in _FIELDS], scope))
        if isinstance(data.get('field_notes'), str) and data['field_notes'].strip():
            batch_notes.append({'batch': index, 'text': data['field_notes'], 'status': 'unreviewed_model_note'})
        if with_concepts:
            try:
                from .usage import tracked_json
                cdata = tracked_json(llm,
                    build_messages(concept_system, ctx, '只抽取本批原文出现的术语，逐条给出定义并附原文引用。'),
                    task='concepts', tracker=tracker,
                )
                concepts = cdata.get('concepts') or []
                if not isinstance(concepts, list):
                    raise ValueError('concepts 必须是数组。')
                all_concepts.extend(c for c in concepts if isinstance(c, dict)
                                    and isinstance(c.get('term'), str) and isinstance(c.get('definition'), str)
                                    and not citation_issues(c['definition'], excerpts))
            except Exception as exc:
                if getattr(exc, 'stop_analysis_run', False):
                    raise
                concept_errors.append(f'{type(exc).__name__}: {exc}')
    # 所有段落都没产出时不能直接取 results[0]；给一个全空骨架，
    # 让下游看到"无内容 + 未覆盖"，而不是抛 IndexError 让整次解读失败。
    merged = dict(results[0]) if results else {k: '' for k, _ in _FIELDS}
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
    from .semantics import make_units, run_review, supported_fields
    # 候选陈述先落盘：人工审核不依赖机器复核是否运行、是否通过。
    # 复用 make_units（确定性 ID），所以复核结果与候选是同一套编号。
    candidates = []
    for batch, (fields, sources) in enumerate(semantic_inputs, 1):
        candidates.extend(make_units(fields, sources, batch))
    merged['_candidates'] = [{k: u[k] for k in ('id', 'field', 'text', 'batch', 'candidate_tags', 'adjacent_tags')}
                             for u in candidates]
    if semantic_review:
        semantic = run_review(semantic_inputs, all_excerpts, cfg, llm, tracker=tracker)
    else:
        semantic = {'version': 2, 'protocol': 'source_selection', 'status': 'skipped_by_user',
                    'passed': False, 'items': [], 'differences': [],
                    'coverage': {'complete': False, 'skipped_claim_ids': [u['id'] for u in candidates],
                                 'method': 'skipped_by_user', 'calls': 0, 'max_calls': None, 'tiles': 0},
                    'supported': 0, 'pending': len(candidates), 'review_calls': 0, 'max_review_calls': None,
                    'note': '按设置跳过机器语义核查；候选陈述已保留，可直接人工审核。'}
    merged['_semantic_review'] = semantic
    merged['_batch_notes'] = batch_notes
    if semantic['status'] not in ('not_run', 'skipped_by_user'):
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
    from .db import material_label, material_rank
    material = next((m for m in kb.list_materials(paper_id) if m['id'] == material_id), {})
    level = material.get('level') or 'unknown'
    coverage['material_level'] = level
    coverage['material_label'] = material_label(level)
    if material_rank(level) < material_rank('fulltext'):
        # 把"只有题录/摘要"顶到最前面：覆盖 N/N 不等于读完了整篇。
        coverage['partial_material'] = True
        prefix = f'本篇材料完整度为「{material_label(level)}」：解读与引用只覆盖已索引内容，不能视为整篇论文已读完。'
        merged['field_notes'] = prefix + ' ' + merged.get('field_notes', '')
    coverage['gates'] = gate_log
    coverage['uncovered_units'] = uncovered
    coverage['usage'] = tracker.to_dict()
    # 跨指令重复：条数正常但内容全是别人的 —— 条数闸门抓不到这一类
    from .gates import overlap_flags
    overlaps = overlap_flags(unit_claims)
    if overlaps:
        coverage['overlaps'] = overlaps
    if uncovered:
        # 有段落没拿到可用产出 —— 整体覆盖不完整，不伪装成通过
        coverage['complete'] = False
        coverage['note'] = coverage.get('note', '') + (
            f' 其中 {len(uncovered)} 段未完成提取闸门或没有可用产出：{"、".join(uncovered)}。'
        )
    merged['_citation_check'] = {'passed': not all_issues, 'issues': list(dict.fromkeys(all_issues))}
    from .evidence import citations_for
    merged['_citations'] = citations_for(all_excerpts)
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
        if (kb.get_paper(paper_id) or {}).get('current_material_id') != material_id:
            from .materials import MaterialChanged
            raise MaterialChanged('解读期间原文材料已变化；保留旧分析，请对当前版本重新解读。')
        if finalize is not None:
            finalize(merged)
        con.execute('DELETE FROM concepts WHERE paper_id=?', (paper_id,))
        kb.add_concepts(paper_id, merged['concepts'])
        kb.save_analysis(paper_id, merged, model=merged['_model'], material_id=material_id)
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


def _run_with_repair(llm, system: str, ctx: str, directive, fallback: str,
                     scope: list[dict], unit_label: str, tracker=None, *,
                     meter=None, available_tokens=None):
    """跑一条指令，按闸门结果**有界修复**。

    返回 ``(data | None, issues, gates)``。``data is None`` 表示这条指令
    没有可用产出（例如输出被截断）—— 此时如实记未覆盖，不伪造空字段。

    修复只针对「疑似漏读」（条数低于期望下限）。截断不自动细分：
    那需要重新规划指令，留到下一版；这里先保证不崩、不假通过。
    """
    from .gates import MAX_REPAIR_ROUNDS, GateReport, GateResult, count_claims, evaluate, repair_text
    from .llm import OutputLimitError
    from .reading import build_messages

    text = directive.text if directive is not None else fallback
    expected = directive.expected if directive is not None else None

    best = None  # (score, data, issues, gates)
    for attempt in range(MAX_REPAIR_ROUNDS + 1):
        messages = build_messages(system, ctx, text)
        if meter is not None and available_tokens is not None and sum(
                meter.estimate_message(m) for m in messages) > available_tokens:
            gates = GateReport([GateResult('input_budget', False, '完整请求超过可用窗口，未调用模型。', 'flag')])
            return (best[1], best[2], gates) if best else (None, [f'{unit_label} 完整请求超出预算。'], gates)
        try:
            from .usage import tracked_json
            raw = tracked_json(llm, messages, task='extract', tracker=tracker)
        except OutputLimitError as exc:
            gates = GateReport([GateResult('json_complete', False, f'输出被截断：{exc}', 'flag')])
            if best is None:
                return None, [f'{unit_label} 输出被截断，该段未覆盖：{exc}'], gates
            return best[1], best[2], best[3]

        if not isinstance(raw, dict):
            raise ValueError('解读必须返回 JSON 对象。')
        data, issues = _validate_fields(raw, scope)
        gates = evaluate(data, _FIELDS, expected=expected, issues=issues)
        claims = count_claims(data, _FIELDS)
        # 先比"有没有闸门失败"，再比"提出多少条" —— 修完更差就不采纳
        score = (len(gates.failed) == 0, claims)
        if best is None or score >= best[0]:
            best = (score, data, issues, gates)
        if not gates.needs_repair or directive is None or attempt >= MAX_REPAIR_ROUNDS:
            break
        text = repair_text(directive, claims)
    return best[1], best[2], best[3]


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
        if not force and (kb.get_analysis(p['id']) or {}).get('material_current'):
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
        if not analysis.get('material_current'):
            raise ValueError('分析基于旧材料或未绑定材料，请重新解读当前原文后导出。')
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
