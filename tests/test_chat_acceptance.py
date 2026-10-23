"""N04 验收工具的度量逻辑。

工具本身也会算错 —— 这里锁住四项指标的定义：
分母为 0 一律 None（n/a）；拒答与答错分开；调用失败单独计数。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import pytest
from chat_acceptance import (  # noqa: E402  # noqa: E402
    build_llm,
    check_citations,
    judge,
    pct,
    quality_passed,
    run_case,
    verdict,
)
from test_workflow_guards import env as workflow_env  # noqa: E402, F401
from test_workflow_guards import paper

env = workflow_env


def test_acceptance_preserves_checked_refusal_sources_like_web(env):
    import json
    cfg, kb, model, _ = env
    pid = paper(kb)
    def respond(messages, task='', **kwargs):
        return {'answer': '证据不足，背景 [P1 p.1]', 'insufficient_evidence': True} if task == 'qa' else {}
    model.chat_json = respond
    cid = kb.add_conversation('Refused sources', scope={'paper_ids': [pid]})
    result = run_case({'cfg': cfg, 'llm': model}, kb,
                      {'id': 'r', 'question': 'alphatopic', 'kind': 'refuse'}, cid, [pid])
    assert result['refused'] and result['valid_citations'] == 1
    assert json.loads(kb.get_messages(cid)[-1]['citations'])[0]['paper_id'] == pid

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
    @pytest.mark.parametrize('status', ['answered', 'refused', 'needs_review'])
    def test_valid_in_scope_sources_are_independent_of_publication_status(self, status):
        r = row('a', 'answerable', citations=1, valid=1, papers=['p1'], refused=status == 'refused')
        r['status'] = status
        assert judge([r], cases(a=('answerable',)))['范围遵守率'] == {'num': 1, 'den': 1, 'ratio': 1.0}

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


def test_actual_references_not_retrieval_inventory(env):
    _, kb, _, _ = env
    a = paper(kb, title='A')
    b = paper(kb, title='B')
    excerpts = [{**kb.get_chunks(pid)[0], 'chunk_id': kb.get_chunks(pid)[0]['id'],
                 'tag': f'P{i}', 'paper_id': pid} for i, pid in enumerate((a, b), 1)]
    assert len(check_citations(kb, excerpts, 'Only A [P1 p.1]')) == 1
    bad = check_citations(kb, excerpts, 'Invented [P99 p.999]')
    assert len(bad) == 1 and not bad[0]['valid']
    assert not check_citations(kb, excerpts, 'Wrong page [P1 p.999]')[0]['valid']
    assert check_citations(kb, excerpts, 'No reference at all') == []


def test_followup_really_reads_first_turn_and_counts_only_used_paper(env):
    cfg, kb, model, _ = env
    paper(kb, title='A', text='alpha evidence')
    paper(kb, title='B', text='alpha evidence')
    cid = kb.add_conversation('Audit')
    agent = {'cfg': cfg, 'llm': model}
    first = run_case(agent, kb, {'id': 'a', 'question': 'alpha FIRST_QUESTION'}, cid, None)
    second = run_case(agent, kb, {'id': 'b', 'kind': 'followup', 'question': '它的 alpha 局限呢？'}, cid, None)
    prompt = str([m for task, m in model.calls if task == 'qa'][-1])
    assert 'FIRST_QUESTION' in prompt and len(kb.get_messages(cid)) == 4
    assert len(second['papers']) == 1 and first['valid_citations'] == 1
    assert second['usage']['calls'] == 2  # expand + answer, both unreported
    assert second['usage']['unknown_usage_calls'] == 2


def test_blocked_draft_cannot_pass_citation_or_cross_or_followup(env):
    cfg, kb, model, _ = env
    a = paper(kb, text='alpha evidence')
    model.text = 'UNSUPPORTED_DRAFT [P99 p.999]'
    cid = kb.add_conversation('Blocked')
    result = run_case({'cfg': cfg, 'llm': model}, kb, {'id': 'f', 'kind': 'followup', 'question': 'alpha'}, cid, [a])
    assert result['status'] == 'invalid_citation' and not result['refused']
    assert 'UNSUPPORTED_DRAFT' not in result['answer'] and result['valid_citations'] == 0
    assert result['citations'] == 1 and result['papers'] == []
    result['parent_usable'] = True
    metrics = judge([result], {'f': {'kind': 'followup', '_expect_ids': [a]}})
    assert metrics['引用命中率']['ratio'] == 0 and metrics['追问可用率']['num'] == 0
    assert not quality_passed([result], {'f': {'kind': 'followup', '_expect_ids': [a]}})


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_live_builder_uses_selected_runtime_without_network(env, monkeypatch, protocol):
    from types import SimpleNamespace

    from litprism.providers import ProviderStore
    cfg, kb, _, _ = env
    store = ProviderStore(kb)
    p = store.create(name='local test', base_url='http://localhost/v1', chat_model='deepseek-v4.1-flash', protocol=protocol)
    store.set_models(p.id, [{'model_id': 'deepseek-v4.1-flash', 'window_tokens': 1000000,
                             'max_output_tokens': 384000}])
    store.set_active_model(p.id, 'deepseek-v4.1-flash')
    calls = []
    def factory(runtime, **kwargs):
        calls.append((runtime, kwargs))
        return SimpleNamespace(name='offline-factory')
    monkeypatch.setattr('litprism.llm.build_llm_for', factory)
    client = build_llm('live', cfg, kb)
    assert client.runtime_config is calls[0][0]
    assert calls[0][1]['model'] == 'deepseek-v4.1-flash'
    assert calls[0][1]['protocol'] == protocol
    assert client.runtime_config.llm.model_window_tokens == 1000000
    assert client.runtime_config.llm.model_max_output_tokens == 384000
    assert cfg.llm.base_url != calls[0][1]['base_url']


def test_live_builder_does_not_silently_mock(env):
    cfg, kb, _, _ = env
    with pytest.raises(ValueError, match='尚未选择模型'):
        build_llm('live', cfg, kb)


def test_error_never_passes_followup_even_with_matching_sources():
    r = row('f', 'followup', citations=1, valid=1, papers=['p1'], error=True)
    assert judge([r], cases(f=('followup', ['p1'])))['追问可用率']['num'] == 0


def test_cli_stub_persists_chained_turns_and_freezes_inputs(tmp_path, monkeypatch):
    import json

    import chat_acceptance
    monkeypatch.setenv('LITPRISM_HOME', str(tmp_path))
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    cfg = Config()
    cfg.app.data_dir = str(tmp_path / 'data')
    kb = KnowledgeBase(cfg)
    pid = paper(kb, title='Frozen Paper', text='alpha method evidence')
    kb.close()
    cases_file = tmp_path / 'cases.json'
    cases_file.write_text(json.dumps({'cases': [
        {'id': 'a', 'kind': 'answerable', 'question': 'alpha', 'scope_titles': ['Frozen Paper'],
         'expect_cite_titles': ['Frozen Paper']},
        {'id': 'b', 'kind': 'followup', 'question': '它的 alpha 方法呢？', 'followup_of': 'a'},
        {'id': 'c', 'kind': 'followup', 'question': '继续 alpha', 'followup_of': 'b'}]}), encoding='utf-8')
    out = tmp_path / 'out.json'
    monkeypatch.setattr(sys, 'argv', ['audit', '--home', str(tmp_path), '--cases', str(cases_file), '--out', str(out)])
    assert chat_acceptance.main() == 0
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['quality_passed'] is None and data['frozen']['cases_sha256']
    assert len({r['conversation_id'] for r in data['rows']}) == 1
    assert all(r['scope'] == [pid] for r in data['rows'])
    kb = KnowledgeBase(cfg)
    assert len(kb.get_messages(data['rows'][0]['conversation_id'])) == 6
    kb.close()


def test_live_quality_failure_is_not_success_exit(tmp_path, monkeypatch):
    import json

    import chat_acceptance
    monkeypatch.setenv('LITPRISM_HOME', str(tmp_path))
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    cfg = Config()
    cfg.app.data_dir = str(tmp_path / 'data')
    kb = KnowledgeBase(cfg)
    paper(kb, text='alpha evidence')
    kb.close()
    spec = tmp_path / 'cases.json'
    spec.write_text(json.dumps({'cases': [{'id': 'r', 'kind': 'refuse', 'question': 'alpha'}]}), encoding='utf-8')
    monkeypatch.setattr(chat_acceptance, 'build_llm', lambda *args: chat_acceptance.StubLLM())
    out = tmp_path / 'fail.json'
    monkeypatch.setattr(sys, 'argv', ['audit', '--home', str(tmp_path), '--cases', str(spec),
                                     '--mode', 'live', '--yes', '--out', str(out)])
    assert chat_acceptance.main() == 1
    assert json.loads(out.read_text(encoding='utf-8'))['quality_passed'] is False
