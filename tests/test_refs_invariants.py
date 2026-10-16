"""ref 系统与不变量校验器的测试。

这两块是阶段 A 的核心，要求**每条不变量都有正例与反例**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.invariants import (  # noqa: E402
    FAILURE_CODES,
    CompactContext,
    ContextConfigView,
    InvariantError,
    MessageView,
    check_compaction,
    check_config,
    describe_rules,
    validate_or_raise_compaction,
)
from litprism.refs import (  # noqa: E402
    RefError,
    RefRange,
    format_range,
    has_duplicates,
    is_contiguous,
    is_valid_ref,
    make_ref,
    normalize_ref,
    parse_range,
    parse_ref,
    try_parse_range,
    try_parse_ref,
)


# ===========================================================================
# ref 系统
# ===========================================================================
class TestMakeRef:
    def test_zero_padded(self):
        assert make_ref(0) == "m00000"
        assert make_ref(1) == "m00001"
        assert make_ref(123) == "m00123"
        assert make_ref(99999) == "m99999"

    def test_beyond_width_grows(self):
        # 超过宽度不截断，自然变长（宽度只是下限）
        assert make_ref(123456) == "m123456"

    def test_rejects_bad_input(self):
        with pytest.raises(RefError):
            make_ref(-1)
        with pytest.raises(RefError):
            make_ref("3")  # type: ignore[arg-type]
        with pytest.raises(RefError):
            make_ref(True)  # bool 是 int 子类，必须显式拒绝


class TestParseRef:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("m00123", 123),
            ("m123", 123),           # 未补零
            ("M00123", 123),         # 大写
            ("M 123", 123),          # 大写 + 空格
            ("`m00123`", 123),       # 反引号围栏
            ("'m123'", 123),         # 单引号
            ('"m123"', 123),         # 双引号
            ("  m00123  ", 123),     # 前后空白
            ("00123", 123),          # 无前缀
            ("m00000", 0),
        ],
    )
    def test_tolerant_parsing(self, raw, expected):
        assert parse_ref(raw) == expected

    def test_output_always_canonical(self):
        for raw in ("m123", "M 123", "`m123`", "00123"):
            assert normalize_ref(raw) == "m00123"

    def test_rejects_garbage(self):
        for bad in ("", "   ", "abc", "m", "mabc", "m1a2", "1.5"):
            with pytest.raises(RefError):
                parse_ref(bad)

    def test_error_message_is_actionable(self):
        with pytest.raises(RefError) as ei:
            parse_ref("hello")
        msg = str(ei.value)
        assert "m00001" in msg          # 给出期望形态示例
        assert "hello" in msg           # 说明实际收到什么

    def test_try_variants(self):
        assert try_parse_ref("m123") == 123
        assert try_parse_ref("nope") is None
        assert is_valid_ref("m00123") is True
        assert is_valid_ref("nope") is False


class TestParseRange:
    @pytest.mark.parametrize("sep", ["-", "–", "—", "~", "～", "..", "…", " 到 ", " 至 "])
    def test_separators(self, sep):
        r = parse_range(f"m00001{sep}m00005")
        assert (r.start, r.end) == (1, 5)
        assert r.corrected is False

    def test_single_ref_is_degenerate_range(self):
        r = parse_range("m00003")
        assert (r.start, r.end) == (3, 3)
        assert r.length == 1

    def test_auto_corrects_reversed(self):
        """模型经常把 start/end 写反 —— 纠正而不报错，但必须标记。"""
        r = parse_range("m00005-m00001")
        assert (r.start, r.end) == (1, 5)
        assert r.corrected is True

    def test_range_with_fences(self):
        r = parse_range("`m1-m5`")
        assert (r.start, r.end) == (1, 5)

    def test_range_length_and_contains(self):
        r = parse_range("m00010-m00012")
        assert r.length == 3
        assert r.contains(11) and not r.contains(13)

    def test_format_roundtrip(self):
        r = parse_range("m1-m5")
        assert r.format() == "m00001-m00005"
        assert format_range(1, 5) == "m00001-m00005"
        assert parse_range(r.format()) == RefRange(1, 5, corrected=False)

    def test_rejects_three_endpoints(self):
        with pytest.raises(RefError) as ei:
            parse_range("m1-m2-m3")
        assert "3 个端点" in str(ei.value)

    def test_rejects_empty(self):
        with pytest.raises(RefError):
            parse_range("")
        with pytest.raises(RefError):
            parse_range(None)  # type: ignore[arg-type]

    def test_try_parse_range(self):
        assert try_parse_range("m1-m5").length == 5  # type: ignore[union-attr]
        assert try_parse_range("nope") is None


class TestSeqHelpers:
    def test_is_contiguous(self):
        assert is_contiguous([1, 2, 3]) is True
        assert is_contiguous([1, 3]) is False
        assert is_contiguous([]) is True
        assert is_contiguous([5, 4, 3]) is True   # 无序但连续

    def test_has_duplicates(self):
        assert has_duplicates([1, 1]) is True
        assert has_duplicates([1, 2]) is False


# ===========================================================================
# 不变量校验
# ===========================================================================
def _msgs(*specs: tuple[int, str]) -> list[MessageView]:
    """(seq, role) → MessageView 列表"""
    return [MessageView(seq=s, ref=make_ref(s), role=role) for s, role in specs]


def _good_ctx(**over) -> CompactContext:
    """一个合法的最小上下文，供各测试覆盖单个字段。"""
    msgs = _msgs((2, "user"), (3, "assistant"), (4, "user"), (5, "assistant"))
    base = dict(
        compaction_id="c_abc123",
        source_command="nudge",
        conversation_id="conv_1",
        start_seq=2,
        end_seq=5,
        shadowed_refs=["m00002", "m00003", "m00004", "m00005"],
        messages=msgs,
        replacement_refs=["m00002", "m00003", "m00004", "m00005"],
        open_tx_exists=False,
        has_summary=True,
        has_end=True,
    )
    base.update(over)
    return CompactContext(**base)  # type: ignore[arg-type]


class TestInvariantsHappyPath:
    def test_good_context_passes(self):
        r = check_compaction(_good_ctx())
        assert r.ok, f"合法上下文被判失败：{[str(v) for v in r.errors]}"

    def test_validate_or_raise_ok(self):
        validate_or_raise_compaction(_good_ctx())

    def test_rules_documented(self):
        rules = describe_rules()
        assert len(rules) == 9
        assert any("inv9" in k for k, _ in rules)


class TestInv1:
    def test_empty_compaction_id(self):
        r = check_compaction(_good_ctx(compaction_id="  "))
        assert not r.ok and any(v.code == "inv1.id" for v in r.errors)

    def test_empty_source_command(self):
        r = check_compaction(_good_ctx(source_command=""))
        assert not r.ok and any(v.code == "inv1.source" for v in r.errors)

    def test_unknown_source_is_warning_only(self):
        r = check_compaction(_good_ctx(source_command="whatever"))
        assert r.ok
        assert any(v.code == "inv1.source" and v.severity == "warning" for v in r.warnings)


class TestInv2:
    def test_negative_seq(self):
        r = check_compaction(_good_ctx(start_seq=-1))
        assert not r.ok and any(v.code == "inv2.seq_negative" for v in r.errors)

    def test_non_integer_seq(self):
        ctx = _good_ctx()
        ctx.messages[0].seq = "3"  # type: ignore[assignment]
        r = check_compaction(ctx)
        assert not r.ok and any(v.code == "inv2.seq_type" for v in r.errors)

    def test_oversized_seq(self):
        r = check_compaction(_good_ctx(end_seq=2**53))
        assert not r.ok and any(v.code == "inv2.seq_range" for v in r.errors)

    def test_reversed_range(self):
        r = check_compaction(_good_ctx(start_seq=9, end_seq=2))
        assert not r.ok and any(v.code == "inv2.range_order" for v in r.errors)


class TestInv3:
    def test_empty_shadowed_refs(self):
        r = check_compaction(_good_ctx(shadowed_refs=[]))
        assert not r.ok and any(v.code == "inv3.empty" for v in r.errors)

    def test_head_mismatch(self):
        r = check_compaction(_good_ctx(shadowed_refs=["m00003", "m00004", "m00005"]))
        assert not r.ok and any(v.code == "inv3.head" for v in r.errors)

    def test_tail_mismatch(self):
        r = check_compaction(_good_ctx(shadowed_refs=["m00002", "m00003", "m00004"]))
        assert not r.ok and any(v.code == "inv3.tail" for v in r.errors)

    def test_unparsable_ref(self):
        r = check_compaction(_good_ctx(shadowed_refs=["m00002", "garbage", "m00005"]))
        assert not r.ok and any(v.code == "inv3.parse" for v in r.errors)


class TestInv4:
    def test_missing_message(self):
        """漏列会让消息既不在上下文里、也不在摘要覆盖范围内。"""
        r = check_compaction(_good_ctx(shadowed_refs=["m00002", "m00003", "m00005"]))
        assert not r.ok
        v = next(v for v in r.errors if v.code == "inv4.missing")
        assert "m00004" in v.message

    def test_extra_message(self):
        r = check_compaction(
            _good_ctx(shadowed_refs=["m00002", "m00003", "m00004", "m00005", "m00006"])
        )
        assert not r.ok and any(v.code == "inv4.extra" for v in r.errors)

    def test_duplicates(self):
        r = check_compaction(_good_ctx(shadowed_refs=["m00002", "m00002", "m00003", "m00004", "m00005"]))
        assert not r.ok and any(v.code == "inv4.duplicate" for v in r.errors)

    def test_out_of_order(self):
        r = check_compaction(_good_ctx(shadowed_refs=["m00002", "m00004", "m00003", "m00005"]))
        assert not r.ok and any(v.code == "inv4.order" for v in r.errors)


class TestInv5:
    def test_uncovered_shadowed_ref(self):
        """血统断链：内容既不可见、也无法从摘要追溯。"""
        r = check_compaction(
            _good_ctx(replacement_refs=["m00002", "m00003", "m00004"])
        )
        assert not r.ok
        v = next(v for v in r.errors if v.code == "inv5.uncovered")
        assert "m00005" in v.message

    def test_none_skips_check(self):
        r = check_compaction(_good_ctx(replacement_refs=None))
        assert r.ok


class TestInv6:
    def test_orphan_answer_warns(self):
        msgs = _msgs((3, "assistant"), (4, "user"), (5, "assistant"))
        r = check_compaction(
            _good_ctx(start_seq=3, end_seq=5, messages=msgs,
                      shadowed_refs=["m00003", "m00004", "m00005"],
                      replacement_refs=["m00003", "m00004", "m00005"])
        )
        assert any(v.code == "inv6.orphan_answer" for v in r.warnings)

    def test_orphan_question_warns(self):
        msgs = _msgs((2, "user"), (3, "assistant"), (4, "user"))
        r = check_compaction(
            _good_ctx(start_seq=2, end_seq=4, messages=msgs,
                      shadowed_refs=["m00002", "m00003", "m00004"],
                      replacement_refs=["m00002", "m00003", "m00004"])
        )
        assert any(v.code == "inv6.orphan_question" for v in r.warnings)

    def test_balanced_range_no_warning(self):
        r = check_compaction(_good_ctx())
        assert not any(v.code.startswith("inv6") for v in r.violations)


class TestInv7:
    def test_concurrent_tx_blocked(self):
        r = check_compaction(_good_ctx(open_tx_exists=True))
        assert not r.ok and any(v.code == "inv7.concurrent" for v in r.errors)


class TestInv8:
    def test_end_without_summary(self):
        r = check_compaction(_good_ctx(has_summary=False, has_end=True))
        assert not r.ok and any(v.code == "inv8.no_summary" for v in r.errors)

    def test_summary_without_end_warns(self):
        r = check_compaction(_good_ctx(has_summary=True, has_end=False))
        assert r.ok
        assert any(v.code == "inv8.unclosed" for v in r.warnings)


class TestInv9:
    """【LitPrism 特有】压缩不得改变引用校验的结论。"""

    def test_identical_verdicts_pass(self):
        same = {"passed": True, "issues": []}
        r = check_compaction(_good_ctx(citation_before=dict(same), citation_after=dict(same)))
        assert r.ok

    def test_verdict_drift_blocked(self):
        r = check_compaction(
            _good_ctx(
                citation_before={"passed": True, "issues": []},
                citation_after={"passed": False, "issues": ["引用了不存在的片段 [P9]"]},
            )
        )
        assert not r.ok
        v = next(v for v in r.errors if v.code == "inv9.citation_drift")
        assert "防幻觉" in v.message

    def test_issue_set_order_ignored(self):
        r = check_compaction(
            _good_ctx(
                citation_before={"passed": False, "issues": ["a", "b"]},
                citation_after={"passed": False, "issues": ["b", "a"]},
            )
        )
        assert r.ok

    def test_recheck_callback_used_when_after_missing(self):
        r = check_compaction(
            _good_ctx(
                citation_before={"passed": True, "issues": []},
                citation_after=None,
                recheck_citations=lambda: {"passed": False, "issues": ["x"]},
            )
        )
        assert not r.ok and any(v.code == "inv9.citation_drift" for v in r.errors)


class TestFailureCodes:
    def test_closed_set(self):
        expected = {"busy", "cancelled", "changed", "summary", "commit", "overflow_limit", "invariant"}
        assert set(FAILURE_CODES) == expected

    def test_messages_are_actionable(self):
        for code, msg in FAILURE_CODES.items():
            assert len(msg) >= 8, f"{code} 的文案太短，用户无法据此行动"


class TestRaiseHelper:
    def test_raises_with_full_detail(self):
        with pytest.raises(InvariantError) as ei:
            validate_or_raise_compaction(_good_ctx(compaction_id="", open_tx_exists=True))
        msg = str(ei.value)
        assert "inv1.id" in msg and "inv7.concurrent" in msg


# ===========================================================================
# 配置 fail-fast
# ===========================================================================
class TestConfigCheck:
    def test_default_config_passes(self):
        r = check_config(ContextConfigView())
        assert r.ok, [str(v) for v in r.errors]

    def test_mutex_fields_rejected(self):
        r = check_config(ContextConfigView(retain_tokens=5000, retain_ratio=0.3))
        assert not r.ok and any(v.code == "cfg.mutex" for v in r.errors)

    def test_budget_nonpositive_rejected(self):
        r = check_config(ContextConfigView(window_tokens=4096, output_reserve_tokens=4096))
        assert not r.ok and any(v.code == "cfg.budget" for v in r.errors)

    def test_retain_ge_step_rejected(self):
        r = check_config(ContextConfigView(retain_tokens=40000, step_tokens=30000))
        assert not r.ok and any(v.code == "cfg.retain_vs_step" for v in r.errors)

    def test_retain_ratio_ge_nudge_rejected(self):
        r = check_config(ContextConfigView(retain_ratio=0.9, nudge_ratio=0.75))
        assert not r.ok and any(v.code == "cfg.retain_vs_nudge" for v in r.errors)

    def test_zero_retries_rejected(self):
        r = check_config(ContextConfigView(overflow_retries=0))
        assert not r.ok and any(v.code == "cfg.retries" for v in r.errors)

    def test_unknown_key_warns_not_fails(self):
        """可选字段畸形应静默丢弃 + 一次警告，不阻断启动。"""
        r = check_config(ContextConfigView(extra={"totally_unknown": 1}))
        assert r.ok
        assert any(v.code == "cfg.unknown_key" for v in r.warnings)

    def test_bad_ratio_order_warns(self):
        r = check_config(ContextConfigView(nudge_ratio=0.9, emergency_ratio=0.8))
        assert any(v.code == "cfg.ratios" for v in r.warnings)


# ===========================================================================
# 配置层接线
# ===========================================================================
class TestConfigWiring:
    def test_defaults_load_and_validate(self):
        from litprism.config import load_config

        cfg = load_config()
        assert cfg.context.window_tokens > 0
        assert cfg.context.available_tokens > 0
        assert cfg.memory.resident_token_budget > 0
        assert cfg.config_warnings == []

    def test_effective_retain_default_is_ratio_of_window(self):
        from litprism.config import ContextConfig

        c = ContextConfig(window_tokens=32000)
        assert c.effective_retain_tokens == int(32000 * 0.30)

    def test_effective_retain_prefers_explicit_tokens(self):
        from litprism.config import ContextConfig

        assert ContextConfig(window_tokens=32000, retain_tokens=5000).effective_retain_tokens == 5000
        assert ContextConfig(window_tokens=32000, retain_ratio=0.25).effective_retain_tokens == 8000

    def test_unknown_key_is_preserved_and_warned(self):
        """未知键不丢弃（保留在 extra），由 check_config 报告为警告而非错误。"""
        from litprism.config import _from_dict

        cfg = _from_dict({"context": {"window_tokens": 16000, "totally_bogus": 1}})
        assert cfg.context.window_tokens == 16000
        assert "totally_bogus" in cfg.context.extra

        view = ContextConfigView(
            window_tokens=cfg.context.window_tokens,
            extra=cfg.context.extra,
        )
        r = check_config(view)
        assert r.ok
        assert any(v.code == "cfg.unknown_key" for v in r.warnings)

    def test_invalid_config_raises_at_load(self, tmp_path):
        """自相矛盾的配置必须在载入期被拒，而不是运行中才发现。"""
        import yaml

        from litprism.config import load_config

        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.safe_dump({"context": {"retain_tokens": 40000, "step_tokens": 30000}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="配置不合法"):
            load_config(bad)

    def test_memory_budget_must_be_positive(self, tmp_path):
        import yaml

        from litprism.config import load_config

        bad = tmp_path / "bad2.yaml"
        bad.write_text(yaml.safe_dump({"memory": {"resident_token_budget": 0}}), encoding="utf-8")
        with pytest.raises(ValueError, match="resident_token_budget"):
            load_config(bad)
