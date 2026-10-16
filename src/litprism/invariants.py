"""不变量校验器。

**设计原则**：校验逻辑做成**可独立运行的服务**，而不是散落在业务代码里的 ``if``。
这样它能被单测、被载入期调用、被提交前调用、被审计脚本调用。

两类校验：

* :func:`check_compaction` —— 压缩提交前的 9 条核心不变量
* :func:`check_config` —— 配置载入期的 fail-fast 清单

**错误信息要求**：说清「哪条规则、哪个 seq、为什么、怎么修」。
一句 ``invalid input`` 对排查毫无价值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .refs import RefError, has_duplicates, is_contiguous, make_ref, parse_ref, safe_ref

# ---------------------------------------------------------------------------
# 压缩失败码（闭集）
# ---------------------------------------------------------------------------
FAILURE_CODES: dict[str, str] = {
    "busy": "本会话有正在进行的压缩，请等它结束或取消。",
    "cancelled": "压缩已取消，未改动任何历史。",
    "changed": "选中的历史在替换前发生了变化，本次尝试已记录。",
    "summary": "无法产出有用摘要（或未通过收缩验证），本次尝试已记录。",
    "commit": "压缩未干净完成，部分历史可能已变，重试前请检查状态。",
    "overflow_limit": "溢出恢复已达重试上限。",
    "invariant": "提交前不变量校验未通过，已阻止本次压缩。",
}


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------
@dataclass
class Violation:
    code: str
    message: str
    where: str = ""
    severity: str = "error"      # error | warning

    def __str__(self) -> str:
        loc = f" @ {self.where}" if self.where else ""
        mark = "✗" if self.severity == "error" else "!"
        return f"{mark} [{self.code}]{loc} {self.message}"


@dataclass
class CheckResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    def add(self, code: str, message: str, where: str = "", severity: str = "error") -> None:
        self.violations.append(Violation(code, message, where, severity))

    def raise_if_failed(self) -> None:
        if not self.ok:
            detail = "\n  ".join(str(v) for v in self.errors)
            raise InvariantError(f"不变量校验未通过：\n  {detail}")

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "全部通过"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} 项错误")
        if self.warnings:
            parts.append(f"{len(self.warnings)} 项警告")
        return "、".join(parts)


class InvariantError(RuntimeError):
    """不变量被破坏。"""


# ---------------------------------------------------------------------------
# 压缩上下文
# ---------------------------------------------------------------------------
@dataclass
class MessageView:
    """区间内的一条可见消息（校验所需的最小字段）。"""

    seq: int
    ref: str
    role: str
    has_citations: bool = False
    citation_check_passed: bool | None = None


@dataclass
class CompactContext:
    """提交前校验所需的全部事实。字段由调用方从数据库组装。"""

    compaction_id: str
    source_command: str
    conversation_id: str
    start_seq: int
    end_seq: int
    shadowed_refs: list[str]
    messages: list[MessageView]
    # 已写入的替换事件（若尚未写入则为 None —— 用于 "提交前" 与 "提交后" 两种校验）
    replacement_refs: list[str] | None = None
    # 事务状态
    open_tx_exists: bool = False
    has_summary: bool = False
    has_end: bool = False
    # 第 9 条：引用校验结论
    citation_before: dict[str, Any] | None = None
    citation_after: dict[str, Any] | None = None
    # 可选：重跑引用校验的回调（用于第 9 条的强校验）
    recheck_citations: Callable[[], dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# 9 条核心不变量
# ---------------------------------------------------------------------------
def check_compaction(ctx: CompactContext) -> CheckResult:
    """校验一次压缩提交。返回全部违规项（不短路），便于一次性给出完整诊断。"""
    r = CheckResult()

    # ---- 1. compactionId / sourceCommand 必须非空 ----
    if not (ctx.compaction_id or "").strip():
        r.add("inv1.id", "compactionId 为空。每次压缩必须有唯一标识，否则无法在日志中定位。", "compaction_id")
    if not (ctx.source_command or "").strip():
        r.add("inv1.source", "sourceCommand 为空。必须记录触发来源（nudge/pressure/manual/overflow）。", "source_command")
    elif ctx.source_command not in ("nudge", "pressure", "manual", "overflow"):
        r.add(
            "inv1.source",
            f"未知触发来源 {ctx.source_command!r}。允许值：nudge / pressure / manual / overflow。",
            "source_command",
            severity="warning",
        )

    # ---- 2. 所有 seq 必须是非负安全整数 ----
    MAX_SAFE_INT = 2**53 - 1
    all_seqs = [ctx.start_seq, ctx.end_seq] + [m.seq for m in ctx.messages]
    for s in all_seqs:
        if not isinstance(s, int) or isinstance(s, bool):
            r.add("inv2.seq_type", f"seq 必须是整数，收到 {type(s).__name__}（值 {s!r}）。", f"seq={s!r}")
        elif s < 0:
            r.add("inv2.seq_negative", f"seq 不能为负，收到 {s}。", f"seq={s}")
        elif s > MAX_SAFE_INT:
            r.add("inv2.seq_range", f"seq 超出安全整数范围（>{MAX_SAFE_INT}），收到 {s}。", f"seq={s}")

    if ctx.start_seq > ctx.end_seq:
        r.add(
            "inv2.range_order",
            f"区间起止写反：start={ctx.start_seq} > end={ctx.end_seq}。"
            "refs.parse_range() 会自动纠正，但落库前必须是规范形态。",
            f"{safe_ref(ctx.start_seq)}-{safe_ref(ctx.end_seq)}",
        )

    # ---- 3. shadowedRefs 非空，且首/末等于区间端点 ----
    ref_seqs: list[int] = []
    if not ctx.shadowed_refs:
        r.add("inv3.empty", "shadowedRefs 为空。压缩必须明确列出被遮蔽的消息，否则无法审计。", "shadowed_refs")
    else:
        bad: list[str] = []
        for x in ctx.shadowed_refs:
            try:
                ref_seqs.append(parse_ref(x))
            except RefError:
                bad.append(repr(x))
        if bad:
            r.add(
                "inv3.parse",
                f"shadowedRefs 含 {len(bad)} 个无法解析的引用：{'、'.join(bad[:5])}"
                f"{'（仅列前 5 个）' if len(bad) > 5 else ''}。",
                "shadowed_refs",
            )
            ref_seqs = []
        elif ref_seqs:
            first, last = ref_seqs[0], ref_seqs[-1]
            if first != ctx.start_seq:
                r.add(
                    "inv3.head",
                    f"shadowedRefs 首项 {ctx.shadowed_refs[0]!r}（seq={first}）"
                    f"与区间起点 {safe_ref(ctx.start_seq)} 不一致。",
                    "shadowed_refs[0]",
                )
            if last != ctx.end_seq:
                r.add(
                    "inv3.tail",
                    f"shadowedRefs 末项 {ctx.shadowed_refs[-1]!r}（seq={last}）"
                    f"与区间终点 {safe_ref(ctx.end_seq)} 不一致。",
                    "shadowed_refs[-1]",
                )

    # ---- 4. shadowedRefs 必须完整、按序、连续枚举区间内每一个可见节点 ----
    if ref_seqs and ctx.messages:
        visible = [m.seq for m in ctx.messages if isinstance(m.seq, int) and ctx.start_seq <= m.seq <= ctx.end_seq]
        if has_duplicates(ref_seqs):
            dupes = sorted({s for s in ref_seqs if ref_seqs.count(s) > 1})
            r.add(
                "inv4.duplicate",
                f"shadowedRefs 含重复项：{[safe_ref(s) for s in dupes]}。每条消息只能被遮蔽一次。",
                "shadowed_refs",
            )
        if ref_seqs != sorted(ref_seqs):
            r.add("inv4.order", "shadowedRefs 未按 seq 升序排列。", "shadowed_refs")
        missing = sorted(set(visible) - set(ref_seqs))
        extra = sorted(set(ref_seqs) - set(visible))
        if missing:
            r.add(
                "inv4.missing",
                f"区间内可见消息未被完整枚举，缺失 {[safe_ref(s) for s in missing]}。"
                "漏列会导致这些消息既不在上下文里、也不在摘要覆盖范围内。",
                "shadowed_refs",
            )
        if extra:
            r.add(
                "inv4.extra",
                f"shadowedRefs 含区间外的消息 {[safe_ref(s) for s in extra]}。",
                "shadowed_refs",
            )
        if not is_contiguous(ref_seqs) and not missing and not extra:
            r.add("inv4.gap", "shadowedRefs 不连续（存在空洞）。", "shadowed_refs", severity="warning")

    # ---- 5. 替换事件必须引用每一个被遮蔽的可见节点 ----
    if ctx.replacement_refs is not None:
        try:
            repl = {parse_ref(x) for x in ctx.replacement_refs}
            shadow = {parse_ref(x) for x in ctx.shadowed_refs}
        except RefError as e:
            r.add("inv5.parse", f"替换事件引用含无法解析项：{e}", "replacement_refs")
        else:
            uncovered = sorted(shadow - repl)
            if uncovered:
                r.add(
                    "inv5.uncovered",
                    f"替换事件未覆盖被遮蔽的消息 {[safe_ref(s) for s in uncovered]}。"
                    "这会形成「血统断链」—— 那些内容既不可见也无法从摘要追溯。",
                    "replacement_refs",
                )

    # ---- 6. 不得切断「用户提问 → 助手回答」配对 ----
    #      （本项目没有 tool-call，与之等价的是问答配对）
    #      仅在 seq 全部合法时才做顺序检查，否则交给 inv2 报告
    seqs_valid = all(isinstance(m.seq, int) and not isinstance(m.seq, bool) for m in ctx.messages)
    if ctx.messages and seqs_valid:
        ordered = sorted([m for m in ctx.messages if ctx.start_seq <= m.seq <= ctx.end_seq], key=lambda x: x.seq)
        for i, m in enumerate(ordered):
            if m.role == "assistant" and i == 0:
                # 区间以 assistant 开头 —— 它的提问在区间外，检查是否被压掉
                r.add(
                    "inv6.orphan_answer",
                    f"区间以助手回答 {m.ref} 开头，其对应的用户提问在区间之外。"
                    "切断问答配对会让摘要里出现「没有问题的答案」。",
                    m.ref,
                    severity="warning",
                )
            if m.role == "user" and i == len(ordered) - 1 and i > 0:
                r.add(
                    "inv6.orphan_question",
                    f"区间以用户提问 {m.ref} 结尾，其回答在区间之外。"
                    "保留最近提问的答案更符合直觉，建议把切口后移。",
                    m.ref,
                    severity="warning",
                )

    # ---- 7. 已有未闭合事务时不得再开一个 ----
    if ctx.open_tx_exists:
        r.add(
            "inv7.concurrent",
            "本会话已存在未闭合的压缩事务。并发压缩会造成区间重叠与血统混乱，已阻止。",
            ctx.conversation_id,
        )

    # ---- 8. 成功的 end 必须恰好伴随一个 summary ----
    if ctx.has_end and not ctx.has_summary:
        r.add(
            "inv8.no_summary",
            "压缩已提交（end）但没有对应的 summary。这会产生一个「内容消失且无替代」的区间。",
            ctx.compaction_id,
        )
    if ctx.has_summary and not ctx.has_end:
        r.add(
            "inv8.unclosed",
            "已有 summary 但事务未闭合（缺 end）。崩溃恢复时需要能识别并清理这种半成品。",
            ctx.compaction_id,
            severity="warning",
        )

    # ---- 9. 【LitPrism 特有】压缩不得改变引用校验的结论 ----
    before, after = ctx.citation_before, ctx.citation_after
    if after is None and ctx.recheck_citations is not None:
        after = ctx.recheck_citations()
    if before is not None and after is not None:
        if not _citation_verdict_equal(before, after):
            r.add(
                "inv9.citation_drift",
                "压缩改变了引用校验的结论。压缩只能改变「原文在不在上下文里」，"
                "不能改变「引用是否成立」—— 否则压缩就成了绕过防幻觉的通道。",
                ctx.compaction_id,
            )

    return r


def _citation_verdict_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """比较两次引用校验的「结论」，忽略与压缩无关的呈现细节。"""

    def norm(d: dict[str, Any]) -> tuple:
        if not isinstance(d, dict):
            return (repr(d),)
        passed = d.get("passed", d.get("citation_check_passed"))
        issues = d.get("issues", d.get("citation_issues")) or []
        # 只比较「通过与否」与「问题条目集合」，不比较顺序与文案细节
        return (bool(passed), tuple(sorted(str(i) for i in issues)))

    return norm(a) == norm(b)


# ---------------------------------------------------------------------------
# 配置 fail-fast 清单
# ---------------------------------------------------------------------------
@dataclass
class ContextConfigView:
    """上下文管理相关配置的最小视图（避免与 config.py 循环依赖）。"""

    window_tokens: int = 32000
    step_tokens: int = 30000
    retain_tokens: int | None = None
    retain_ratio: float | None = None
    nudge_ratio: float = 0.75
    emergency_ratio: float = 0.95
    output_reserve_tokens: int = 4096
    overflow_retries: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


KNOWN_CONFIG_KEYS = {
    "window_tokens", "step_tokens", "retain_tokens", "retain_ratio",
    "nudge_ratio", "emergency_ratio", "output_reserve_tokens", "overflow_retries",
}


def check_config(cfg: ContextConfigView) -> CheckResult:
    """配置载入期校验。

    **区分两类**（这是配置设计的核心判断）：

    * **必须拒绝**：会让系统进入自相矛盾状态的组合（如保留预算 ≥ 触发阈值）
    * **只警告**：未知键、可选字段畸形 —— 静默丢弃 + 一次警告，不阻断启动
    """
    r = CheckResult()

    # 硬拒绝：互斥字段同时给出
    if cfg.retain_tokens is not None and cfg.retain_ratio is not None:
        r.add(
            "cfg.mutex",
            "retain_tokens 与 retain_ratio 互斥，不能同时指定。"
            "请二选一：按绝对 token 数或按窗口比例。",
            "retain_tokens / retain_ratio",
        )

    # 硬拒绝：预算算式为负
    if cfg.window_tokens <= 0:
        r.add("cfg.window", f"window_tokens 必须为正，收到 {cfg.window_tokens}。", "window_tokens")
    else:
        budget = cfg.window_tokens - cfg.output_reserve_tokens
        if budget <= 0:
            r.add(
                "cfg.budget",
                f"预算算式 W - O - B <= 0：window={cfg.window_tokens} - "
                f"output_reserve={cfg.output_reserve_tokens} = {budget}。"
                "输出预留已占满窗口，无法组装任何上下文。",
                "window_tokens / output_reserve_tokens",
            )

    # 硬拒绝：保留预算 ≥ 触发阈值（会导致「压缩了但还是超」）
    if cfg.retain_tokens is not None and cfg.retain_tokens >= cfg.step_tokens:
        r.add(
            "cfg.retain_vs_step",
            f"retain_tokens（{cfg.retain_tokens}）≥ step_tokens（{cfg.step_tokens}）。"
            "保留的比触发步长还多，压缩后立刻会再次触发，形成抖动。",
            "retain_tokens / step_tokens",
        )
    if cfg.retain_ratio is not None and cfg.retain_ratio >= cfg.nudge_ratio:
        r.add(
            "cfg.retain_vs_nudge",
            f"retain_ratio（{cfg.retain_ratio}）≥ nudge_ratio（{cfg.nudge_ratio}）。"
            "保留比例必须小于触发比例，否则压缩无意义。",
            "retain_ratio / nudge_ratio",
        )

    # 硬拒绝：非正数
    if cfg.overflow_retries <= 0:
        r.add(
            "cfg.retries",
            f"overflow_retries 必须为正，收到 {cfg.overflow_retries}。"
            "为 0 意味着窗口溢出后完全没有恢复路径。",
            "overflow_retries",
        )
    if cfg.step_tokens <= 0:
        r.add("cfg.step", f"step_tokens 必须为正，收到 {cfg.step_tokens}。", "step_tokens")

    # 警告：比例区间（nudge >= emergency 会让增长层形同虚设，但系统仍可运行，故不拒绝启动）
    if not (0 < cfg.nudge_ratio < cfg.emergency_ratio <= 1.0):
        r.add(
            "cfg.ratios",
            f"比例区间不合理：nudge={cfg.nudge_ratio}, emergency={cfg.emergency_ratio}。"
            "要求 0 < nudge < emergency <= 1；当前设置下增长层可能永远轮不到触发。",
            "nudge_ratio / emergency_ratio",
            severity="warning",
        )

    # 警告：未知键（静默丢弃，不阻断启动）
    unknown = set(cfg.extra) - KNOWN_CONFIG_KEYS
    for k in sorted(unknown):
        r.add("cfg.unknown_key", f"未知配置键 {k!r}，已忽略。", k, severity="warning")

    return r


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def validate_or_raise_compaction(ctx: CompactContext) -> CheckResult:
    r = check_compaction(ctx)
    r.raise_if_failed()
    return r


def validate_or_raise_config(cfg: ContextConfigView) -> CheckResult:
    r = check_config(cfg)
    r.raise_if_failed()
    return r


def describe_rules() -> list[tuple[str, str]]:
    """给文档与设置页用的规则清单。"""
    return [
        ("inv1", "compactionId 与 sourceCommand 必须非空"),
        ("inv2", "所有 seq 必须是非负安全整数，区间起止不得写反"),
        ("inv3", "shadowedRefs 非空，且首/末等于区间端点"),
        ("inv4", "shadowedRefs 完整、按序、连续枚举区间内每个可见节点"),
        ("inv5", "替换事件必须引用每一个被遮蔽的可见节点"),
        ("inv6", "不得切断「用户提问 → 助手回答」配对"),
        ("inv7", "已有未闭合事务时不得再开一个"),
        ("inv8", "成功的 end 必须恰好伴随一个 summary"),
        ("inv9", "【LitPrism】压缩不得改变引用校验的结论"),
    ]


def format_result(r: CheckResult, indent: str = "  ") -> str:
    if not r.violations:
        return f"{indent}✅ 全部通过"
    return "\n".join(f"{indent}{v}" for v in r.violations)
