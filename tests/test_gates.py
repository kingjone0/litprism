"""机械闸门与有界修复的测试。

闸门必须是**确定性**的：同样输入 → 同样结论。否则"为什么这条被判漏读"
就说不清，评测也无法复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.extract import _run_with_repair  # noqa: E402
from litprism.gates import (  # noqa: E402
    MAX_REPAIR_ROUNDS,
    count_claims,
    evaluate,
    numeric_claims,
    repair_text,
    split_scope,
)
from litprism.plan import Directive, SectionRun  # noqa: E402

FIELDS = [
    ("research_question", "研究问题"), ("methodology", "方法论"),
    ("experiment_design", "实验设计"), ("key_conclusions", "关键结论"),
    ("innovations", "创新点"), ("limitations", "局限与未来工作"),
]


def data(**fields) -> dict:
    base = {k: "" for k, _ in FIELDS}
    base.update(fields)
    return base


class TestCountClaims:
    def test_counts_sentences(self):
        d = data(key_conclusions="结论一 [P1 p.1]。结论二 [P1 p.1]。")
        assert count_claims(d, FIELDS) == 2

    def test_ignores_empty_and_punctuation(self):
        assert count_claims(data(), FIELDS) == 0
        assert count_claims(data(key_conclusions="。。！"), FIELDS) == 0

    def test_citation_only_line_not_counted(self):
        """只有引用标记、没有实质文字的行不算陈述。"""
        assert count_claims(data(key_conclusions="[P1 p.1]。"), FIELDS) == 0

    def test_across_fields(self):
        d = data(methodology="方法 A [P1 p.1]。", key_conclusions="结论 B [P1 p.1]。")
        assert count_claims(d, FIELDS) == 2


class TestNumericClaims:
    def test_extracts_numeric_sentences(self):
        d = data(key_conclusions="准确率为 91.2% [P1 p.1]。方法使用 Transformer [P1 p.1]。")
        got = numeric_claims(d, FIELDS)
        assert len(got) == 1 and "91.2" in got[0]

    def test_no_numbers(self):
        assert numeric_claims(data(key_conclusions="没有数字 [P1 p.1]。"), FIELDS) == []


class TestGates:
    def test_all_pass(self):
        d = data(key_conclusions="结论一 [P1 p.1]。结论二 [P1 p.1]。")
        g = evaluate(d, FIELDS, expected=(1, 5))
        assert not g.failed and not g.needs_repair

    def test_missing_citation_flagged(self):
        d = data(key_conclusions="有内容但没有来源。")
        g = evaluate(d, FIELDS, expected=(1, 5))
        hit = [r for r in g.results if r.name == 'has_citation'][0]
        assert not hit.ok and hit.action == 'flag'

    def test_coverage_low_triggers_repair(self):
        """⭐ 条数低于下限 = 疑似漏读，这是唯一会触发重跑的闸门。"""
        d = data(key_conclusions="只有一条 [P1 p.1]。")
        g = evaluate(d, FIELDS, expected=(10, 20))
        cov = [r for r in g.results if r.name == 'section_coverage'][0]
        assert not cov.ok and cov.action == 'repair' and g.needs_repair
        assert '疑似漏读' in cov.detail

    def test_coverage_high_is_flag_not_repair(self):
        """条数过多不重跑 —— 重跑只会更多，标人工核对即可。"""
        d = data(key_conclusions="一 [P1 p.1]。二 [P1 p.1]。三 [P1 p.1]。")
        g = evaluate(d, FIELDS, expected=(1, 2))
        cov = [r for r in g.results if r.name == 'section_coverage'][0]
        assert not cov.ok and cov.action == 'flag'
        assert not g.needs_repair

    def test_no_expected_always_passes_coverage(self):
        d = data(key_conclusions="只有一条 [P1 p.1]。")
        g = evaluate(d, FIELDS, expected=None)
        assert not g.needs_repair

    def test_issues_recorded_as_flag(self):
        d = data(key_conclusions="内容 [P1 p.1]。")
        g = evaluate(d, FIELDS, expected=(1, 5), issues=['页码不符'])
        hit = [r for r in g.results if r.name == 'citation_issues'][0]
        assert not hit.ok and hit.action == 'flag'

    def test_deterministic(self):
        d = data(key_conclusions="一条 [P1 p.1]。")
        a = evaluate(d, FIELDS, expected=(5, 9)).to_dict()
        b = evaluate(d, FIELDS, expected=(5, 9)).to_dict()
        assert a == b


class TestRepairText:
    def test_includes_hint_and_numbers(self):
        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=8000)])
        lo, hi = d.expected
        text = repair_text(d, got=2)
        assert d.text in text
        assert '2 条' in text and f'{lo}–{hi}' in text
        assert '逐小节检查' in text

    def test_split_scope(self):
        assert split_scope('「A」、「B」、「C」、「D」') == ('「A」、「B」', '「C」、「D」')
        assert split_scope('「A」') is None


# ---------------------------------------------------------------------------
class StubLLM:
    """按调用次数返回不同结果，用来驱动修复循环。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, messages, task=None):
        self.calls += 1
        r = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def payload(sentences: int, *, cite: bool = True) -> dict:
    body = ''.join(f'结论{i} [P1 p.1]。' for i in range(1, sentences + 1)) if cite else '结论没有来源。'
    return {**{k: '' for k, _ in FIELDS}, 'key_conclusions': body,
            'field_evidence': {'key_conclusions': ['P1 p.1'] if cite else []}, 'keywords': []}


SCOPE = [{'tag': 'P1', 'id': 'c1', 'paper_id': 'p1', 'page': 1,
          'text': '原文内容', 'paper_title': 'T'}]


class TestOverlap:
    """跨指令重复：条数正常但内容全是别人的。"""

    def test_duplicate_ratio_identical(self):
        from litprism.gates import duplicate_ratio

        a = ["在 WMT 2014 英德任务上取得 28.4 BLEU。"]
        assert duplicate_ratio(a, a) == 1.0

    def test_duplicate_ratio_disjoint(self):
        from litprism.gates import duplicate_ratio

        a = ["完全不同的内容甲。"]
        b = ["毫不相干的话题乙。"]
        assert duplicate_ratio(a, b) == 0.0

    def test_duplicate_ratio_empty(self):
        from litprism.gates import duplicate_ratio

        assert duplicate_ratio([], ["x"]) == 0.0

    def test_flags_copying_unit(self):
        from litprism.gates import overlap_flags

        a = ["在 WMT 2014 英德任务上取得 28.4 BLEU，超过此前最好结果。",
             "训练使用 8 张 P100，耗时 3.5 天。"]
        b = list(a)  # B 完全抄 A
        flags = overlap_flags([("d1", a), ("d2", b)])
        assert len(flags) == 1
        assert flags[0]["unit"] == "d2" and flags[0]["resembles"] == "d1"
        assert flags[0]["ratio"] >= 0.8

    def test_no_flag_for_distinct_content(self):
        from litprism.gates import overlap_flags

        a = ["在 WMT 2014 英德任务上取得 28.4 BLEU。"]
        b = ["局限性在于长序列推理成本高，且未讨论低资源语言。"]
        assert overlap_flags([("d1", a), ("d2", b)]) == []

    def test_no_flag_for_single_unit(self):
        from litprism.gates import overlap_flags

        assert overlap_flags([("d1", ["一条陈述。"])]) == []

    def test_empty_units_ignored(self):
        from litprism.gates import overlap_flags

        assert overlap_flags([("d1", []), ("d2", ["内容。"])]) == []


class TestRepairLoop:
    def test_no_repair_when_coverage_ok(self):
        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=1000)])
        lo, hi = d.expected
        llm = StubLLM([payload(lo + 1)])
        data_, issues, gates = _run_with_repair(llm, 'S', 'CTX', d, 'F', SCOPE, 'd1')
        assert llm.calls == 1 and data_ is not None and not gates.needs_repair

    def test_repair_retries_when_thin(self):
        """⭐ 条数不足 → 触发一次重跑。"""
        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=1000)])
        lo, hi = d.expected
        llm = StubLLM([payload(1), payload(lo + 2)])
        data_, issues, gates = _run_with_repair(llm, 'S', 'CTX', d, 'F', SCOPE, 'd1')
        assert llm.calls == 2
        assert count_claims(data_, FIELDS) == lo + 2

    def test_keeps_best_when_repair_worse(self):
        """⭐ 修复后更差就不采纳 —— 不能让"修"把好结果改坏。"""
        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=1000)])
        lo, hi = d.expected
        llm = StubLLM([payload(lo + 1), payload(1)])
        data_, issues, gates = _run_with_repair(llm, 'S', 'CTX', d, 'F', SCOPE, 'd1')
        assert count_claims(data_, FIELDS) == lo + 1

    def test_repair_bounded(self):
        """最多 1 + MAX_REPAIR_ROUNDS 次调用。"""
        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=1000)])
        llm = StubLLM([payload(1)])  # 永远不够
        _run_with_repair(llm, 'S', 'CTX', d, 'F', SCOPE, 'd1')
        assert llm.calls == MAX_REPAIR_ROUNDS + 1

    def test_truncation_marks_uncovered(self):
        """⭐ 输出截断不能崩、也不能伪造空字段。"""
        from litprism.llm import OutputLimitError

        d = Directive(id='d1', runs=[SectionRun(label='方法', chunk_ids=['c1'], tokens=1000)])
        llm = StubLLM([OutputLimitError('撞顶')])
        data_, issues, gates = _run_with_repair(llm, 'S', 'CTX', d, 'F', SCOPE, 'd1')
        assert data_ is None
        assert any('未覆盖' in i for i in issues)
        assert not gates.results[0].ok

    def test_no_directive_never_repairs(self):
        """分批模式没有期望区间，不做修复。"""
        llm = StubLLM([payload(1)])
        data_, issues, gates = _run_with_repair(llm, 'S', 'CTX', None, 'F', SCOPE, 'b1')
        assert llm.calls == 1 and data_ is not None
