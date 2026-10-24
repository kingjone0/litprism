"""三个交互 Agent：

* :class:`QAAgent`        —— 基于本地知识库的带引用问答（含虚构引用检测）
* :class:`CompareAgent`   —— 多篇论文对比分析
* :class:`RecommendAgent` —— 依据研究方向推荐文献（推荐项全部经真实性核验，可选导入本地库）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .db import KnowledgeBase
from .prompts import QA_QUOTE_CHECK, QA_SYSTEM, RECOMMEND_SYSTEM
from .search import citing_works, related_works, search_external, search_local
from .textutil import truncate
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
    evidence_audit: dict = field(default_factory=dict)  # mechanical location only, audit sidebar
    excerpts: list[dict] = field(default_factory=list)
    citation_issues: list[str] = field(default_factory=list)
    citation_check_passed: bool = True
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    provider_usage: dict | None = None
    request_tokens: int = 0
    reasoning: str = ''
    request_settings: dict = field(default_factory=dict)
    status: str = 'answered'
    draft_answer: str = ''  # audit only; never a normal answer or history input
    usage: dict = field(default_factory=dict)
    retrieval: dict = field(default_factory=dict)
    guard_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "confidence": self.confidence,
            "insufficient_evidence": self.insufficient_evidence,
            "open_questions": self.open_questions,
            "evidence_map": self.evidence_map,
            "evidence_audit": self.evidence_audit,
            "excerpts": [
                {
                    "tag": e["tag"], "chunk_id": e.get("chunk_id"),
                    "paper_id": e["paper_id"], "title": e["title"],
                    "page": e["page"], "section": e["section"], "score": e["score"],
                    "text": truncate(e["text"], 700),
                    'source_kind': e.get('source_kind', 'paper'), 'source_id': e.get('source_id'), 'url': e.get('url'),
                }
                for e in self.excerpts
            ],
            "citation_issues": self.citation_issues,
            "citation_check_passed": self.citation_check_passed,
            "model": self.model,
            "warnings": self.warnings,
            'provider_usage': self.provider_usage,
            'request_tokens': self.request_tokens,
            'reasoning': self.reasoning,
            'request_settings': self.request_settings,
            'status': self.status,
            'usage': self.usage,
            'retrieval': self.retrieval,
            'guard_issues': self.guard_issues,
        }


class QAAgent:
    """检索增强问答。三道防幻觉闸门：

    1. 只把检索到的原文片段交给模型，提示词禁止使用片段外知识；
    2. 无召回时直接拒答（``refuse_when_no_evidence``）；
    3. 回答产出后，逐条校验 ``[P# p.##]`` 引用是否指向真实片段（``strict_citation_check``）。
    """

    def __init__(self, kb: KnowledgeBase, cfg: Config, llm):
        self.kb = kb
        self.cfg = getattr(llm, 'runtime_config', cfg)
        self.llm = llm

    def ask(
        self,
        question: str,
        *,
        paper_ids: list[str] | None = None,
        top_k: int | None = None,
        conversation_id: str | None = None,
        before_seq: int | None = None,
        strict_scope: bool = True,
        external: tuple | list = (),
    ) -> QAAnswer:
        from .context.dialogue import HISTORY_GUARD, DialogueContext, build_dialogue
        from .context.meter import Meter
        from .usage import TrackedClient, UsageTracker

        tracker = UsageTracker()
        client = TrackedClient(self.llm, tracker)
        res = QAAnswer(question=question, model=getattr(self.llm, 'name', 'unknown'))
        def finish():
            res.usage = tracker.to_dict()
            return res
        dialogue = build_dialogue(self.kb, self.cfg, conversation_id, before_seq) if conversation_id else DialogueContext()
        meter = Meter(self.cfg)
        def freeze_refusal(outcome, request):
            if conversation_id:
                from .memory.view import TurnViewStore
                TurnViewStore(self.kb).freeze(conversation_id, dialogue.memory, read_refs={
                    **dialogue.memory.read_refs(), 'question': question, 'request': request,
                    'outcome': outcome, 'model': getattr(self.llm, 'name', 'unknown'),
                    'preferences': [m.to_dict() for m in dialogue.memory.resident]})
        if meter.estimate_text(question) > self.cfg.context.available_tokens // 2:
            freeze_refusal('question_over_budget', [])
            res.answer, res.status = '问题过长，请缩短后重试。', 'budget_exceeded'
            res.insufficient_evidence, res.confidence = True, 'none'
            return finish()
        top_k = top_k or self.cfg.retrieval.top_k
        from .qa_retrieval import as_excerpts, material_text, retrieve
        from .qa_evidence import manifest, material_risks
        scope = '全库可引用论文与人工确认知识，以及本轮取得的外部证据（如有；保留类型与审核状态）'
        delivery = {}
        if paper_ids:
            titles = [self.kb.get_paper(p) for p in paper_ids]
            scope = ('仅限指定论文：' if strict_scope else '重点论文（同时补充相关可信知识及本轮外部证据）：')+'、'.join(truncate(t['title'], 40) for t in titles if t)
        def messages_for(excerpts, history=''):
            import json
            nonlocal delivery
            delivery = manifest(self.kb, excerpts)
            system = QA_SYSTEM+'\n\n'+HISTORY_GUARD+'\n\n'+dialogue.memory.prompt_block()
            if any(e.get('source_kind') != 'paper' for e in excerpts):
                system += '\n部分片段为人工确认知识或本轮搜索摘要。必须按程序标记区分来源/审核状态；搜索摘要未必经人工审核，不能称为网页正文或核验论文。'
            # Keep source bytes before changing history/question for caching.
            content = (f'检索范围：{scope}\n\n【片段】\n'+material_text(excerpts)
                       +'\n\n【程序送达记录】\n'+json.dumps(delivery, ensure_ascii=False)
                       +'\n\n'+history+f'\n\n【问题】{question}\n\n'
                       +'请仅依据上述片段作答，并在每条结论后只标注片段编号（如 [P1]），不生成页码；来源链接由程序展示真实页码。'
                       +'未提供的论文或章节不能声称已读完，题录/摘要不等于全文。'
                       +'证据不足应明确拒答，不能依据历史回答补事实。')
            if not self.cfg.qa.evidence_anchor_trial:
                content += '\n'+QA_QUOTE_CHECK
            if self.cfg.qa.evidence_anchor_trial:
                from .qa_anchors import catalog
                content += '\n\n【试验定位锚点（程序生成，不是语义结论）】\n'+json.dumps(catalog(excerpts), ensure_ascii=False)
                system += ('\n本轮开启原文锚点小范围试验。回答使用answer_units，每项保留text/subject/basis/sources结构；'
                           'answer_units各项原文quotes优先只选择本轮目录中该来源的anchor_id，形如'
                           '{"source":"P1","anchor_id":"qa_…"}，不要再复制quote。'
                           '原文由程序按材料版本和字符范围回填，不得发明锚点或引用其他来源的锚点。'
                           '程序记录仍使用metadata及实际完整字段。锚点位置正确不代表能支持陈述。')
                system += ('优先选择覆盖实际依据的短句/短段锚点；需要多句或多个片段时分别列出各自锚点，'
                           '不得把邻近内容自动当成依据，短段边界不认证语义完整。')
            return [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]
        from .context.dialogue import is_followup
        hits, res.retrieval = retrieve(self.kb, question, dialogue.query(question, paper_ids), paper_ids=paper_ids,
                                      top_k=top_k, llm=client, search=search_local, cfg=self.cfg,
                                      fits_full=lambda hs: sum(meter.estimate_message(m) for m in messages_for(as_excerpts(hs)))+8 <= self.cfg.context.available_tokens,
                                      prior_refs=dialogue.evidence_refs if is_followup(question) else (),
                                      strict_scope=strict_scope, external=external)

        if not hits:
            res.status = 'refused'
            res.insufficient_evidence = True
            res.confidence = "none"
            res.answer = (
                "本地知识库中没有检索到与该问题相关的原文片段。\n\n"
                "可能原因：① 尚未导入相关论文（可用 `add` 导入 PDF，或 `recommend` 检索并导入）；"
                "② 问题用词与论文表述差异较大，可换用论文中的术语再问。"
            )
            if conversation_id:
                from .memory.view import TurnViewStore
                TurnViewStore(self.kb).freeze(conversation_id, read_refs={'request': [], 'outcome': 'no_evidence', 'question': question})
            return finish()

        excerpts = as_excerpts(hits)
        res.excerpts = excerpts
        material = material_text(excerpts)
        messages = messages_for(excerpts)

        meter = Meter(self.cfg)
        while sum(meter.estimate_message(m) for m in messages) + 8 > self.cfg.context.available_tokens:
            if len(excerpts) <= 1:
                res.answer = '当前上下文预算不足以容纳问题与原文证据，请缩小范围或增加窗口预算。'
                res.insufficient_evidence = True
                res.excerpts = []
                res.confidence = 'none'
                res.status = 'budget_exceeded'
                freeze_refusal('evidence_over_budget', messages)
                return finish()
            excerpts.pop()
            material = material_text(excerpts)
            messages = messages_for(excerpts)
        remaining = max(0, self.cfg.context.available_tokens - sum(meter.estimate_message(m) for m in messages) - 8)
        history, history_refs = dialogue.prompt(meter, remaining)
        if history:
            messages = messages_for(excerpts, history)
        from copy import deepcopy
        attempts = []
        error = None
        for retry in range(self.cfg.context.overflow_retries + 1):
            attempts.append(deepcopy(messages))
            try:
                data = client.chat_json(messages, task='qa')
                if not isinstance(data, dict):
                    raise ValueError('模型没有返回 JSON 对象。')
                from .qa_answer import compile_answer
                answer, mapping, answer_bindings, answer_protocol = compile_answer(data)
                if 'insufficient_evidence' in data and type(data['insufficient_evidence']) is not bool:
                    raise ValueError('证据不足标记必须为布尔值。')
                error = None
                break
            except Exception as exc:
                error = exc
                overflow = any(word in str(exc).lower() for word in ('context_length', 'context window', 'maximum context', 'too many tokens'))
                if not overflow or retry >= self.cfg.context.overflow_retries:
                    break
                if history:
                    history, history_refs = '', []
                elif len(excerpts) > 1:
                    excerpts.pop()
                    material = material_text(excerpts)
                else:
                    break
                messages = messages_for(excerpts, history)
        res.request_tokens = sum(meter.estimate_message(m) for m in messages)
        res.retrieval.update(delivered_chunks=len(excerpts),
                             delivered_chunk_ids=[e['chunk_id'] for e in excerpts],
                             evidence_papers=list(dict.fromkeys(e['paper_id'] for e in excerpts if e['paper_id'])),
                             window_limited=len(excerpts) < res.retrieval['candidate_chunks'])
        res.retrieval['evidence_manifest'] = delivery
        res.provider_usage = getattr(self.llm, 'last_usage', None) if error is None else None
        res.reasoning = getattr(self.llm, 'last_reasoning', '') if error is None else ''
        res.request_settings = dict(getattr(self.llm, 'last_request_settings', {}) or {})
        if conversation_id:
            from .memory.view import TurnViewStore
            TurnViewStore(self.kb).freeze(
                conversation_id, dialogue.memory,
                read_refs={**dialogue.memory.read_refs(), 'history': history_refs,
                           'retrieved': [e['chunk_id'] for e in excerpts if e.get('chunk_id')],
                           'request': messages, 'request_attempts': attempts, 'model': res.model,
                           'outcome': 'error' if error else 'submitted',
                           'preferences': [m.to_dict() for m in dialogue.memory.resident]},
                retrieved_tokens=meter.estimate_text(material),
            )
        if error:
            from .llm import safe_failure
            res.request_settings['failure'] = safe_failure(error)
            res.answer = '模型调用失败，请检查服务状态后重试。'
            res.status = 'error'
            res.confidence = 'error'
            res.warnings.append('模型请求失败：'+str(res.request_settings['failure']))
            return finish()

        res.answer = answer
        res.draft_answer = res.answer
        res.confidence = str(data.get("confidence") or "unknown")
        res.insufficient_evidence = bool(data.get("insufficient_evidence"))
        res.open_questions = list(data.get("open_questions") or [])
        from .qa_claims import audit_map
        res.evidence_audit = audit_map(res.draft_answer, mapping, res.excerpts,
                                       anchor_trial=self.cfg.qa.evidence_anchor_trial,
                                       answer_bindings=answer_bindings, answer_protocol=answer_protocol)
        res.retrieval['evidence_protocol'] = res.evidence_audit['protocol']
        res.evidence_map = res.evidence_audit['items']

        self._check_citations(res)
        # 偏好守卫与引用检查是独立约束，不能因关闭常规检查而绕过。
        from .memory.guards import detect_memory_as_fact, validate_citations

        for result in (detect_memory_as_fact(res.answer), validate_citations(res.answer)):
            if not result.ok:
                res.citation_check_passed = False
                res.citation_issues.extend(v.message for v in result.violations)
                res.warnings.append(result.summary())
        if not res.citation_check_passed:
            res.status = 'invalid_citation'
            res.answer = '回答的来源校验未通过，已阻止展示结论。请核对原文后重新提问。'
            res.open_questions, res.evidence_map = [], []
        else:
            from .qa_guard import answer_risks
            from .qa_conditions import conditional_risks
            res.guard_issues = list(dict.fromkeys(
                answer_risks(res.answer, res.excerpts)
                + conditional_risks(res.answer, res.excerpts)
                + material_risks(res.answer, res.excerpts, res.retrieval['evidence_manifest'])
                + res.evidence_audit['blocking_issues']))
            res.warnings.extend(res.evidence_audit['warnings'])
            if res.guard_issues:
                from .textutil import parse_citations
                refs = [f'[{tag}'+(f' p.{page}' if page is not None else '')+']'
                        for tag, page in dict.fromkeys(parse_citations(res.answer))]
                res.status, res.confidence = 'needs_review', 'none'
                res.answer = '回答的证据归属存在疑点，已隔离生成草稿。请核对原文后再判断。\n'+' '.join(refs)
                res.open_questions, res.evidence_map = [], []
            elif res.insufficient_evidence:
                res.status = 'refused'
        return finish()

    def _check_citations(self, res: QAAnswer) -> None:
        """校验回答中的引用标记是否都能对应到真实片段。"""
        from .evidence import citation_issues, stored_excerpts
        res.citation_issues.extend(citation_issues(res.answer, res.excerpts, required=not res.insufficient_evidence))
        from .textutil import parse_citations
        used = {tag for tag, _ in parse_citations(res.answer)}
        try:
            stored_excerpts(self.kb, [{'tag': e['tag'], 'chunk_id': e.get('chunk_id'), 'paper_id': e.get('paper_id'),
                                      'web_grant_id': e.get('web_grant_id')}
                                      for e in res.excerpts if e['tag'] in used], allow_session=True)
        except ValueError as exc:
            res.citation_issues.append(str(exc))
            res.answer = '来源状态在生成期间发生变化，已阻止展示结论，请核对右栏来源后重试。'
            res.insufficient_evidence = True
        res.citation_check_passed = not res.citation_issues
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
        self.cfg = getattr(llm, 'runtime_config', cfg)
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

        from .comparison import check_comparison
        from .evidence import require_citable
        from .prompts import SYSTEM_ACADEMIC
        from .reading import build_messages, excerpt_context, reading_batches
        for paper in papers:
            require_citable(paper.get('verify_level'))
        system = SYSTEM_ACADEMIC + "\n仅比较给定原文，不使用先前解读作为事实。维度缺信息写未提及。输出 JSON：summary, dimensions（每项 dimension, values，键为 D1/D2 等论文编号）, convergence, divergence, evidence_map。每个非空学术结论标注 [P# p.N]，P 是原文片段编号；无页码来源不写页码。"
        system += '\n严格使用以下结构：{"summary":"结论 [P1 p.3]","dimensions":[{"dimension":"方法","values":{"D1":"方法 [P1 p.3]","D2":"方法 [P2 p.3]"}}],"convergence":["一致点 [P1 p.3][P2 p.3]"],"divergence":["差异 [P1 p.3][P2 p.3]"],"evidence_map":[{"claim":"结论","sources":["P1 p.3","P2 p.3"]}]}。这里页码仅为格式示例，必须替换为实际页码。convergence、divergence、sources 必须是字符串数组，无结论则用空数组。'
        instruction = '仅比较本批原文，未读取的部分不能写成原文未提及。'
        if aspects:
            instruction += '\n关注维度：' + '、'.join(aspects)
        batches, excerpts, coverage = reading_batches(self.kb, self.cfg, papers, system, instruction)
        from .usage import UsageTracker, tracked_json
        tracker = UsageTracker()
        results, issues = [], []
        from .tasks import checkpoint
        for index, batch in enumerate(batches, 1):
            checkpoint(f'对比原文第 {index}/{len(batches)} 批')
            raw = tracked_json(self.llm,
                build_messages(system, excerpt_context(batch), instruction), task='compare', tracker=tracker)
            result, errors = check_comparison(raw, papers, batch)
            issues.extend(errors)
            results.append(result)
        checkpoint('校验对比引用与文献归属')
        data = {'summary': '\n\n'.join(r['summary'] for r in results if isinstance(r.get('summary'), str)),
                'dimensions': [], 'convergence': [], 'divergence': [], 'evidence_map': []}
        for result in results:
            for key in ('dimensions', 'convergence', 'divergence', 'evidence_map'):
                data[key].extend(result.get(key) or [])
        dimensions = {}
        for dimension in data['dimensions']:
            if not isinstance(dimension, dict) or not isinstance(dimension.get('dimension'), str) or not isinstance(dimension.get('values'), dict):
                continue
            merged = dimensions.setdefault(dimension['dimension'], {})
            for tag, value in dimension['values'].items():
                if isinstance(value, str) and value and value != '未提及':
                    parts = merged.setdefault(tag, [])
                    if value not in parts:
                        parts.append(value)
        data['dimensions'] = [{'dimension': name, 'values': {tag: '\n\n'.join(parts) for tag, parts in values.items()}}
                              for name, values in dimensions.items()]
        from .semantics import run_review, supported_fields
        inputs = []
        for result, batch in zip(results, batches, strict=True):
            fields = [('summary', result.get('summary') or '', [])]
            for dimension in result.get('dimensions') or []:
                for tag, text in dimension['values'].items():
                    fields.append(('dimension|'+dimension['dimension']+'|'+tag, text, []))
            for key in ('convergence', 'divergence'):
                fields.extend((key, text, []) for text in result.get(key) or [])
            inputs.append((fields, batch))
        if not issues:
            semantic = run_review(inputs, excerpts, self.cfg, self.llm, tracker=tracker)
            data['_semantic_review'] = semantic
            if semantic['status'] != 'not_run':
                safe = supported_fields(semantic)
                data['summary'] = safe.get('summary', '')
                data['dimensions'] = [
                    {'dimension': name, 'values': {tag: safe.get('dimension|'+name+'|'+tag, '') for tag in values}}
                    for name, values in dimensions.items()]
                for key in ('convergence', 'divergence'):
                    data[key] = [u['text'] for u in semantic['items'] if u['field'] == key and u['status'] == 'machine_supported']
                data['evidence_map'] = [{'claim': u['text'], 'sources': [q['tag'] for q in u['quotes']]}
                                        for u in semantic['items'] if u['status'] == 'machine_supported']
        issues = list(dict.fromkeys(issues))
        data['_coverage'] = coverage
        coverage['usage'] = tracker.to_dict()
        data['_citation_check'] = {'passed': not issues, 'issues': issues}
        from .evidence import citations_for, stored_excerpts
        data['_citations'] = citations_for(excerpts)
        try:
            stored_excerpts(self.kb, data['_citations'])
        except ValueError as exc:
            issues.append(str(exc))
            data['_citation_check'] = {'passed': False, 'issues': issues}
        data['_model'] = getattr(self.llm, 'name', 'unknown')
        data['_papers'] = [{'tag': f'D{i}', 'id': p['id'], 'title': p['title']} for i, p in enumerate(papers, 1)]
        data['_coverage']['reading_mode'] = 'whole' if len(batches) == 1 else 'batched'
        if issues:
            data = {k: v for k, v in data.items() if k.startswith('_')}
            data['summary'] = '引用校验未通过，已阻止展示无法回溯的对比结论。'
        if missing:
            data['_missing'] = missing
        if data.get('_semantic_review', {}).get('status') not in (None, 'not_run'):
            import json
            import uuid

            from .knowledge import now
            data['_semantic_run_id'] = 'sm_'+uuid.uuid4().hex[:12]
            with self.kb.tx() as con:
                stored_excerpts(self.kb, data['_citations'])
                con.execute('INSERT INTO semantic_runs VALUES (?,?,?,?)',
                            (data['_semantic_run_id'], '跨论文对比', json.dumps(data, ensure_ascii=False), now()))
        return data


def render_comparison(data: dict) -> str:
    papers = data.get("_papers") or []
    lines = ["# 论文对比分析", ""]
    semantic = data.get('_semantic_review') or {}
    lines += ['> '+(semantic.get('note') or '尚未运行整体语义核查。引用存在不等于语义正确。'),
              f"> 待核对陈述：{semantic.get('pending', '未知')}；待核对项不作为正常结论导出。", '']
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
        self.cfg = getattr(llm, 'runtime_config', cfg)
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
                if not p or not is_citable(p.get('verify_level')):
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
