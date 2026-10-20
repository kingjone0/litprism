"""Real transport parsing/API/storage flow with offline response fixtures.

These tests verify wiring and fail-closed behavior, not model accuracy.
"""
import json

import pytest
from fastapi.testclient import TestClient

from litprism.config import Config, LLMConfig
from litprism.db import KnowledgeBase
from litprism.extract import analyze_paper, export_notes
from litprism.llm import OpenAICompatClient, OutputLimitError
from litprism.web import server


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.context.window_tokens = 128000
    cfg.context.output_reserve_tokens = cfg.llm.max_tokens = 8192
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Offline pipeline study', verify_level='verified')
    kb.add_chunks(pid, [
        {'text': 'Alpha evidence. '*160+'模型得分为71.8。模型使用两个层。', 'page': 1, 'section': '结果'},
        {'text': 'Beta context. '*160+'实验使用固定测试集。', 'page': 2, 'section': '结果'},
    ])
    llm = OpenAICompatClient(LLMConfig(max_tokens=8192))
    calls = []
    def post(*args, **kwargs):
        task = llm.last_request_settings['task']
        calls.append((task, kwargs['json']['messages']))
        if task == 'extract':
            content = {'key_conclusions': (
                '模型得分为71.8 [P1 p.1]。模型使用两个层 [P1 p.1]。'
                '模型得分为99.9 [P1 p.1]。未报告的新任务效果良好 [P2 p.2]。'),
                'field_evidence': {'key_conclusions': ['P1 p.1', 'P2 p.2']}}
        elif task == 'concepts':
            content = {'concepts': []}
        elif task == 'semantic_review':
            request = json.loads(kwargs['json']['messages'][-1]['content'])
            c = request['claims'][0]
            verdict = ('contradicted' if '99.9' in c['text'] else
                       'needs_review' if '未报告' in c['text'] else 'supported')
            content = {'reviews': [{'id': c['id'], 'verdict': verdict,
                                   'reason': 'Offline wiring fixture',
                                   'evidence': [request['sources'][0]['tag']]}]}
        else:
            assert task == 'semantic_cross_review'
            content = {'differences': []}
        class Response:
            status_code = 200
            def json(self):
                return {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(content, ensure_ascii=False)}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                              'prompt_cache_hit_tokens': 6, 'prompt_cache_miss_tokens': 4}}
        return Response()
    monkeypatch.setattr('litprism.llm.requests.post', post)
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    monkeypatch.setattr(server, 'build_llm', lambda _: llm)
    api = TestClient(server.create_app(cfg))
    yield cfg, kb, pid, llm, calls, api
    kb.close()


def test_plan_to_review_to_export_and_human_revoke(pipeline):
    cfg, kb, pid, llm, calls, api = pipeline
    result = analyze_paper(kb, cfg, llm, pid, force=True)
    assert result['_coverage']['reading_mode'] == 'whole'
    report = result['_semantic_review']
    assert report['supported'] == report['pending'] == 2
    assert not report['passed']
    assert '99.9' not in result['key_conclusions']
    assert '未报告' not in result['key_conclusions']
    usage = result['_coverage']['usage']
    assert usage['calls'] == len(calls)
    assert usage['input'] == 10*len(calls) and usage['output'] == 5*len(calls)
    assert usage['unknown_usage_calls'] == 0
    assert set(usage['per_task']) == {'extract', 'concepts', 'semantic_review', 'semantic_cross_review'}
    assert report['usage']['calls'] == sum(t.startswith('semantic_') for t, _ in calls)
    stored = kb.get_analysis(pid)['payload']
    assert stored['_coverage']['usage'] == usage
    files = export_notes(kb, cfg, pid)
    markdown = next(p for p in files if p.suffix == '.md').read_text(encoding='utf-8')
    assert '71.8' in markdown and '99.9' not in markdown
    detail = api.get(f'/api/paper/{pid}/semantic').json()
    rejected = next(c for c in report['items'] if c['status'] == 'contradicted')
    path = f"/api/paper/{pid}/semantic/{rejected['id']}/review"
    reviewed = api.post(path, json={'analysis_hash': detail['analysis_hash'], 'action': 'confirm',
                        'note': '人工对照原文数值', 'reviewed_text': '模型得分为71.8。'})
    assert reviewed.status_code == 200
    sid = reviewed.json()['knowledge_id']
    revoked = api.post(path, json={'analysis_hash': detail['analysis_hash'],
                                  'action': 'revoke', 'note': '人工撤销本次入库'})
    assert revoked.status_code == 200
    assert kb._con.execute('SELECT status FROM evidence_sources WHERE id=?', (sid,)).fetchone()[0] == 'revoked'
    # Human correction is separate knowledge, never mutates machine claims.
    assert kb.get_analysis(pid)['payload'] == stored


def test_truncated_local_review_stays_pending_with_usage(pipeline, monkeypatch):
    cfg, kb, pid, llm, calls, _ = pipeline
    original = llm.chat_json
    def fail(messages, *, task=None):
        if task == 'semantic_review':
            llm.last_usage = {'prompt_tokens': 123, 'completion_tokens': 4096}
            raise OutputLimitError('controlled truncation')
        return original(messages, task=task)
    monkeypatch.setattr(llm, 'chat_json', fail)
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False)
    assert result['_semantic_review']['supported'] == 0
    assert not result['_semantic_review']['passed']
    assert not result['key_conclusions']
    usage = result['_coverage']['usage']
    assert usage['per_task']['semantic_review']['calls'] == 1
    assert usage['per_task']['semantic_review']['failed_calls'] == 1
    assert usage['per_task']['semantic_review']['output'] == 4096
    assert not any(t == 'semantic_cross_review' for t, _ in calls)


def test_source_labels_are_included_in_window_decision(pipeline):
    from litprism.context.meter import Meter
    cfg, kb, _, _, _, _ = pipeline
    cfg.context.window_tokens = 5000
    cfg.context.output_reserve_tokens = 1024
    pid = kb.add_paper(title='Long title '+('reference metadata '*60), verify_level='verified')
    # 每块要足够大，30 块才会真的跨多批；否则 4k 窗口一次就装下了
    kb.add_chunks(pid, [{'text': 'alpha evidence. '*200, 'page': i, 'section': '结果'} for i in range(1, 31)])
    class Model:
        name = 'offline:window-envelope'
        def __init__(self):
            self.messages = []
        def chat_json(self, messages, **kwargs):
            self.messages.append(messages)
            return {}
    model = Model()
    result = analyze_paper(kb, cfg, model, pid, force=True, with_concepts=False)
    assert result['_coverage']['reading_mode'] == 'batched'
    assert result['_coverage']['total_chunks'] == 30
    # 拆成几批取决于窗口估算；这里只要求"窗口判断里带上了原文标签"这一事实。
    assert model.messages, '至少要发出一次请求'
    assert any('Long title' in str(m) for m in model.messages)
    assert all(sum(Meter(cfg).estimate_message(m) for m in messages) <= cfg.context.available_tokens
               for messages in model.messages)


def test_failed_count_gate_is_not_reported_as_complete(pipeline, monkeypatch):
    cfg, kb, pid, llm, _, _ = pipeline
    original = llm.chat_json
    def sparse(messages, *, task=None):
        if task == 'extract':
            return {'key_conclusions': '模型得分为71.8 [P1 p.1]。'}
        return original(messages, task=task)
    monkeypatch.setattr(llm, 'chat_json', sparse)
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False)
    assert not result['_coverage']['complete']
    # 模型有返回但字段缺失 → 记 flagged，不是"完全没产出"
    assert result['_coverage']['flagged_units']
    assert result['_coverage']['uncovered_units'] == []
    assert any(not g['passed'] for g in result['_coverage']['gates'])


def test_candidates_are_persisted_before_semantic_review(pipeline):
    """候选陈述必须先落盘，且与复核结果共用同一套确定性 ID。"""
    cfg, kb, pid, llm, calls, api = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False)
    candidates = payload['_candidates']
    assert candidates and all({'id', 'field', 'text', 'batch'} <= set(c) for c in candidates)
    reviewed = {i['id'] for i in payload['_semantic_review']['items']}
    assert {c['id'] for c in candidates} == reviewed


def test_semantic_review_can_be_skipped_with_candidates_kept(pipeline):
    """机器复核可关：候选、原文、人工审核入口都要留着。"""
    cfg, kb, pid, llm, calls, api = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False,
                            semantic_review=False)
    assert payload['_semantic_review']['status'] == 'skipped_by_user'
    assert [c[0] for c in calls if c[0].startswith('semantic_')] == []
    assert payload['_candidates']
    # 未跑机器复核时正文保留原始提取，不因"没有 supported"而清空
    assert payload['key_conclusions'].strip()


def test_persistent_run_records_the_skipped_review_option(pipeline):
    """开关要写进运行清单：关闭复核的运行，记录和结果都必须是"按设置跳过"。"""
    from litprism.analysis_runs import AnalysisRuns
    cfg, kb, pid, llm, calls, api = pipeline
    store = AnalysisRuns(kb)
    rid = store.create(cfg, llm, pid, None, False, False)['id']
    done = store.execute(rid, cfg, llm)
    assert done['semantic_review'] is False
    assert kb.get_analysis(pid)['payload']['_semantic_review']['status'] == 'skipped_by_user'


def test_candidates_are_persisted_before_semantic_review(pipeline):
    """候选陈述必须先落盘，且与复核结果共用同一套确定性 ID。"""
    cfg, kb, pid, llm, calls, api = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False)
    candidates = payload['_candidates']
    assert candidates and all({'id', 'field', 'text', 'batch'} <= set(c) for c in candidates)
    reviewed = {i['id'] for i in payload['_semantic_review']['items']}
    assert {c['id'] for c in candidates} == reviewed


def test_semantic_review_can_be_skipped_with_candidates_kept(pipeline):
    """机器复核可关：候选、原文、人工审核入口都要留着。"""
    cfg, kb, pid, llm, calls, api = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False,
                            semantic_review=False)
    assert payload['_semantic_review']['status'] == 'skipped_by_user'
    assert [c[0] for c in calls if c[0].startswith('semantic_')] == []
    assert payload['_candidates']
    # 未跑机器复核时正文保留原始提取，不因"没有 supported"而清空
    assert payload['key_conclusions'].strip()



def test_abstract_only_paper_is_flagged_in_analysis(pipeline):
    """只有题录摘要的论文：解读必须明确标注，不能因为 N/N 块送达就算整篇读完。"""
    cfg, kb, pid, llm, calls, api = pipeline
    aid = kb.add_paper(title='Abstract only study', abstract='一句话摘要', verify_level='verified',
                       material_level='abstract')
    kb.add_chunks(aid, [{'text': '标题与摘要。模型得分为71.8，实验使用固定测试集。' * 12,
                         'page': None, 'section': '题录与摘要'}])
    payload = analyze_paper(kb, cfg, llm, aid, force=True, with_concepts=False, semantic_review=False)
    coverage = payload['_coverage']
    assert coverage['material_level'] == 'abstract'
    assert coverage['partial_material'] is True
    assert '仅题录与摘要' in payload['field_notes']


def test_structure_damage_is_retried_with_strict_json_and_archived(pipeline, monkeypatch):
    """结构损坏要单独重试（不是复用字段修复），并留下原始片段。"""
    cfg, kb, pid, llm, calls, _ = pipeline
    original = llm.chat_json
    state = {'n': 0}

    def flaky(messages, *, task=None):
        if task != 'extract':
            return original(messages, task=task)
        state['n'] += 1
        if state['n'] == 1:
            return {'这段被我写坏了': 'xxx'}          # 未知字段 → 结构损坏
        return {'research_question': '问题？[P1 p.1]', 'methodology': '方法。[P1 p.1]',
                'key_conclusions': '结论。[P1 p.1]'}

    monkeypatch.setattr(llm, 'chat_json', flaky)
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    coverage = result['_coverage']
    assert coverage['structure_failures'], '结构损坏的原始片段必须留档'
    assert '这段被我写坏了' in coverage['structure_failures'][0]['raw']
    assert state['n'] >= 2, '应使用严格 JSON 提示重试一次'
    # 采纳的是重试后的结果，闸门里不该残留"结构损坏"这一项
    assert not any(g['name'] == 'structure_valid' for g in coverage['gates'][0]['results'])


def test_structure_damage_is_retried_with_strict_json_and_archived(pipeline, monkeypatch):
    """结构损坏要单独重试（不是复用字段修复），并留下原始片段。"""
    cfg, kb, pid, llm, calls, _ = pipeline
    original = llm.chat_json
    state = {'n': 0}

    def flaky(messages, *, task=None):
        if task != 'extract':
            return original(messages, task=task)
        state['n'] += 1
        if state['n'] == 1:
            return {'这段被我写坏了': 'xxx'}          # 未知字段 → 结构损坏
        return {'research_question': '问题？[P1 p.1]', 'methodology': '方法。[P1 p.1]',
                'key_conclusions': '结论。[P1 p.1]'}

    monkeypatch.setattr(llm, 'chat_json', flaky)
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    coverage = result['_coverage']
    assert coverage['structure_failures'], '结构损坏的原始片段必须留档'
    assert '这段被我写坏了' in coverage['structure_failures'][0]['raw']
    assert state['n'] >= 2, '应使用严格 JSON 提示重试一次'
    # 采纳的是重试后的结果，闸门里不该残留结构损坏项
    assert not any(g['name'] == 'structure_valid' for g in coverage['gates'][0]['results'])


def test_coverage_reports_section_counts(pipeline):
    cfg, kb, pid, llm, calls, _ = pipeline
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    coverage = result['_coverage']
    assert coverage['sections_total'] >= 1
    assert isinstance(coverage['section_claims'], dict)
    assert coverage['section_claims'], '正常解读至少要有一节的陈述计数'


def test_coverage_reports_section_counts(pipeline):
    cfg, kb, pid, llm, calls, _ = pipeline
    result = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    coverage = result['_coverage']
    assert coverage['sections_total'] >= 1
    assert isinstance(coverage['section_claims'], dict)
    assert coverage['section_claims'], '正常解读至少要有一节的陈述计数'
