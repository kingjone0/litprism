"""Explicit persistent analysis workflow; legacy analyze API remains compatible."""
from __future__ import annotations

import copy
import threading

from fastapi import HTTPException

from ..analysis_runs import AnalysisRuns, RunConflict


def register_analysis_runs(app, kb, cfg, task_llm):
    store = AnalysisRuns(kb)
    jobs, lock = {}, threading.RLock()

    def detail(rid):
        try:
            return store.detail(rid)
        except KeyError as exc:
            raise HTTPException(404, '分析运行不存在。') from exc

    def schedule(rid, client, run_cfg, limits=None, retry=False):
        with lock:
            old = app.state.tasks.items.get(jobs.get(rid))
            if old and old['status'] in ('queued', 'running', 'cancelling'):
                raise HTTPException(409, '该运行已在队列或执行中，不能重复发起。')
            task = app.state.tasks.submit(lambda: store.execute(
                rid, run_cfg, client, limits=limits, retry_failed=retry))
            jobs[rid] = task['id']
        return {'run_id': rid, 'task_id': task['id']}

    @app.get('/api/paper/{pid}/analysis-runs')
    def runs(pid: str):
        paper = kb.resolve_paper(pid)
        if not paper or paper.get('deleted_at'):
            raise HTTPException(404, '论文不存在或已回收。')
        return {'items': store.list(paper['id'])}

    @app.get('/api/analysis-runs/{rid}')
    def get_run(rid: str):
        return detail(rid)

    @app.post('/api/paper/{pid}/analysis-runs')
    def start_run(pid: str, req: dict):
        paper = kb.resolve_paper(pid)
        if not paper or paper.get('deleted_at'):
            raise HTTPException(404, '论文不存在或已回收。')
        client, run_cfg = task_llm(), copy.deepcopy(cfg)
        try:
            created = store.create(run_cfg, client, paper['id'], req.get('budgets'), req.get('with_concepts', True))
        except (ValueError, RunConflict) as exc:
            raise HTTPException(400, str(exc)) from exc
        return schedule(created['id'], client, run_cfg)

    @app.post('/api/analysis-runs/{rid}/resume')
    def resume(rid: str, req: dict):
        detail(rid)
        from ..analysis_runs import budgets
        try:
            budgets(req.get('budgets'), detail(rid)['budgets'])
            if type(req.get('retry_failed', False)) is not bool:
                raise ValueError('失败重试选项必须为布尔值。')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return schedule(rid, task_llm(), copy.deepcopy(cfg), req.get('budgets'), req.get('retry_failed', False))
