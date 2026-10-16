"""Meter（测量服务）测试。

重点验证三件事：
1. 两种占用（会话记账 vs 发送视图）**必须分开**，混用会让用户以为压缩没生效；
2. 中文估算不能沿用 4 字符/token（会严重低估）；
3. 提供方上报的用量**只在 envelope 完全匹配时**才采信。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import ContextConfig  # noqa: E402
from litprism.context.meter import CJK_NOTE, Meter  # noqa: E402
from litprism.refs import make_ref  # noqa: E402


def _msg(seq: int, role: str, content: str, **over) -> dict:
    d = {"seq": seq, "ref": make_ref(seq), "role": role, "content": content, "kind": "normal"}
    d.update(over)
    return d


class TestEstimation:
    def test_empty_is_zero(self):
        m = Meter()
        assert m.estimate_text("") == 0
        assert m.estimate_text(None) == 0

    def test_latin_uses_4_chars_per_token(self):
        m = Meter(ContextConfig(latin_chars_per_token=4.0))
        # 400 个拉丁字符 ≈ 100 token
        assert 90 <= m.estimate_text("a" * 400) <= 110

    def test_cjk_is_not_underestimated(self):
        """4 字符/token 对中文严重低估 —— 中文必须用独立系数。"""
        m = Meter(ContextConfig(cjk_chars_per_token=1.5, latin_chars_per_token=4.0))
        cjk_text = "深度学习在医学图像分割中的应用研究" * 10   # 180 个汉字
        latin_text = "a" * 180
        cjk_tokens = m.estimate_text(cjk_text)
        latin_tokens = m.estimate_text(latin_text)
        # 同样字符数，中文的 token 数应显著高于拉丁文（约 2.5 倍）
        assert cjk_tokens > latin_tokens * 2, f"中文 {cjk_tokens} vs 拉丁 {latin_tokens}"

    def test_mixed_text_between_the_two(self):
        m = Meter()
        mixed = "深度学习" * 20 + "a" * 80
        assert m.estimate_text(mixed) > 0

    def test_message_includes_overhead(self):
        m = Meter()
        assert m.estimate_message(_msg(1, "user", "")) >= 4

    def test_message_counts_citations(self):
        """引用附件同样占上下文，必须计价。"""
        m = Meter()
        bare = m.estimate_message(_msg(1, "assistant", "结论如下。"))
        with_cites = m.estimate_message(
            _msg(1, "assistant", "结论如下。", citations=json.dumps([{"chunk_id": "c_" + "x" * 40, "page": 3}]))
        )
        assert with_cites > bare


class TestTwoKindsOfOccupancy:
    def test_total_and_surface_differ_when_shadowed(self):
        """核心：会话记账含已压缩原文，发送视图不含 —— 两者必须分开。"""
        m = Meter()
        messages = [
            _msg(1, "system", "系统提示词"),
            _msg(2, "user", "问题一" * 100),
            _msg(3, "assistant", "回答一" * 100),
            _msg(4, "user", "问题二"),
            _msg(5, "assistant", "回答二"),
        ]
        blocks = [
            {
                "id": "b_1",
                "summary": "此前讨论了问题一。",
                "direct_refs": json.dumps(["m00002", "m00003"]),
                "active": 1,
            }
        ]
        meas = m.measure_nodes(messages, blocks)
        assert meas.total_tokens > meas.surface_tokens, "被遮蔽的内容仍应计入会话记账"
        # 发送视图里不应出现被遮蔽的消息
        visible_seqs = {n.seq for n in meas.nodes}
        assert 2 not in visible_seqs and 3 not in visible_seqs
        assert 4 in visible_seqs and 5 in visible_seqs

    def test_inactive_block_does_not_shadow(self):
        m = Meter()
        messages = [_msg(2, "user", "内容" * 50), _msg(3, "assistant", "回复" * 50)]
        blocks = [{"id": "b_1", "summary": "x", "direct_refs": json.dumps(["m00002", "m00003"]), "active": 0}]
        meas = m.measure_nodes(messages, blocks)
        assert {n.seq for n in meas.nodes} == {2, 3}

    def test_block_contributes_to_surface(self):
        m = Meter()
        messages = [_msg(2, "user", "内容" * 50)]
        blocks = [{"id": "b_1", "summary": "摘要正文" * 20, "direct_refs": json.dumps(["m00002"]), "active": 1}]
        meas = m.measure_nodes(messages, blocks)
        assert any(n.kind == "compact-checkpoint" for n in meas.nodes)

    def test_dirty_refs_do_not_crash(self):
        """脏数据不应让测量崩掉。"""
        m = Meter()
        messages = [_msg(2, "user", "x" * 100)]
        blocks = [{"id": "b", "summary": "s", "direct_refs": "{not json", "active": 1}]
        meas = m.measure_nodes(messages, blocks)
        assert meas.total_tokens > 0

    def test_checkpoint_not_double_counted(self):
        """压缩块与它的替换消息是同一份摘要的两种表示 —— 不能计价两次。

        否则用户会看到「摘要占了 13%」而账本说「压到 6%」的矛盾数字。
        """
        m = Meter()
        summary = "摘要正文" * 50
        messages = [
            _msg(2, "user", "旧问题" * 100),
            _msg(
                3, "user", summary,
                kind="compact-checkpoint", block_id="blk_1",
            ),
        ]
        blocks = [{"id": "blk_1", "summary": summary, "direct_refs": json.dumps(["m00002"]), "active": 1}]

        meas = m.measure_nodes(messages, blocks)
        checkpoint_nodes = [n for n in meas.nodes if n.kind == "compact-checkpoint"]
        assert len(checkpoint_nodes) == 1, f"检查点被重复计价：{len(checkpoint_nodes)} 个节点"

        # 摘要部分应约等于单份摘要的估价
        single = m.estimate_message(messages[1])
        assert checkpoint_nodes[0].tokens == single

    def test_block_counted_when_no_replacement_message(self):
        """替换消息缺失时（异常状态）块本身仍要计价，不能漏算。"""
        m = Meter()
        messages = [_msg(2, "user", "x" * 100)]
        blocks = [{"id": "blk_1", "summary": "摘要" * 50, "direct_refs": json.dumps(["m00002"]), "active": 1}]
        meas = m.measure_nodes(messages, blocks)
        assert any(n.kind == "compact-checkpoint" and n.block_id == "blk_1" for n in meas.nodes)


class TestPressureAndBreakdown:
    def test_pressure_uses_surface(self):
        m = Meter(ContextConfig(window_tokens=1000, output_reserve_tokens=100))
        messages = [_msg(1, "user", "a" * 400)]   # ≈100 token
        meas = m.measure_nodes(messages)
        assert 0 < meas.pressure < 1
        assert meas.headroom_tokens > 0

    def test_breakdown_separates_system_dialogue_summary(self):
        m = Meter()
        messages = [
            _msg(1, "system", "系统提示"),
            _msg(2, "user", "问题" * 50),
        ]
        blocks = [{"id": "b", "summary": "旧摘要" * 20, "direct_refs": json.dumps(["m00002"]), "active": 1}]
        bd = m.measure_nodes(messages, blocks).breakdown()
        assert bd["system"] > 0
        assert bd["摘要"] > 0

    def test_zero_window_does_not_divide_by_zero(self):
        m = Meter(ContextConfig(window_tokens=0))
        assert m.measure_nodes([_msg(1, "user", "x")]).pressure == 0.0


class TestProviderReportedUsage:
    def test_reported_usage_used_only_on_envelope_match(self):
        """只在 envelope 完全匹配时采信 —— 否则会用上次的用量描述这次上下文。"""
        m = Meter()
        messages = [_msg(1, "user", "问题" * 100)]
        h = Meter.envelope_hash(messages, [])
        m.record_provider_usage(h, 12345)

        # 匹配 → 采信
        meas = m.measure_nodes(messages, [])
        reported = m._provider_usage.get(Meter.envelope_hash(messages, []))
        assert reported == 12345

        # 不匹配 → 不采信
        changed = [_msg(1, "user", "换了内容" * 100)]
        assert m._provider_usage.get(Meter.envelope_hash(changed, [])) is None
        assert meas.estimated is True
        assert meas.note == CJK_NOTE

    def test_envelope_hash_is_stable_and_sensitive(self):
        m1, m2 = [_msg(1, "user", "abc")], [_msg(1, "user", "abc")]
        m3 = [_msg(1, "user", "abd")]
        assert Meter.envelope_hash(m1, []) == Meter.envelope_hash(m2, [])
        assert Meter.envelope_hash(m1, []) != Meter.envelope_hash(m3, [])

    def test_block_state_affects_envelope(self):
        messages = [_msg(1, "user", "x")]
        b_active = [{"id": "b", "summary": "s", "active": 1}]
        b_inactive = [{"id": "b", "summary": "s", "active": 0}]
        assert Meter.envelope_hash(messages, b_active) != Meter.envelope_hash(messages, b_inactive)


class TestDefaultProtection:
    def test_system_first_user_last_user_recent_are_protected(self):
        m = Meter(ContextConfig(recent_turns_protected=2))
        messages = [
            _msg(1, "system", "sys"),
            _msg(2, "user", "第一个问题"),
            _msg(3, "assistant", "回答"),
            _msg(4, "user", "中间问题"),
            _msg(5, "assistant", "中间回答"),
            _msg(6, "user", "最后一个问题"),
        ]
        protected, reasons = m._default_protected(messages)
        assert 1 in protected, "system 必须受保护"
        assert 2 in protected, "第一条用户消息（研究意图）必须受保护"
        assert 6 in protected, "最后一条用户消息（当前指令）必须受保护"
        assert all(reasons[s] for s in protected), "每个受保护项都要有可读理由"

    def test_empty_messages(self):
        m = Meter()
        protected, reasons = m._default_protected([])
        assert protected == set() and reasons == {}
