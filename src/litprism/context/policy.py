"""策略（Policy）—— 决定「何时压」与「压哪一段」。

核心判据
--------
**在「新增量」上动手，而不是在「窗口位置」上动手。**

* 窗口位置 = 阈值到了就重写历史 → 前缀作废、损失随轮次复利衰减、单粒度、模型无参与。
* 新增量 = 只压「上次压缩之后新增、且已被任务消费掉」的部分 → 前缀不变、
  每份内容一生只折一次、可分级、模型可参与。

LitPrism 取后者，而且我们的场景有天然优势：**"已消费"的边界清晰可判** ——
一轮问答结束后，那一轮检索到的原文片段就已消费完。

三层触发（互相独立）
--------------------
1. **增长层**（日常主力）：绝对 token 步长，与窗口大小解耦。
   若只按百分比，首次压缩的绝对位置 ≈ 开机占用 + 一个步长，窗口越大触发越晚。
2. **压力层**（兜底）：窗口百分比。
3. **人工**（常备逃生门）：``/compact``。

三个不可妥协的选择约束
----------------------
1. **system message 永不遮蔽**（遮蔽它等于重写整个前缀）
2. **绝不切断「用户提问 → 助手回答」配对**
3. **找不到安全段就返回 null，什么都不做** —— 静默 no-op 比"压出一堆碎片"好得多
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config, ContextConfig
from ..refs import make_ref, safe_ref
from .meter import Measurement, NodeCost

# 触发来源（与 invariants.FAILURE_CODES 之外的一套，表示"为什么压"）
TRIGGERS = ("nudge", "pressure", "manual", "overflow")


@dataclass
class TriggerDecision:
    should_compact: bool
    trigger: str | None = None
    reason: str = ""
    emergency_truncate: bool = False
    metrics: dict = field(default_factory=dict)

    def describe(self) -> str:
        if not self.should_compact:
            return f"不压缩：{self.reason}"
        extra = "（含硬截断）" if self.emergency_truncate else ""
        return f"触发压缩 [{self.trigger}]{extra}：{self.reason}"


@dataclass
class SelectionResult:
    ok: bool
    start_seq: int | None = None
    end_seq: int | None = None
    shadowed_refs: list[str] = field(default_factory=list)
    shadowed_tokens: int = 0
    retained_tokens: int = 0
    reason: str = ""
    noop_reason: str = ""
    protected_snapshot: dict[int, str] = field(default_factory=dict)

    @property
    def range_text(self) -> str:
        if not self.ok or self.start_seq is None or self.end_seq is None:
            return "—"
        return f"{safe_ref(self.start_seq)}-{safe_ref(self.end_seq)}"

    def describe(self) -> str:
        if not self.ok:
            return f"无可压缩区间（{self.noop_reason}）"
        return (
            f"候选区间 {self.range_text}：遮蔽 {len(self.shadowed_refs)} 条 / "
            f"{self.shadowed_tokens} token，保留 {self.retained_tokens} token"
        )


class Policy:
    """触发与选择策略。"""

    def __init__(self, cfg: Config | ContextConfig | None = None):
        if isinstance(cfg, Config):
            self.cfg = cfg.context
        elif isinstance(cfg, ContextConfig):
            self.cfg = cfg
        else:
            self.cfg = ContextConfig()

    # ------------------------------------------------------------------
    # 受保护区
    # ------------------------------------------------------------------
    def protected_zones(self, measurement: Measurement, messages: list[dict]) -> dict[int, str]:
        """组装完整受保护区。返回 ``{seq: 理由}``。

        LitPrism 的特化在最后两条 —— 它们直接服务于防幻觉的可追溯性。
        """
        zones: dict[int, str] = {}

        for n in measurement.nodes:
            if n.role == "system":
                zones[n.seq] = "系统提示词（前缀必须逐字节稳定）"
            if n.kind == "compact-checkpoint":
                zones[n.seq] = "压缩检查点（它装着摘要本身）"

        users = [m["seq"] for m in messages if m.get("role") == "user" and isinstance(m.get("seq"), int)]
        if users:
            zones[users[0]] = "第一条用户消息（研究意图，丢了整段会话失去锚）"
            zones[users[-1]] = "最后一条用户消息（当前指令）"

        seqs = sorted(m["seq"] for m in messages if isinstance(m.get("seq"), int))
        for s in seqs[-self.cfg.recent_turns_protected :]:
            zones.setdefault(s, f"最近 {self.cfg.recent_turns_protected} 条消息（活跃工作集）")

        # ---- LitPrism 特化 1：当前轮引用的片段所在消息 ----
        for m in messages[-2:]:
            if m.get("citations"):
                s = m.get("seq")
                if isinstance(s, int):
                    zones[s] = "当前轮的引用片段（正在被引用的证据）"

        # ---- LitPrism 特化 2：交付物生成中的源范围 ----
        # 生成中的综述/对比报告，其依据不能被折掉。由调用方通过 extra_protected 注入。
        return zones

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------
    def decide_trigger(
        self,
        measurement: Measurement,
        *,
        anchor_seq: int = 0,
        messages: list[dict] | None = None,
        protected: dict[int, str] | None = None,
        summary_quality_ok: bool = True,
        force: str | None = None,
    ) -> TriggerDecision:
        """三层触发判定。

        ``force`` 用于人工与溢出路径（``'manual'`` / ``'overflow'``），会绕过增长门控。
        """
        messages = messages or []
        protected = protected or {}
        cfg = self.cfg
        pressure = measurement.pressure

        metrics = {
            "surface_tokens": measurement.surface_tokens,
            "total_tokens": measurement.total_tokens,
            "window_tokens": measurement.window_tokens,
            "pressure": pressure,
            "anchor_seq": anchor_seq,
        }

        # ---- 人工 / 溢出：绕过常规阈值 ----
        if force in ("manual", "overflow"):
            emergency = force == "overflow"
            metrics["incremental_tokens"] = self._incremental_tokens(messages, anchor_seq, protected)
            return TriggerDecision(
                should_compact=True,
                trigger=force,
                reason="用户手动触发" if force == "manual" else "窗口溢出恢复路径",
                emergency_truncate=emergency,
                metrics=metrics,
            )

        # ---- 层 2：压力层（先判，因为它是硬约束）----
        if pressure >= cfg.emergency_ratio:
            metrics["incremental_tokens"] = self._incremental_tokens(messages, anchor_seq, protected)
            return TriggerDecision(
                should_compact=True,
                trigger="pressure",
                reason=(
                    f"发送视图 {measurement.surface_tokens} token 已达窗口的 "
                    f"{pressure:.0%}（≥ {cfg.emergency_ratio:.0%} 紧急线）"
                ),
                emergency_truncate=True,
                metrics=metrics,
            )
        if pressure >= cfg.nudge_ratio:
            metrics["incremental_tokens"] = self._incremental_tokens(messages, anchor_seq, protected)
            return TriggerDecision(
                should_compact=True,
                trigger="pressure",
                reason=(
                    f"发送视图 {measurement.surface_tokens} token 达窗口的 "
                    f"{pressure:.0%}（≥ {cfg.nudge_ratio:.0%} 触发线）"
                ),
                metrics=metrics,
            )

        # ---- 层 1：增长层（绝对步长，与窗口解耦）----
        incremental = self._incremental_tokens(messages, anchor_seq, protected)
        gate = self.growth_gate()
        metrics["incremental_tokens"] = incremental
        metrics["growth_gate"] = gate

        if incremental < gate:
            return TriggerDecision(
                should_compact=False,
                reason=f"自锚点 {safe_ref(anchor_seq)} 以来可压缩增量 {incremental} token < 门控 {gate}",
                metrics=metrics,
            )
        if not summary_quality_ok:
            return TriggerDecision(
                should_compact=False,
                reason=f"增量 {incremental} token 已达门控 {gate}，但摘要质量未达标，本次跳过",
                metrics=metrics,
            )
        return TriggerDecision(
            should_compact=True,
            trigger="nudge",
            reason=f"自锚点 {safe_ref(anchor_seq)} 以来可压缩增量 {incremental} token ≥ 门控 {gate}",
            metrics=metrics,
        )

    def growth_gate(self) -> int:
        """增长门控阈值。

        ``max(12000, 0.45 * step)``：步长本身设下限，避免用户把 step 调得很小时
        门控退化成"每两条消息就压一次"。
        """
        return max(12000, int(0.45 * self.cfg.step_tokens))

    def _incremental_tokens(
        self, messages: list[dict], anchor_seq: int, protected: dict[int, str]
    ) -> int:
        """自锚点以来**新增、且已被消费掉**的 token。

        "已被消费" = 不在受保护区内 —— 保护区里的内容是活跃工作集，还不该折。
        """
        from .meter import Meter

        meter = Meter(self.cfg)
        total = 0
        for m in messages:
            seq = m.get("seq")
            if not isinstance(seq, int) or seq <= anchor_seq:
                continue
            if seq in protected:
                continue
            total += meter.estimate_message(m)
        return total

    # ------------------------------------------------------------------
    # 选择
    # ------------------------------------------------------------------
    def select(
        self,
        measurement: Measurement,
        *,
        messages: list[dict] | None = None,
        extra_protected: dict[int, str] | None = None,
        aggressive: bool = False,
    ) -> SelectionResult:
        """选出可压缩区间。找不到就返回 ``ok=False``（静默 no-op）。

        ``aggressive=True``（溢出恢复）时把保留预算减半 —— 但仍不碰受保护区。
        """
        messages = messages or []
        nodes = measurement.nodes
        if not nodes:
            return SelectionResult(ok=False, noop_reason="上下文为空")

        zones = self.protected_zones(measurement, messages)
        # 合并测量阶段已标记的受保护项（meter 的默认保护与 policy 的完整保护可能有细微差异，
        # 取并集更安全 —— 漏保护比多保护危险得多）
        for n in nodes:
            if n.protected:
                zones.setdefault(n.seq, n.protected_reason or "受保护")
        if extra_protected:
            zones.update(extra_protected)

        def compactable(n: NodeCost) -> bool:
            # 三条硬约束：system 不折、checkpoint 不折、受保护区不折
            if n.role == "system":
                return False
            if n.kind == "compact-checkpoint":
                return False
            if n.seq in zones:
                return False
            return True

        # ---- 1. 从头部找第一个可压缩节点 ----
        start = 0
        while start < len(nodes) and not compactable(nodes[start]):
            start += 1
        if start >= len(nodes):
            return SelectionResult(
                ok=False,
                noop_reason="没有可压缩的消息（全部受保护或为系统/摘要节点）",
                protected_snapshot=zones,
            )

        # ---- 2. 连续可压缩段的上界 ----
        run_end = start
        while run_end < len(nodes) and compactable(nodes[run_end]):
            run_end += 1

        # ---- 3. 尾部保留：预算位置与受保护区起点取较早者 ----
        retain = self.cfg.effective_retain_tokens
        if aggressive:
            retain = max(1, retain // 2)

        acc = 0
        budget_from = len(nodes)
        while budget_from > 0 and acc < retain:
            budget_from -= 1
            acc += nodes[budget_from].tokens

        protected_from = len(nodes)
        for i in range(len(nodes) - 1, -1, -1):
            if nodes[i].protected or nodes[i].seq in zones:
                protected_from = i
            else:
                break

        keep_from = min(budget_from, protected_from)

        # ---- 4. 安全切口：切口处必须是 user（新轮开始），避免孤儿提问 ----
        while keep_from > start and keep_from < len(nodes) and nodes[keep_from].role != "user":
            keep_from -= 1

        if keep_from <= start:
            # 区分两种 no-op：预算覆盖全部 vs 切口不安全。前者是正常状态，后者需要人看。
            if budget_from <= start:
                reason = (
                    f"保留预算 {retain} token 已覆盖整个上下文，无需压缩"
                    "（这是正常状态，不是错误）"
                )
            else:
                reason = "找不到安全切口（继续回退会切到受保护区或切断问答配对）"
            return SelectionResult(
                ok=False,
                noop_reason=reason,
                protected_snapshot=zones,
            )

        # ---- 5. 区间不得越过连续可压缩段 ----
        end_exclusive = min(keep_from, run_end)
        if end_exclusive <= start:
            return SelectionResult(
                ok=False,
                noop_reason="可压缩段与保留段之间没有空间",
                protected_snapshot=zones,
            )

        candidate = nodes[start:end_exclusive]
        if len(candidate) < 2:
            return SelectionResult(
                ok=False,
                noop_reason=f"候选区间只有 {len(candidate)} 条消息，压缩不划算",
                protected_snapshot=zones,
            )

        shadowed_tokens = sum(n.tokens for n in candidate)
        retained_tokens = sum(n.tokens for n in nodes[end_exclusive:])

        return SelectionResult(
            ok=True,
            start_seq=candidate[0].seq,
            end_seq=candidate[-1].seq,
            shadowed_refs=[n.ref for n in candidate],
            shadowed_tokens=shadowed_tokens,
            retained_tokens=retained_tokens,
            reason=(
                f"遮蔽 {len(candidate)} 条（{shadowed_tokens} token），"
                f"保留 {len(nodes) - end_exclusive} 条（{retained_tokens} token）"
            ),
            protected_snapshot=zones,
        )

    # ------------------------------------------------------------------
    # 溢出恢复
    # ------------------------------------------------------------------
    def overflow_recovery(self, retries_used: int) -> tuple[bool, str]:
        """窗口溢出后的恢复路径判定。

        参考实现的经验：**只在视图确实前进后重试**，且限次。
        无限重试会在"压缩无效"时形成死循环。
        """
        if retries_used >= self.cfg.overflow_retries:
            return False, (
                f"溢出恢复已达重试上限（{self.cfg.overflow_retries} 次）。"
                "继续重试不会改变结果，请检查是否有单条消息本身超出窗口。"
            )
        return True, f"允许第 {retries_used + 1} 次溢出恢复（上限 {self.cfg.overflow_retries}）"


# ---------------------------------------------------------------------------
# 呈现
# ---------------------------------------------------------------------------
def format_decision(d: TriggerDecision) -> str:
    return d.describe()


def format_selection(s: SelectionResult) -> str:
    lines = [s.describe()]
    if s.ok:
        lines.append(f"  遮蔽清单：{', '.join(s.shadowed_refs)}")
    if s.protected_snapshot:
        lines.append("  受保护区：")
        for seq, reason in sorted(s.protected_snapshot.items()):
            lines.append(f"    {safe_ref(seq)}  {reason}")
    return "\n".join(lines)
