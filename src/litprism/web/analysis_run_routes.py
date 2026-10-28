"""Explicit persistent analysis workflow; legacy analyze API remains compatible."""
from __future__ import annotations

import copy
import threading

from fastapi import HTTPException

from ..analysis_runs import AnalysisRuns, RunConflict
from ..materials import reading_status


def register_analysis_runs(app, kb, cfg, task_llm):
    store = AnalysisRuns(kb)
    jobs, lock = {}, threading.RLock()

    def detail(rid):
        try:
            with lock:
                result = store.detail(rid)
                task = app.state.tasks.items.get(jobs.get(rid)) or {}
                result.update(active=task.get('status') in ('queued', 'running', 'cancelling'), task_id=task.get('id'))
                return result
        except KeyError as exc:
            raise HTTPException(404, '分析运行不存在。') from exc

    def schedule(rid, client, run_cfg, limits=None, retry=False):
        with lock:
            ensure_idle(store.row(rid)['paper_id'])
            task = app.state.tasks.submit(lambda: store.execute(
                rid, run_cfg, client, limits=limits, retry_failed=retry))
            jobs[rid] = task['id']
        return {'run_id': rid, 'task_id': task['id']}

    def active_run(pid):
        for rid, tid in jobs.items():
            task = app.state.tasks.items.get(tid) or {}
            if task.get('status') in ('queued', 'running', 'cancelling') and store.row(rid)['paper_id'] == pid:
                return detail(rid)
        return None

    def ensure_idle(pid):
        if active_run(pid):
            raise HTTPException(409, '该论文已有解读正在执行，请查看运行记录，勿重复发起。')

    @app.get('/api/paper/{pid}/reading')
    def prepare_reading(pid: str):
        with lock, kb._tx_lock:
            paper = kb.resolve_paper(pid)
            if not paper or paper.get('deleted_at'):
                raise HTTPException(404, '论文不存在或已回收。')
            return reading_status(kb, paper) | {'title': paper.get('display_title') or paper['title'],
                                               'active_run': active_run(paper['id'])}

    @app.get('/api/paper/{pid}/analysis-runs')
    def runs(pid: str):
        paper = kb.resolve_paper(pid)
        if not paper or paper.get('deleted_at'):
            raise HTTPException(404, '论文不存在或已回收。')
        return {'items': [detail(row['id']) for row in store.list(paper['id'])]}

    @app.get('/api/analysis-runs/{rid}')
    def get_run(rid: str):
        return detail(rid)

    @app.post('/api/paper/{pid}/analysis-runs')
    def start_run(pid: str, req: dict):
        with lock, kb._tx_lock:
            paper = kb.resolve_paper(pid)
            if not paper or paper.get('deleted_at'):
                raise HTTPException(404, '论文不存在或已回收。')
            ensure_idle(paper['id'])
            prepared = reading_status(kb, paper)
            if 'material_id' in req and req['material_id'] != prepared['material_id']:
                raise HTTPException(409, '当前材料已变化，请关闭后重新打开解读窗口。')
            if not prepared['ready']:
                raise HTTPException(400, prepared['reason'])
            client, run_cfg = task_llm(), copy.deepcopy(cfg)
            try:
                created = store.create(run_cfg, client, paper['id'], req.get('budgets'),
                                       req.get('with_concepts', True), req.get('semantic_review', True))
            except (ValueError, RunConflict) as exc:
                raise HTTPException(400, str(exc)) from exc
            return schedule(created['id'], client, run_cfg)

    @app.post('/api/analysis-runs/{rid}/resume')
    def resume(rid: str, req: dict):
        with lock, kb._tx_lock:
            run = detail(rid)
            from ..analysis_runs import budgets
            try:
                budgets(req.get('budgets'), run['budgets'])
                if type(req.get('retry_failed', False)) is not bool:
                    raise ValueError('失败重试选项必须为布尔值。')
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            ensure_idle(run['paper_id'])
            paper = kb.get_paper(run['paper_id'])
            if 'material_id' in req and req['material_id'] != (paper or {}).get('current_material_id'):
                raise HTTPException(409, '当前材料已变化，请重新检查运行记录。')
            return schedule(rid, task_llm(), copy.deepcopy(cfg), req.get('budgets'), req.get('retry_failed', False))
