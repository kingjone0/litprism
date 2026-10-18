"""可观察、可协作取消的任务。取消不承诺撤销已发送的远端请求。"""
from __future__ import annotations

import asyncio
import contextvars
import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import File, HTTPException, UploadFile

_current = contextvars.ContextVar('litprism_task', default=None)


class TaskCancelled(RuntimeError):
    pass


def checkpoint(message: str = ''):
    task = _current.get()
    if task:
        if task['cancel'].is_set():
            raise TaskCancelled('任务已取消；已完成的导入不会撤销。')
        if message:
            task['progress'] = message


class TaskManager:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='litprism-task')
        self.items = {}
        self.lock = threading.Lock()

    def submit(self, fn):
        with self.lock:
            if len([t for t in self.items.values() if t['status'] in ('queued', 'running', 'cancelling')]) >= 8:
                raise HTTPException(429, '任务队列已满，请稍后重试。')
            tid = 'job_'+uuid.uuid4().hex[:12]
            task = {'id': tid, 'status': 'queued', 'progress': '等待执行', 'cancel': threading.Event()}
            self.items[tid] = task
            finished = [k for k, v in self.items.items() if v['status'] in ('succeeded', 'failed', 'cancelled')]
            for old in finished[:-50]:
                del self.items[old]
        def run():
            token = _current.set(task)
            try:
                checkpoint()
                task['status'] = 'running'
                task['progress'] = '开始处理'
                result = fn()
                if asyncio.iscoroutine(result):
                    result = asyncio.run(result)
                with self.lock:
                    checkpoint()
                    task.update(status='succeeded', result=result, progress='完成')
            except Exception as exc:
                cancelled = task['cancel'].is_set() or isinstance(exc, TaskCancelled)
                task.update(status='cancelled' if cancelled else 'failed', error=str(getattr(exc, 'detail', exc)))
            finally:
                _current.reset(token)
        self.pool.submit(run)
        return {'id': tid}

    def get(self, tid):
        task = self.items.get(tid)
        if task is None:
            raise HTTPException(404, '任务不存在；任务状态不跨应用重启保存。')
        return task

    def cancel(self, tid):
        with self.lock:
            task = self.get(tid)
            if task['status'] in ('queued', 'running', 'cancelling'):
                task['cancel'].set()
                task['status'] = 'cancelling'
            return task['status']


def register_tasks(app):
    manager = TaskManager()
    app.state.tasks = manager
    def endpoint(path):
        return next(r.endpoint for r in app.routes if getattr(r, 'path', None) == path and 'POST' in (getattr(r, 'methods', None) or []))

    @app.post('/api/tasks')
    def start_task(req: dict):
        from .web.server import CompareReq
        op, args = req.get('operation'), req.get('args') or {}
        if not isinstance(args, dict):
            raise HTTPException(400, '任务参数必须是对象。')
        operations = {
            'chat': lambda: endpoint('/api/chat/{conv_id}')(args['conv_id'], args.get('request') or {}),
            'analyze': lambda: endpoint('/api/analyze/{paper_id}')(args['paper_id'], force=True),
            'compare': lambda: endpoint('/api/compare')(CompareReq(**args)),
            'skill': lambda: endpoint('/api/skills/{skill_id}/run')(args['skill_id'], args.get('request') or {}),
            'batch': lambda: endpoint('/api/library/batch')(args),
        }
        if op not in operations:
            raise HTTPException(400, '不支持的任务。')
        return manager.submit(operations[op])

    @app.post('/api/tasks/upload')
    async def upload_task(files: Annotated[list[UploadFile], File()]):
        blobs, total = [], 0
        for f in files:
            data = await f.read(100*1024*1024+1)
            total += len(data)
            if total > 100*1024*1024:
                raise HTTPException(413, '本批文件超过 100 MB，请分批导入。')
            blobs.append((f.filename, data))
        async def invoke():
            uploads = [UploadFile(filename=name, file=io.BytesIO(data)) for name, data in blobs]
            try:
                return await endpoint('/api/upload')(uploads, analyze=False, verify=True)
            finally:
                for upload in uploads:
                    await upload.close()
        return manager.submit(invoke)

    @app.get('/api/tasks/{tid}')
    def task_status(tid: str):
        return {k: v for k, v in manager.get(tid).copy().items() if k != 'cancel'}

    @app.post('/api/tasks/{tid}/cancel')
    def cancel_task(tid: str):
        return {'status': manager.cancel(tid), 'note': '当前远端请求返回后停止；已完成导入不撤销。'}
