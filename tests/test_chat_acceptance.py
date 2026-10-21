"""N04 验收工具的度量逻辑。

工具本身也会算错 —— 这里锁住四项指标的定义：
分母为 0 一律 None（n/a）；拒答与答错分开；调用失败单独计数。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from chat_acceptance import judge, pct, verdict  # noqa: E402


def srow(cid, kind, hits, top_score):
    return {'id': cid, 'kind': kind, 'hits': hits, 'papers': {}, 'top_score': top_score}


def row(cid, kind, *, citations=0, valid=0, refused=False, in_scope=True, papers=(), error=False):
    return {'id': cid, 'kind': kind, 'citations': citations, 'valid_citations': valid,
            'refused': refused, 'in_scope': in_scope, 'papers': list(papers), 'error': error}


def cases(**spec):
    return {k: {'kind': v[0], '_expect_ids': v[1] if len(v) > 1 else [],
                'expect_min_papers': v[2] if len(v) > 2 else 2}
            for k, v in spec.items()}


class TestCitationHit:
    def test_ratio(self):
        rows = [row('a', 'answerable', citations=4, valid=3)]
        m = judge(rows, cases(a=('answerable',)))
        assert m['引用命中率'] == {'num': 3, 'den': 4, 'ratio': 0.75}

    def test_no_citations_is_none(self):
        """没有引用 ≠ 100%；没有分母就是不可知。"""
        m = judge([row('a', 'refuse', refused=True)], cases(a=('refuse',)))
        assert m['引用命中率']['ratio'] is None


class TestScope:
    def test_out_of_scope_counts_against(self):
        rows = [row('a', 'answerable', citations=2, valid=2, in_scope=True),
                row('b', 'answerable', citations=2, valid=2, in_scope=False)]
        m = judge(rows, cases(a=('answerable',), b=('answerable',)))
        assert m['范围遵守率'] == {'num': 1, 'den': 2, 'ratio': 0.5}

    def test_refusal_does_not_affect_scope(self):
        """拒答题没有引用，不该进范围分母。"""
        rows = [row('a', 'answerable', citations=1, valid=1),
                row('b', 'refuse', refused=True)]
        m = judge(rows, cases(a=('answerable',), b=('refuse',)))
        assert m['范围遵守率']['den'] == 1


class TestRefusal:
    def test_correct_and_false_refusal(self):
        rows = [row('a', 'refuse', refused=True), row('b', 'refuse', refused=False),
                row('c', 'answerable', refused=True), row('d', 'answerable', refused=False)]
        m = judge(rows, cases(a=('refuse',), b=('refuse',), c=('answerable',), d=('answerable',)))
        assert m['拒答正确率']['num'] == 1 and m['拒答正确率']['den'] == 2
        # ⭐ 应答题被拒 = 误拒，与"该拒没拒"分开记
        assert m['拒答正确率']['false_refuse'] == 1
        assert m['拒答正确率']['answerable'] == 2

    def test_no_refuse_cases_is_none(self):
        m = judge([row('a', 'answerable')], cases(a=('answerable',)))
        assert m['拒答正确率']['ratio'] is None


class TestFollowup:
    def test_hit(self):
        rows = [row('f', 'followup', citations=1, valid=1, papers=['p1'])]
        m = judge(rows, cases(f=('followup', ['p1'])))
        assert m['追问可用率'] == {'num': 1, 'den': 1, 'ratio': 1.0}

    def test_wrong_paper(self):
        """追问指向了另一篇 → 不可用。"""
        rows = [row('f', 'followup', citations=1, valid=1, papers=['p2'])]
        m = judge(rows, cases(f=('followup', ['p1'])))
        assert m['追问可用率']['num'] == 0


class TestCross:
    def test_needs_multiple_papers(self):
        rows = [row('x', 'cross', citations=8, valid=8, papers=['p1'])]
        m = judge(rows, cases(x=('cross', [], 2)))
        assert m['跨论文引用']['num'] == 0  # 只有一篇 → 不算跨论文

    def test_two_papers_ok(self):
        rows = [row('x', 'cross', citations=8, valid=8, papers=['p1', 'p2'])]
        m = judge(rows, cases(x=('cross', [], 2)))
        assert m['跨论文引用']['num'] == 1


class TestErrors:
    def test_failed_calls_are_counted(self):
        """调用失败必须单独计数 —— 否则失败会被算成"答过"。"""
        rows = [row('a', 'answerable', error=True), row('b', 'answerable')]
        m = judge(rows, cases(a=('answerable',), b=('answerable',)))
        assert m['调用失败'] == {'num': 1, 'den': 2, 'ratio': 0.5}


class TestPct:
    def test_none_renders_as_na(self):
        """⭐ 绝不能因为 (v or 0) 把未知渲染成 0%。"""
        assert pct(None) == 'n/a'
        assert pct(0) == '0%'
        assert pct(1) == '100%'


class TestSweepVerdict:
    """阈值扫描的判定：关键不是"能否压到 0"，而是分数有没有序。"""

    def test_unordered_scores_kill_the_threshold_idea(self):
        """⭐ 无关问题分更高时，任何绝对阈值都会先误杀应答题。"""
        rows = [srow('a', 'answerable', {0.0: 8, 8.0: 1}, 9.66),
                srow('r', 'refuse', {0.0: 8, 8.0: 2}, 10.5)]
        _summary, scores = verdict(rows, [0.0, 8.0])
        assert scores['scores_ordered'] is False
        assert scores['refuse_top_score'] == 10.5
        assert scores['answerable_top_score'] == 9.66

    def test_ordered_scores_may_separate(self):
        rows = [srow('a', 'answerable', {0.0: 8, 5.0: 8}, 12.0),
                srow('r', 'refuse', {0.0: 8, 5.0: 0}, 4.0)]
        summary, scores = verdict(rows, [0.0, 5.0])
        assert scores['scores_ordered'] is True
        assert summary[1]['separable'] is True

    def test_not_separable_when_answerable_dies_too(self):
        rows = [srow('a', 'answerable', {0.0: 8, 5.0: 0}, 12.0),
                srow('r', 'refuse', {0.0: 8, 5.0: 0}, 4.0)]
        summary, _ = verdict(rows, [0.0, 5.0])
        assert summary[1]['separable'] is False
