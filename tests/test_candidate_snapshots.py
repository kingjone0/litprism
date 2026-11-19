import json

import pytest
from test_planned_pipeline import pipeline

from litprism.analysis_runs import AnalysisRuns
from litprism.candidate_snapshots import pending
from litprism.db import KnowledgeBase
from litprism.extract import analyze_paper
from litprism.knowledge import get_source


def test_failed_review_keeps_persistent_candidates_without_replacing_published_analysis(pipeline, monkeypatch):
    cfg, kb, pid, llm, _, api = pipeline
    kb.save_analysis(pid, {'key_conclusions': 'OLD_PUBLISHED'})
    old = kb.get_analysis(pid)['payload']
    def fail(*args, **kwargs):
        candidate = pending(kb, pid)
        assert candidate and candidate['payload']['_candidates']
        assert kb.get_analysis(pid)['payload'] == old
        raise RuntimeError('offline review failure')
    monkeypatch.setattr('litprism.semantics.run_review', fail)
    store = AnalysisRuns(kb)
    rid = store.create(cfg, llm, pid, with_concepts=False)['id']
    detail = store.execute(rid, cfg, llm)
    assert detail['status'] == 'failed' and detail['candidate_count']
    assert detail['candidate_snapshot_id'].startswith('ac_')
    reopened = KnowledgeBase(cfg)
    try:
        assert pending(reopened, pid)['id'] == detail['candidate_snapshot_id']
        assert reopened.get_analysis(pid)['payload'] == old
    finally:
        reopened.close()
    data = api.get(f'/api/paper/{pid}/semantic').json()
    assert data['stage'] == 'candidate' and not data['report']['passed']
    claim = next(c for c in data['candidates'] if '71.8' in c['text'])
    body = {'action': 'confirm', 'note': '核对当前原文得分及条件', 'reviewed_text': '模型得分为71.8。',
            'analysis_hash': data['analysis_hash']}
    response = api.post(f'/api/paper/{pid}/semantic/{claim["id"]}/review', json=body)
    assert response.status_code == 200, response.text
    source = get_source(kb, response.json()['knowledge_id'])
    assert source['status'] == 'manual_confirmed' and 'chunk_id=' in source['content']
    revoked = api.post(f'/api/paper/{pid}/semantic/{claim["id"]}/review', json={**body, 'action': 'revoke'})
    assert revoked.status_code == 200 and get_source(kb, source['id'])['status'] == 'revoked'
    assert kb.get_analysis(pid)['payload'] == old


def test_skipping_machine_review_allows_direct_human_confirmation(pipeline):
    cfg, kb, pid, llm, _, api = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    assert payload['_candidate_snapshot_id']
    assert pending(kb, pid) is None  # finalized branch is shown instead
    data = api.get(f'/api/paper/{pid}/semantic').json()
    assert data['report']['items'] and all(c['status'] == 'candidate' for c in data['report']['items'])
    claim = data['candidates'][0]
    response = api.post(f'/api/paper/{pid}/semantic/{claim["id"]}/review', json={
        'action': 'confirm', 'analysis_hash': data['analysis_hash'], 'note': '人工核对原文',
        'reviewed_text': '已核对的得分为71.8。'})
    assert response.status_code == 200, response.text
    assert api.get(f'/api/paper/{pid}/quality').json()['totals']['reviewed'] == 1


def test_material_or_analysis_change_cannot_confirm_an_old_candidate(pipeline, monkeypatch):
    cfg, kb, pid, llm, _, api = pipeline
    monkeypatch.setattr('litprism.semantics.run_review', lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False)
    data = api.get(f'/api/paper/{pid}/semantic').json()
    cid = data['candidates'][0]['id']
    kb.add_chunks(pid, [{'text': 'new material '+('evidence '*100), 'page': 3}])
    assert pending(kb, pid) is None
    response = api.post(f'/api/paper/{pid}/semantic/{cid}/review', json={
        'action': 'confirm', 'analysis_hash': data['analysis_hash'], 'note': 'stale', 'reviewed_text': 'stale'})
    assert response.status_code == 409
    assert not kb._con.execute('SELECT 1 FROM evidence_sources').fetchone()


def test_candidate_snapshot_survives_backup_and_pending_raw_text_is_not_exported(pipeline, tmp_path):
    from litprism.backup import create_backup, restore_backup
    from litprism.extract import format_analysis, export_notes
    cfg, kb, pid, llm, _, _ = pipeline
    payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    assert '99.9' not in format_analysis(kb.get_paper(pid), payload)
    with pytest.raises(ValueError, match='候选'):
        export_notes(kb, cfg, pid)
    archive = tmp_path / 'candidate-backup.zip'
    create_backup(kb, cfg, archive)
    target = tmp_path / 'restored-candidates'
    restore_backup(archive, target)
    con = __import__('sqlite3').connect(target/'litprism.db')
    try:
        assert con.execute('SELECT 1 FROM analysis_versions WHERE id=?', (payload['_candidate_snapshot_id'],)).fetchone()
    finally:
        con.close()


def test_per_instruction_candidate_persistence_on_midway_interruption(pipeline, monkeypatch):
    from litprism.plan import Directive, Plan
    cfg, kb, pid, llm, _, api = pipeline

    plan = Plan(
        paper_id=pid, mode='whole', context_tokens=100,
        directives=[Directive(id='d1', runs=[], quota=5), Directive(id='d2', runs=[], quota=5)],
        note='2 directives plan',
    )
    monkeypatch.setattr('litprism.plan.build_plan', lambda *a, **k: plan)

    orig_run_with_repair = __import__('litprism.extract', fromlist=['_run_with_repair'])._run_with_repair
    call_count = 0
    def failing_run_with_repair(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError('simulated interruption at directive 2')
        return orig_run_with_repair(*args, **kwargs)

    monkeypatch.setattr('litprism.extract._run_with_repair', failing_run_with_repair)

    with pytest.raises(RuntimeError, match='simulated interruption at directive 2'):
        analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)

    # 1. 终态发布守卫未受触碰：analyses 保持为空（未发布）
    assert kb.get_analysis(pid) is None

    # 2. 候选快照已即时落库第 1 条指令的成果
    cand = pending(kb, pid)
    assert cand is not None
    payload = cand['payload']
    assert payload['_incomplete'] is True
    assert payload['_progress']['completed'] == 1
    assert payload['_progress']['total'] == 2
    assert payload['_progress']['unit'] == 'd1'
    assert len(payload['_candidates']) > 0

    # 3. Web API 接口可正常读取该未完成候选快照
    sem = api.get(f'/api/paper/{pid}/semantic').json()
    assert sem['stage'] == 'candidate'
    assert sem['incomplete'] is True
    assert sem['progress']['completed'] == 1
    assert len(sem['candidates']) > 0

    # 4. 人工可以直接对未完成候选进行单项裁决并沉淀独立知识
    claim = sem['candidates'][0]
    review_resp = api.post(f'/api/paper/{pid}/semantic/{claim["id"]}/review', json={
        'action': 'confirm', 'note': '人工核对第1条指令原文', 'reviewed_text': '人工核对结论。',
        'analysis_hash': sem['analysis_hash']})
    assert review_resp.status_code == 200
    source = get_source(kb, review_resp.json()['knowledge_id'])
    assert source['status'] == 'manual_confirmed'
    # 终态表依然未发布
    assert kb.get_analysis(pid) is None

    # 5. 后续重试/继续执行到全部完成时，终态发布接管并置换为正式解读
    call_count = 0
    monkeypatch.setattr('litprism.extract._run_with_repair', orig_run_with_repair)
    completed_payload = analyze_paper(kb, cfg, llm, pid, force=True, with_concepts=False, semantic_review=False)
    assert completed_payload['_candidate_snapshot_id']
    # 正式分析发布到 analyses 表
    assert kb.get_analysis(pid) is not None
    # pending 状态已解除（已发布，不再处于待审候选草稿态）
    assert pending(kb, pid) is None
    # API 读取展示正式解读
    final_sem = api.get(f'/api/paper/{pid}/semantic').json()
    assert final_sem['stage'] == 'analysis'
    assert final_sem['incomplete'] is False


def test_analysis_runs_detail_reports_incomplete_candidate_progress_on_interruption(pipeline, monkeypatch):
    from litprism.plan import Directive, Plan
    cfg, kb, pid, llm, _, _ = pipeline

    plan = Plan(
        paper_id=pid, mode='whole', context_tokens=100,
        directives=[Directive(id='d1', runs=[], quota=5), Directive(id='d2', runs=[], quota=5)],
        note='2 directives plan',
    )
    monkeypatch.setattr('litprism.plan.build_plan', lambda *a, **k: plan)

    orig_run_with_repair = __import__('litprism.extract', fromlist=['_run_with_repair'])._run_with_repair
    call_count = 0
    def failing_run_with_repair(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError('interrupted at directive 2')
        return orig_run_with_repair(*args, **kwargs)

    monkeypatch.setattr('litprism.extract._run_with_repair', failing_run_with_repair)

    store = AnalysisRuns(kb)
    rid = store.create(cfg, llm, pid, with_concepts=False, semantic_review=False)['id']
    detail = store.execute(rid, cfg, llm)
    assert detail['status'] == 'failed'
    assert detail['candidate_incomplete'] is True
    assert detail['candidate_progress']['completed'] == 1
    assert detail['candidate_progress']['total'] == 2
    assert detail['candidate_progress']['unit'] == 'd1'
    assert detail['candidate_count'] > 0
    assert detail['candidate_snapshot_id'].startswith('ac_')


