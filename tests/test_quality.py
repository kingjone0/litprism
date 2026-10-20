"""N06 质量聚合的测试。

口径底线：
1. 分母为 0 一律 ``None``（界面 n/a），**不是 0%**；
2. 撤销是回退动作，不计入审核样本；
3. 分字段统计，不合并成单一总分；
4. 与 ``tools/eval_extraction.py`` 同源 —— 同一份数据只能有一个数字。
"""

from __future__ import annotations

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.quality import aggregate, empty_summary, summarize


def claim(cid, field, text, status='machine_supported'):
    return {'id': cid, 'field': field, 'text': text, 'status': status,
            'candidate_tags': ['P1'], 'batch': 1}


def review(cid, action, reviewed_text=None):
    return {'claim_id': cid, 'action': action, 'reviewed_text': reviewed_text, 'note': '依据'}


REPORT = {'items': [
    claim('c1', 'key_conclusions', '准确率 91.2%。'),
    claim('c2', 'key_conclusions', '训练用了 8 块 P100。'),
    claim('c3', 'methodology', '使用 Adam 优化器。'),
    claim('c4', 'methodology', '学习率 3e-4。'),
    claim('c5', 'limitations', '未讨论低资源语言。', status='needs_review'),
]}


class TestAggregate:
    def test_counts_per_field(self):
        got = aggregate(REPORT, [])
        assert got['key_conclusions']['total'] == 2
        assert got['methodology']['total'] == 2
        assert got['limitations']['total'] == 1
        assert got['limitations']['machine_supported'] == 0

    def test_verbatim_vs_corrected(self):
        got = aggregate(REPORT, [
            review('c1', 'confirm', '准确率 91.2%。'),          # 未改 → 逐字
            review('c2', 'confirm', '训练用了 8 块 P100，耗时 3.5 天。'),  # 改过 → 修正
        ])
        assert got['key_conclusions']['verbatim'] == 1
        assert got['key_conclusions']['corrected'] == 1
        assert got['key_conclusions']['reviewed'] == 2

    def test_confirm_without_text_counts_as_verbatim(self):
        got = aggregate(REPORT, [review('c1', 'confirm', None)])
        assert got['key_conclusions']['verbatim'] == 1

    def test_reject(self):
        got = aggregate(REPORT, [review('c1', 'reject')])
        assert got['key_conclusions']['rejected'] == 1

    def test_last_record_wins(self):
        """确认→再拒绝：以最后一条为准。"""
        got = aggregate(REPORT, [review('c1', 'confirm', '准确率 91.2%。'), review('c1', 'reject')])
        assert got['key_conclusions']['rejected'] == 1
        assert got['key_conclusions']['verbatim'] == 0

    def test_revoke_is_not_a_sample(self):
        """⭐ 撤销是回退，不计入审核样本 —— 否则覆盖会被虚增。"""
        got = aggregate(REPORT, [review('c1', 'confirm', '准确率 91.2%。'), review('c1', 'revoke')])
        assert got['key_conclusions']['reviewed'] == 0

    def test_orphan_review_ignored(self):
        """陈述来自旧版本、已不在当前报告里 → 忽略。"""
        got = aggregate(REPORT, [review('gone', 'confirm', 'x')])
        assert sum(f['reviewed'] for f in got.values()) == 0

    def test_manual_counted_per_field(self):
        got = aggregate(REPORT, [], [{'field': 'methodology'}, {'field': 'methodology'}])
        assert got['methodology']['manual'] == 2
        assert got['methodology']['total'] == 2  # 人工不改变机器分母


class TestSummarize:
    def test_ratios(self):
        s = summarize(REPORT, [
            review('c1', 'confirm', '准确率 91.2%。'),
            review('c2', 'confirm', '训练用了 8 块 P100，耗时 3.5 天。'),
            review('c3', 'reject'),
        ], [{'field': 'limitations'}])
        kc = next(f for f in s['fields'] if f['field'] == 'key_conclusions')
        assert kc['precision'] == pytest.approx(0.5)
        assert kc['usable'] == pytest.approx(1.0)
        assert kc['coverage'] == pytest.approx(1.0)

    def test_zero_denominator_is_none(self):
        """⭐ 分母为 0 的比例是 None；但覆盖的分母是"陈述总数"，非零就给数字。

        这正是两条口径的区别：没审过 → 精度不可知（None），
        但"5 条里 0 条审过"是确凿的 0% 覆盖。
        """
        s = summarize(REPORT, [])
        for f in s['fields']:
            assert f['precision'] is None
            assert f['usable'] is None
            assert f['recall'] is None
            assert f['coverage'] == 0.0  # 有陈述、没审 → 确实是 0%

    def test_recall_needs_both_sides(self):
        s = summarize(REPORT, [], [{'field': 'limitations'}])
        lim = next(f for f in s['fields'] if f['field'] == 'limitations')
        # 该字段机器 1 条 + 人工 1 条 → 有分母
        assert lim['recall'] == pytest.approx(0.5)

    def test_recall_none_when_machine_empty(self):
        """⭐ 机器没有陈述时召回不可计算，不是 100%。"""
        s = summarize({'items': []}, [], [{'field': 'methodology'}])
        assert s['totals']['recall'] is None
        assert '没有机器陈述' in s['note']

    def test_totals_sum_fields(self):
        s = summarize(REPORT, [review('c1', 'confirm', '准确率 91.2%。'), review('c3', 'reject')])
        assert s['totals']['total'] == 5
        assert s['totals']['reviewed'] == 2
        assert s['totals']['verbatim'] == 1
        assert s['totals']['rejected'] == 1

    def test_fields_sorted_by_total(self):
        s = summarize(REPORT, [])
        totals = [f['total'] for f in s['fields']]
        assert totals == sorted(totals, reverse=True)

    def test_has_data_false_when_empty(self):
        s = empty_summary()
        assert s['has_data'] is False
        assert '不可用' in s['note']

    def test_has_data_true_with_manual_only(self):
        s = summarize({'items': []}, [], [{'field': 'methodology'}])
        assert s['has_data'] is True

    def test_definitions_present(self):
        s = summarize(REPORT, [])
        assert set(s['definitions']) == {'precision', 'usable', 'coverage', 'recall'}


class TestApi:
    def test_endpoint_matches_module(self, env):
        """⭐ 接口与模块必须给出同一组数字。"""
        from litprism.semantics import payload_hash

        _, kb, _, client = env
        pid = paper(kb, text='We train on eight P100 GPUs.')
        payload = {'_semantic_review': {'status': 'not_run', 'items': REPORT['items']}, '_citations': []}
        kb.save_analysis(pid, payload, model='test')

        d = client.get(f'/api/paper/{pid}/quality').json()
        direct = summarize(REPORT, [], [])
        assert d['totals']['total'] == direct['totals']['total']
        assert d['totals']['precision'] == direct['totals']['precision']
        assert d['analysis_hash'] == payload_hash(payload)

    def test_unknown_paper_404(self, env):
        _, _kb, _, client = env
        assert client.get('/api/paper/nope/quality').status_code == 404
