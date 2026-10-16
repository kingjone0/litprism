"""Policy（触发 + 选择）测试。

覆盖三件事：
1. 三层触发各自独立生效（增长层用**绝对步长**，与窗口解耦）；
2. 受保护区真的受保护（system / 首末用户消息 / 最近 N 条 / 引用片段）；
3. 找不到安全切口时**静默 no-op**，而不是压出一堆碎片。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import ContextConfig  # noqa: E402
from litprism.context.meter import Meter  # noqa: E402
from litprism.context.policy import Policy  # noqa: E402
from litprism.refs import make_ref  # noqa: E402


def _msg(seq: int, role: str, content: str, **over) -> dict:
    d = {"seq": seq, "ref": make_ref(seq), "role": role, "content": content, "kind": "normal"}
    d.update(over)
    return d


def _cfg(**over) -> ContextConfig:
    """测试用配置。

    ``retain_tokens`` 必须显著小于会话规模，否则保留预算会覆盖整个上下文，
    压缩自然无对象（那是正确行为，但测不出选择逻辑）。
    """
    base = dict(
        window_tokens=200000,
        step_tokens=30000,
        retain_tokens=6000,          # ≈ 8 条消息
        recent_turns_protected=2,
        output_reserve_tokens=4096,
    )
    base.update(over)
    return ContextConfig(**base)


def _conversation(turns: int = 8, size: int = 400) -> list[dict]:
    """构造 system + N 轮问答。"""
    msgs = [_msg(1, "system", "你是学术助手。" * 5)]
    seq = 2
    for i in range(turns):
        msgs.append(_msg(seq, "user", f"第{i}个问题。" * size))
        seq += 1
        msgs.append(_msg(seq, "assistant", f"第{i}个回答。" * size))
        seq += 1
    return msgs


def _measure(messages, cfg, blocks=()):
    return Meter(cfg).measure_nodes(messages, blocks)


# ===========================================================================
# 触发
# ===========================================================================
class TestTrigger:
    def test_no_trigger_when_fresh(self):
        cfg = _cfg(step_tokens=30000)
        msgs = _conversation(turns=1, size=10)
        p = Policy(cfg)
        d = p.decide_trigger(_measure(msgs, cfg), anchor_seq=0, messages=msgs)
        assert d.should_compact is False
        assert "增量" in d.reason

    def test_growth_gate_uses_absolute_step_not_percentage(self):
        """增长层必须与窗口大小解耦：窗口放大 10 倍，触发位置不该跟着放大 10 倍。"""
        small = ContextConfig(window_tokens=60000, step_tokens=30000)
        huge = ContextConfig(window_tokens=600000, step_tokens=30000)
        msgs = _conversation(turns=6, size=600)

        d_small = Policy(small).decide_trigger(
            _measure(msgs, small), anchor_seq=0, messages=msgs,
            protected=Meter(small)._default_protected(msgs)[0] and Policy(small).protected_zones(_measure(msgs, small), msgs),
        )
        d_huge = Policy(huge).decide_trigger(
            _measure(msgs, huge), anchor_seq=0, messages=msgs,
            protected=Policy(huge).protected_zones(_measure(msgs, huge), msgs),
        )
        # 两者判定应一致 —— 因为触发依据是绝对增量，不是窗口比例
        assert d_small.should_compact == d_huge.should_compact

    def test_growth_gate_floor(self):
        """步长很小时门控不该退化成"每两条消息压一次"。"""
        p = Policy(ContextConfig(step_tokens=1000))
        assert p.growth_gate() == 12000

    def test_pressure_trigger_at_nudge_ratio(self):
        cfg = _cfg(window_tokens=1000, output_reserve_tokens=100, nudge_ratio=0.2, emergency_ratio=0.95, step_tokens=10**9)
        msgs = [_msg(1, "system", "s"), _msg(2, "user", "内容" * 200)]
        meas = _measure(msgs, cfg)
        d = Policy(cfg).decide_trigger(meas, anchor_seq=0, messages=msgs)
        assert d.should_compact and d.trigger == "pressure"

    def test_emergency_triggers_hard_truncate(self):
        cfg = _cfg(window_tokens=400, output_reserve_tokens=10, emergency_ratio=0.9, step_tokens=10**9)
        msgs = [_msg(1, "system", "s"), _msg(2, "user", "内容" * 400)]
        d = Policy(cfg).decide_trigger(_measure(msgs, cfg), anchor_seq=0, messages=msgs)
        assert d.should_compact and d.emergency_truncate is True

    def test_manual_bypasses_gates(self):
        cfg = _cfg(step_tokens=30000)
        msgs = _conversation(turns=1, size=10)
        d = Policy(cfg).decide_trigger(_measure(msgs, cfg), anchor_seq=0, messages=msgs, force="manual")
        assert d.should_compact and d.trigger == "manual"

    def test_overflow_marks_emergency(self):
        cfg = _cfg()
        msgs = _conversation(turns=1, size=10)
        d = Policy(cfg).decide_trigger(_measure(msgs, cfg), anchor_seq=0, messages=msgs, force="overflow")
        assert d.should_compact and d.trigger == "overflow" and d.emergency_truncate

    def test_skips_when_summary_quality_bad(self):
        cfg = _cfg(step_tokens=1000)
        msgs = _conversation(turns=6, size=600)
        p = Policy(cfg)
        zones = p.protected_zones(_measure(msgs, cfg), msgs)
        d = p.decide_trigger(
            _measure(msgs, cfg), anchor_seq=0, messages=msgs,
            protected=zones, summary_quality_ok=False,
        )
        assert d.should_compact is False
        assert "摘要质量未达标" in d.reason

    def test_anchor_advances_incremental_window(self):
        """锚点推进后，同一批消息不再算作"新增量" —— 这是「在新增量上动手」的关键。"""
        cfg = _cfg(step_tokens=30000)
        msgs = _conversation(turns=6, size=600)
        p = Policy(cfg)
        zones = p.protected_zones(_measure(msgs, cfg), msgs)

        at_zero = p.decide_trigger(_measure(msgs, cfg), anchor_seq=0, messages=msgs, protected=zones)
        at_end = p.decide_trigger(_measure(msgs, cfg), anchor_seq=12, messages=msgs, protected=zones)
        assert at_zero.should_compact is True
        assert at_end.should_compact is False

    def test_overflow_recovery_limited(self):
        p = Policy(ContextConfig(overflow_retries=1))
        ok, _ = p.overflow_recovery(0)
        assert ok
        ok2, msg = p.overflow_recovery(1)
        assert not ok2 and "上限" in msg


# ===========================================================================
# 受保护区
# ===========================================================================
class TestProtectedZones:
    def test_system_first_last_user_recent(self):
        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=5, size=20)
        p = Policy(cfg)
        zones = p.protected_zones(_measure(msgs, cfg), msgs)
        assert 1 in zones, "system"
        assert 2 in zones, "第一条用户消息"
        last_user = [m["seq"] for m in msgs if m["role"] == "user"][-1]
        assert last_user in zones, "最后一条用户消息"

    def test_current_turn_citations_protected(self):
        """LitPrism 特化：正在被引用的证据不能被折掉。"""
        cfg = _cfg(recent_turns_protected=1)
        msgs = _conversation(turns=5, size=20)
        msgs[-1]["citations"] = '[{"chunk_id":"c_1","page":3}]'
        p = Policy(cfg)
        zones = p.protected_zones(_measure(msgs, cfg), msgs)
        assert msgs[-1]["seq"] in zones
        assert "引用片段" in zones[msgs[-1]["seq"]]

    def test_checkpoint_nodes_protected(self):
        cfg = _cfg()
        msgs = _conversation(turns=4, size=20)
        blocks = [{"id": "b1", "summary": "旧摘要", "direct_refs": "[]", "active": 1}]
        p = Policy(cfg)
        zones = p.protected_zones(_measure(msgs, cfg, blocks), msgs)
        assert any("压缩检查点" in v for v in zones.values())

    def test_extra_protected_injected(self):
        cfg = _cfg()
        msgs = _conversation(turns=6, size=50)
        p = Policy(cfg)
        s = p.select(_measure(msgs, cfg), messages=msgs, extra_protected={4: "交付物生成中的源范围"})
        assert 4 in s.protected_snapshot


# ===========================================================================
# 选择
# ===========================================================================
class TestSelection:
    def test_selects_old_range_and_keeps_tail(self):
        cfg = _cfg(recent_turns_protected=3)
        msgs = _conversation(turns=10, size=200)
        p = Policy(cfg)
        s = p.select(_measure(msgs, cfg), messages=msgs)
        assert s.ok, s.noop_reason
        assert s.shadowed_refs, "应选出可压缩区间"
        assert s.shadowed_tokens > 0 and s.retained_tokens > 0
        # 尾部必须保留
        assert s.end_seq < msgs[-1]["seq"]

    def test_never_shadows_system(self):
        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=10, size=200)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok
        assert make_ref(1) not in s.shadowed_refs, "system message 永不遮蔽"

    def test_safe_cut_is_user_boundary(self):
        """切口处必须是 user，避免出现「没有问题的答案」。"""
        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=10, size=200)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok
        # 区间末条应是 assistant（回答完整），下一条是 user
        seq_to_role = {m["seq"]: m["role"] for m in msgs}
        assert seq_to_role[s.end_seq] == "assistant"

    def test_noop_when_everything_protected(self):
        cfg = _cfg(recent_turns_protected=100)
        msgs = _conversation(turns=3, size=50)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok is False
        assert s.noop_reason

    def test_noop_on_empty_context(self):
        cfg = _cfg()
        s = Policy(cfg).select(_measure([], cfg), messages=[])
        assert s.ok is False and "为空" in s.noop_reason

    def test_noop_when_candidate_too_short(self):
        cfg = _cfg(recent_turns_protected=5)
        msgs = _conversation(turns=4, size=50)   # 8 条，保护 5 + 首末 → 剩不下
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok is False

    def test_aggressive_shrinks_retain_budget(self):
        cfg = _cfg(recent_turns_protected=2, retain_tokens=20000)
        msgs = _conversation(turns=14, size=300)
        p = Policy(cfg)
        normal = p.select(_measure(msgs, cfg), messages=msgs)
        aggr = p.select(_measure(msgs, cfg), messages=msgs, aggressive=True)
        assert normal.ok and aggr.ok
        assert aggr.retained_tokens <= normal.retained_tokens

    def test_shadowed_refs_are_contiguous(self):
        """不变量 4 要求：区间必须完整、按序、连续枚举。"""
        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=10, size=200)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok
        from litprism.refs import is_contiguous, parse_ref

        seqs = [parse_ref(r) for r in s.shadowed_refs]
        assert is_contiguous(seqs), f"区间不连续：{seqs}"

    def test_result_feeds_invariant_check(self):
        """选出的区间必须能通过不变量校验 —— 两者是配套的。"""
        from litprism.invariants import CompactContext, MessageView, check_compaction

        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=10, size=200)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.ok

        views = [
            MessageView(seq=m["seq"], ref=m["ref"], role=m["role"])
            for m in msgs
            if s.start_seq <= m["seq"] <= s.end_seq
        ]
        ctx = CompactContext(
            compaction_id="c_test",
            source_command="nudge",
            conversation_id="conv",
            start_seq=s.start_seq,
            end_seq=s.end_seq,
            shadowed_refs=s.shadowed_refs,
            messages=views,
            replacement_refs=list(s.shadowed_refs),
            has_summary=True,
            has_end=True,
        )
        r = check_compaction(ctx)
        assert r.ok, [str(v) for v in r.errors]

    def test_protected_snapshot_has_reasons(self):
        cfg = _cfg(recent_turns_protected=3)
        msgs = _conversation(turns=10, size=200)
        s = Policy(cfg).select(_measure(msgs, cfg), messages=msgs)
        assert s.protected_snapshot
        assert all(v for v in s.protected_snapshot.values()), "每个受保护项都要有可读理由"


# ===========================================================================
# 呈现
# ===========================================================================
class TestFormatting:
    def test_decision_and_selection_describe(self):
        from litprism.context.policy import format_decision, format_selection

        cfg = _cfg(recent_turns_protected=2)
        msgs = _conversation(turns=10, size=200)
        p = Policy(cfg)
        meas = _measure(msgs, cfg)
        assert "不压缩" in format_decision(p.decide_trigger(meas, anchor_seq=999, messages=msgs))
        text = format_selection(p.select(meas, messages=msgs))
        assert "候选区间" in text and "受保护区" in text
