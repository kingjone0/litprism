"""Skill 执行器。

执行契约
--------
```
① 加载并校验 manifest
② 按 context 声明预取上下文（只调用白名单内的能力）
③ 渲染 prompt.md（替换 {{var}}）
④ 组装最终提示词：
     全局学术规范与防幻觉约束（框架强制前置，不可移除）
   + Skill 提示词
   + 预取上下文
   + 用户输入
⑤ 调用模型
⑥ 后处理：citation_required=true → 强制走引用回溯校验
⑦ 落库或返回
```

**两条不可协商的点**

1. 全局约束由框架**强制前置**，Skill 作者无法移除或覆盖。
2. `citation_required` 时引用校验由**框架**执行，不依赖 Skill 作者自觉 ——
   用户自行添加的 Skill 是系统里最不可信的输入源。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..prompts import SYSTEM_ACADEMIC
from ..textutil import parse_citations, truncate
from .manifest import CAPABILITIES, SkillManifest
from .registry import SkillEntry, SkillRegistry

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CitationCheck:
    passed: bool = True
    issues: list[str] = field(default_factory=list)
    cited_tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "issues": self.issues, "cited_tags": self.cited_tags}


@dataclass
class SkillRunResult:
    ok: bool
    skill_id: str = ""
    skill_version: str = ""
    answer: str = ""
    deliverable_id: str | None = None
    excerpts: list[dict] = field(default_factory=list)
    citation_check: CitationCheck = field(default_factory=CitationCheck)
    prefetched: dict = field(default_factory=dict)
    prompt_tokens_hint: int = 0
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            "answer": self.answer,
            "deliverable_id": self.deliverable_id,
            "citation_check": self.citation_check.to_dict(),
            "excerpt_count": len(self.excerpts),
            "error": self.error,
            "warnings": self.warnings,
        }


class SkillRunner:
    """执行一个 Skill。"""

    def __init__(
        self,
        kb: Any,
        cfg: Config,
        *,
        llm: Any = None,
        registry: SkillRegistry | None = None,
    ):
        self.kb = kb
        self.cfg = getattr(llm, 'runtime_config', cfg)
        self.llm = llm
        self.registry = registry or SkillRegistry(cfg)

    # ------------------------------------------------------------------
    def run(
        self,
        skill_id: str,
        inputs: dict | None = None,
        *,
        dry_run: bool = False,
    ) -> SkillRunResult:
        entry = self.registry.get(skill_id) or self._discover_one(skill_id)
        if entry is None:
            return SkillRunResult(ok=False, skill_id=skill_id, error=f"未找到 Skill {skill_id!r}")
        if not entry.ok:
            return SkillRunResult(
                ok=False,
                skill_id=skill_id,
                error=f"Skill 不可用（{entry.status}）："
                + "；".join(str(i) for i in entry.validation.errors),
            )

        m = entry.manifest
        inputs = dict(inputs or {})
        for inp in m.inputs:
            if inp.type in ('papers', 'collection') and inp.key in inputs:
                value = inputs[inp.key]
                # 可选范围的空表单不代表全库；显式空列表表示不选任何文献。
                if value != '':
                    inputs['paper_ids' if inp.type == 'papers' else 'collection_id'] = value
                elif not inp.required:
                    inputs.pop(inp.key, None)

        # ---- ① 输入契约校验 ----
        missing = [i.key for i in m.inputs if i.required and not str(inputs.get(i.key, "")).strip()]
        if missing:
            return SkillRunResult(
                ok=False, skill_id=m.id, skill_version=m.version,
                error=f"缺少必填输入：{missing}",
            )

        # ---- ② 声明式上下文预取 ----
        try:
            prefetched, excerpts = self._prefetch(m, inputs)
        except Exception as e:  # noqa: BLE001
            return SkillRunResult(
                ok=False, skill_id=m.id, skill_version=m.version,
                error=f"上下文预取失败：{type(e).__name__}: {e}",
            )

        # ---- ③④ 组装提示词 ----
        system, user = self._compose(m, inputs, prefetched, excerpts)
        if dry_run:
            return SkillRunResult(
                ok=True, skill_id=m.id, skill_version=m.version,
                answer="（dry-run，未调用模型）",
                excerpts=excerpts, prefetched=prefetched,
                warnings=[f"system prompt {len(system)} 字符 / user prompt {len(user)} 字符"],
            )

        # ---- ⑤ 调用模型 ----
        from ..context.meter import Meter
        cost = sum(Meter(self.cfg).estimate_message(x) for x in [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
        if cost > self.cfg.context.available_tokens:
            return SkillRunResult(ok=False, skill_id=m.id, error='Skill 输入和原文超过上下文预算，请缩小范围。')
        if m.citation_required and not excerpts:
            return SkillRunResult(ok=False, skill_id=m.id, error="没有可引用的原文片段，拒绝生成。", prefetched=prefetched,
                                  citation_check=CitationCheck(passed=False, issues=['没有原文证据。']))
        try:
            answer = self._call_llm(m, system, user)
        except Exception as e:  # noqa: BLE001
            return SkillRunResult(
                ok=False, skill_id=m.id, skill_version=m.version,
                error=f"模型调用失败：{type(e).__name__}: {e}",
                excerpts=excerpts, prefetched=prefetched,
            )

        # ---- ⑥ 引用校验（框架执行，不依赖 Skill 作者）----
        from ..tasks import checkpoint
        checkpoint('检查 Skill 引用与保存产出')
        check = CitationCheck()
        if m.citation_required:
            check = self._check_citations(answer, excerpts)
            from ..evidence import stored_excerpts
            try:
                stored_excerpts(self.kb, [{'tag': e['tag'], 'chunk_id': e.get('id') or e.get('chunk_id'), 'paper_id': e.get('paper_id')} for e in excerpts])
            except ValueError as exc:
                check.passed = False
                check.issues.append(str(exc))

        # ---- ⑦ 落库 ----
        result = SkillRunResult(
            ok=check.passed,
            skill_id=m.id,
            skill_version=m.version,
            answer=answer,
            excerpts=excerpts,
            citation_check=check,
            prefetched=prefetched,
        )
        if m.output_type == "deliverable" and check.passed:
            result.deliverable_id = self._save_deliverable(m, inputs, answer, check, excerpts)
        if m.citation_required and not check.passed:
            result.warnings.append(
                "引用校验未通过：回答中可能存在无法追溯或虚构的来源标记，请以原文片段为准。"
            )
        return result

    # ------------------------------------------------------------------
    # 上下文预取
    # ------------------------------------------------------------------
    def _prefetch(self, m: SkillManifest, inputs: dict) -> tuple[dict, list[dict]]:
        """按 context 声明取上下文。**只调用白名单内的能力。**"""
        allowed = set(m.capabilities)
        from ..scope import paper_scope
        paper_scope(self.kb, inputs)  # 范围错误须终止，不能吞掉后继续全库预取。
        out: dict[str, Any] = {}
        excerpts: list[dict] = []

        for spec in m.context:
            cap = spec.capability
            if cap not in allowed:
                # 校验阶段已报错，这里再兜一层 —— 绝不放行未声明的能力
                continue
            if cap not in CAPABILITIES:
                continue
            args = {k: _render_value(v, inputs) for k, v in spec.args.items()}
            paper_scope(self.kb, inputs, args)
            try:
                data = self._dispatch(cap, args, inputs)
            except Exception as e:  # noqa: BLE001 - 单个能力失败不应中断整体
                out.setdefault("_errors", []).append(f"{cap}: {type(e).__name__}: {e}")
                continue
            out[cap] = data
            if cap == "search_passages":
                for item in data or []:
                    tag = f"P{len(excerpts) + 1}"
                    excerpts.append({**item, "tag": tag})
        return out, excerpts

    def _dispatch(self, cap: str, args: dict, inputs: dict) -> Any:
        from ..scope import paper_scope
        from ..search import search_local
        scope = paper_scope(self.kb, inputs, args)

        if cap == "search_passages":
            query = str(args.get("query") or inputs.get("topic") or inputs.get("focus") or "")
            top_k = int(args.get("top_k") or 8)
            if not query:
                return []
            return search_local(self.kb, query, top_k=top_k, citable_only=True, paper_ids=scope)

        if cap == "search_papers":
            query = str(args.get("query") or "")
            limit = int(args.get("limit") or 10)
            papers = self.kb.list_papers()
            if scope is not None:
                papers = [p for p in papers if p['id'] in scope]
            if query:
                q = query.lower()
                papers = [p for p in papers if q in (p.get("title") or "").lower()]
            return papers[:limit]

        if cap == "get_analysis":
            pid = args.get("paper_id") or inputs.get("paper_id")
            if not pid:
                return []
            if scope is not None and pid not in scope:
                return []
            if self.kb.get_paper(str(pid)) is None:
                return []
            a = self.kb.get_analysis(str(pid))
            return [a] if a else []

        if cap == "list_collections":
            rows = self.kb._con.execute(
                "SELECT id, name, parent_id FROM collections WHERE deleted_at IS NULL ORDER BY sort_order, name"
            ).fetchall()
            return [dict(r) for r in rows]

        if cap == "get_graph":
            from ..relations import compute_relations

            threshold = float(args.get("threshold") or 0.25)
            edges = compute_relations(self.kb, threshold=threshold, persist=False)
            return [e for e in edges if scope is None or (e['paper_a'] in scope and e['paper_b'] in scope)]

        if cap == "verify_paper":
            from ..verify import PaperVerifier
            from ..whitelist import Whitelist

            title = str(args.get("title") or inputs.get("topic") or "")
            doi = args.get("doi")
            if not title and not doi:
                return []
            v = PaperVerifier(self.cfg, Whitelist(self.kb))
            return [v.verify(title=title, doi=doi).to_dict()]

        if cap == "compare_papers":
            ids = args.get("paper_ids") or inputs.get("papers") or []
            if isinstance(ids, str):
                ids = [x.strip() for x in ids.split(",") if x.strip()]
            out = []
            for pid in ids:
                a = self.kb.get_analysis(str(pid))
                p = self.kb.get_paper(str(pid))
                if p and (scope is None or pid in scope):
                    out.append({"paper": p, "analysis": a})
            return out

        return []

    # ------------------------------------------------------------------
    # 提示词组装
    # ------------------------------------------------------------------
    def _compose(
        self,
        m: SkillManifest,
        inputs: dict,
        prefetched: dict,
        excerpts: list[dict],
    ) -> tuple[str, str]:
        """组装 system / user。

        **全局约束强制前置** —— Skill 作者无法移除或覆盖。
        """
        skill_prompt = _render_template(m.prompt, inputs)

        system = (
            SYSTEM_ACADEMIC
            + "\n\n【当前任务由 Skill 定义】\n"
            + skill_prompt
        )

        parts: list[str] = []
        ctx_text = self._render_context(prefetched, excerpts)
        if ctx_text:
            parts.append(ctx_text)
        parts.append("【用户输入】")
        for inp in m.inputs:
            val = inputs.get(inp.key)
            if val in (None, ""):
                continue
            parts.append(f"- {inp.label or inp.key}：{val}")
        if not m.inputs:
            parts.append("（无额外输入）")
        return system, "\n\n".join(parts)

    def _render_context(self, prefetched: dict, excerpts: list[dict]) -> str:
        parts: list[str] = []
        if excerpts:
            parts.append("【检索到的原文片段】")
            for e in excerpts:
                head = f"[{e['tag']}] {e.get('paper_title') or e.get('paper_id')}"
                if e.get('source_kind') and e['source_kind'] != 'paper':
                    head += f" · 人工确认的{e['source_kind']}快照 · {e.get('url') or '用户知识条目'}（不是自动核验论文）"
                if e.get("page") is not None:
                    head += f" 第 {e['page']} 页"
                if e.get("section"):
                    head += f" · {e['section']}"
                parts.append(head)
                parts.append(str(e.get("text") or ""))
                parts.append("")

        for cap, data in prefetched.items():
            if cap in ("search_passages", "_errors"):
                continue
            if not data:
                continue
            parts.append(f"【{cap} 结果】")
            parts.append(truncate(json.dumps(data, ensure_ascii=False, default=str), 4000))
            parts.append("")

        errors = prefetched.get("_errors")
        if errors:
            parts.append("【上下文预取的失败项（不要臆测其内容）】")
            parts.extend(f"- {e}" for e in errors)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def _call_llm(self, m: SkillManifest, system: str, user: str) -> str:
        if self.llm is None:
            raise RuntimeError("未配置模型")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {}
        if m.model.get("temperature") is not None:
            kwargs["temperature"] = float(m.model["temperature"])
        if m.model.get("max_tokens") is not None:
            kwargs["max_tokens"] = min(int(m.model["max_tokens"]), self.cfg.context.output_reserve_tokens)
        if hasattr(self.llm, "chat"):
            return self.llm.chat(messages, task="skill", **kwargs)
        raise RuntimeError("llm 对象缺少 chat 方法")

    # ------------------------------------------------------------------
    # 引用校验
    # ------------------------------------------------------------------
    def _check_citations(self, answer: str, excerpts: list[dict]) -> CitationCheck:
        """框架执行的引用回溯校验。规则与问答同一套。"""
        from ..evidence import citation_issues
        issues = citation_issues(answer, excerpts)
        return CitationCheck(passed=not issues, issues=issues, cited_tags=[t for t, _ in parse_citations(answer)])

    # ------------------------------------------------------------------
    def _save_deliverable(
        self,
        m: SkillManifest,
        inputs: dict,
        answer: str,
        check: CitationCheck,
        excerpts: list[dict],
    ) -> str:
        did = f"del_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO deliverables (id, type, title, skill_id, source_scope, content,"
                " citations, status, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    m.deliverable_type,
                    _derive_title(m, inputs),
                    m.id,
                    json.dumps({**inputs, 'citation_check': check.to_dict(), 'model': getattr(self.llm, 'name', 'unknown')}, ensure_ascii=False),
                    answer,
                    json.dumps(
                        [{"tag": e["tag"], "paper_id": e.get("paper_id"),
                          "page": e.get("page"), "chunk_id": e.get("id")} for e in excerpts],
                        ensure_ascii=False,
                    ),
                    "draft",
                    getattr(self.llm, 'name', m.model.get('name')),
                    now,
                    now,
                ),
            )
        return did

    def _discover_one(self, skill_id: str) -> SkillEntry | None:
        self.registry.discover()
        return self.registry.get(skill_id)


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------
def _render_template(template: str, inputs: dict) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        val = inputs.get(key)
        return str(val) if val not in (None, "") else f"（未提供 {key}）"

    return _VAR_RE.sub(repl, template or "")


def _render_value(value: Any, inputs: dict) -> Any:
    if isinstance(value, str):
        exact = _VAR_RE.fullmatch(value)
        if exact and exact.group(1) in inputs:
            return inputs[exact.group(1)]
        return _render_template(value, inputs)
    return value


def _derive_title(m: SkillManifest, inputs: dict) -> str:
    topic = inputs.get("topic") or inputs.get("focus") or inputs.get("scope") or ""
    base = m.name or m.id
    if topic:
        return f"{base} · {truncate(str(topic), 40)}"
    return f"{base} · {_now()[:16]}"


def format_result(r: SkillRunResult) -> str:
    if not r.ok:
        return f"❌ Skill {r.skill_id} 执行失败：{r.error}"
    lines = [f"✅ Skill {r.skill_id} v{r.skill_version} 执行完成"]
    if r.deliverable_id:
        lines.append(f"  交付物：{r.deliverable_id}")
    if r.excerpts:
        lines.append(f"  引用片段：{len(r.excerpts)} 段")
    if r.citation_check.cited_tags:
        lines.append(f"  引用标记：{'、'.join(r.citation_check.cited_tags)}")
    lines.append(
        f"  引用校验：{'通过' if r.citation_check.passed else '未通过'}"
    )
    for issue in r.citation_check.issues:
        lines.append(f"    · {issue}")
    for w in r.warnings:
        lines.append(f"  ⚠️ {w}")
    return "\n".join(lines)
