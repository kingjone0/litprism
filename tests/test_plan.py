"""解读计划生成的测试。

核心性质：
1. **按预估条数装箱**，不是按字数 —— 每条指令的预估条数不超过配额；
2. **整篇装不下就退回分批**，不假装缓存还能用；
3. **确定性** —— 同样输入必然产出同样的计划（可复现、可存档）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.plan import (  # noqa: E402
    CLAIM_TOKENS,
    DEFAULT_QUOTA,
    DENSITY,
    build_plan,
    format_plan,
    section_runs,
)


class FakeMeter:
    """1 token = 4 字符，方便构造确定性的体量。"""

    def estimate_text(self, text: str) -> int:
        return max(1, len(text) // 4)


def chunk(i: int, section: str, chars: int = 400) -> dict:
    return {"id": f"c{i}", "section": section, "text": "x" * chars, "page": 1}


PAPER = {"id": "p1", "title": "T"}


class TestSectionRuns:
    def test_consecutive_same_section_merges(self):
        runs = section_runs([chunk(1, "方法"), chunk(2, "方法"), chunk(3, "结果")], FakeMeter())
        assert [r.label for r in runs] == ["方法", "结果"]
        assert len(runs[0].chunk_ids) == 2

    def test_same_label_twice_becomes_two_runs(self):
        """同名章节在文中出现两次 → 两个连续段，不合并。"""
        runs = section_runs([chunk(1, "结果"), chunk(2, "方法"), chunk(3, "结果")], FakeMeter())
        assert [r.label for r in runs] == ["结果", "方法", "结果"]

    def test_missing_section_defaults_to_body(self):
        runs = section_runs([chunk(1, "")], FakeMeter())
        assert runs[0].label == "正文"


class TestModeSelection:
    def test_whole_when_fits(self):
        plan = build_plan(PAPER, [chunk(1, "方法")], FakeMeter(), available_tokens=100_000)
        assert plan.mode == "whole"

    def test_batched_when_over_budget(self):
        """⭐ 装不下时如实退回，不假装共享前缀。"""
        big = [chunk(i, "方法", chars=4000) for i in range(1, 11)]  # 10k token
        plan = build_plan(PAPER, big, FakeMeter(), available_tokens=5_000)
        assert plan.mode == "batched"
        assert plan.directives == []
        assert "无法共享缓存前缀" in plan.note

    def test_overhead_counts_toward_budget(self):
        c = [chunk(i, "方法", chars=400) for i in range(1, 11)]  # 1k token
        assert build_plan(PAPER, c, FakeMeter(), available_tokens=1_100, overhead_tokens=0).mode == "whole"
        assert build_plan(PAPER, c, FakeMeter(), available_tokens=1_100, overhead_tokens=500).mode == "batched"


class TestPacking:
    def test_small_sections_merged(self):
        """⭐ 摘要只有几条也要占一次调用是浪费 —— 小节点应合并。"""
        chunks = [chunk(1, "摘要", 200), chunk(2, "引言", 200), chunk(3, "结论", 200)]
        plan = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        assert len(plan.directives) == 1
        assert len(plan.directives[0].runs) == 3

    def test_quota_respected(self):
        """每条指令的预估条数不超过配额（除非单节本身超配额）。"""
        chunks = [chunk(i, f"节{i}", chars=4000) for i in range(1, 8)]  # 每节 1k token
        plan = build_plan(PAPER, chunks, FakeMeter(), available_tokens=1_000_000)
        for d in plan.directives:
            if d.parts == 1 and len(d.runs) > 1:
                assert d.est_claims <= DEFAULT_QUOTA * 1.2

    def test_oversized_section_split_into_parts(self):
        big = [chunk(1, "结果", chars=200_000)]  # 50k token → 225 条
        plan = build_plan(PAPER, big, FakeMeter(), available_tokens=1_000_000)
        d = plan.directives[0]
        assert d.parts > 1
        assert "第 1/" in d.scope
        assert len(plan.directives) == d.parts

    def test_empty_paper(self):
        plan = build_plan(PAPER, [], FakeMeter(), available_tokens=100_000)
        assert plan.mode == "whole"
        assert plan.directives == []


class TestDirectiveContent:
    def test_scope_lists_sections(self):
        chunks = [chunk(1, "方法", 200), chunk(2, "结果", 200)]
        plan = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        text = plan.directives[0].text
        assert "方法" in text and "结果" in text
        assert "其余章节仅作上下文参考" in text

    def test_expected_range_brackets_estimate(self):
        chunks = [chunk(1, "方法", chars=4000)]  # 1k token → 4.5 条
        plan = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        lo, hi = plan.directives[0].expected
        assert lo >= 1 and hi > lo
        assert lo <= plan.directives[0].est_claims <= hi

    def test_chunk_ids_cover_all(self):
        chunks = [chunk(i, "方法", 200) for i in range(1, 6)]
        plan = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        covered = {cid for d in plan.directives for cid in d.chunk_ids}
        assert covered == {c["id"] for c in chunks}


class TestDeterminism:
    def test_same_input_same_plan(self):
        chunks = [chunk(i, "方法", 400) for i in range(1, 6)]
        a = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        b = build_plan(PAPER, chunks, FakeMeter(), available_tokens=100_000)
        assert a.to_dict() == b.to_dict()
        assert a.cache_key == b.cache_key

    def test_different_paper_different_key(self):
        a = build_plan({"id": "p1"}, [chunk(1, "方法")], FakeMeter(), available_tokens=100_000)
        b = build_plan({"id": "p2"}, [chunk(1, "方法")], FakeMeter(), available_tokens=100_000)
        assert a.cache_key != b.cache_key

    def test_format_plan_readable(self):
        plan = build_plan(PAPER, [chunk(1, "方法", 400)], FakeMeter(), available_tokens=100_000)
        text = format_plan(plan)
        assert "计划 p1" in text and "whole" in text


class TestConstants:
    def test_constants_consistent(self):
        """配额 × 单条 token ≈ 输出预算的 70%（4096 留 30% 余量）。"""
        assert CLAIM_TOKENS == 64
        assert DEFAULT_QUOTA == 44
        budget = DEFAULT_QUOTA * CLAIM_TOKENS
        assert 0.65 * 4096 <= budget <= 0.75 * 4096, f"配额占用 {budget}，偏离 70% 过多"
        assert 4.0 <= DENSITY <= 5.0
