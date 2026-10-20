"""Backup UI/API. Active connections stay bound to the old root until restart."""
from __future__ import annotations

import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from ..backup import (
    BackupError,
    activate_directory,
    cancel_activation,
    create_backup,
    inspect_backup,
    migrate_data,
    restore_backup,
)

MAX_UPLOAD = 2 * 1024**3


def register_backups(app, kb, cfg):
    lock = threading.RLock()
    state = {'switch': None, 'mutations': 0, 'switching': False}
    previews = {}

    @app.middleware('http')
    async def data_switch_guard(request, call_next):
        mutation = request.method not in ('GET', 'HEAD', 'OPTIONS')
        with lock:
            if mutation and (state['switch'] or state['switching']) and request.url.path != '/api/backups/cancel-switch':
                return JSONResponse(status_code=409, content={'detail': '已安排数据目录切换，请重启或取消切换后再修改数据。'})
            if mutation:
                state['mutations'] += 1
        try:
            return await call_next(request)
        finally:
            if mutation:
                with lock:
                    state['mutations'] -= 1

    def status():
        with lock:
            switch = state['switch']
            return {'data_dir': str(kb.db_path.parent), 'pending_directory': switch['data_dir'] if switch else None,
                    'requires_restart': bool(switch), 'credentials_note': '密钥不随备份迁移，恢复后请重新配置。'}

    app.state.backup_status = status

    @app.get('/api/backups/status')
    def backup_status():
        return status()

    @app.post('/api/backups/export')
    def export(req: dict):
        include = req.get('include_originals', False)
        if type(include) is not bool:
            raise HTTPException(400, '是否包含原文须为布尔值。')
        folder = tempfile.TemporaryDirectory(prefix='litprism-download-')
        try:
            path = Path(folder.name)/'litprism-backup.zip'
            create_backup(kb, cfg, path, include_originals=include)
            return FileResponse(path, filename='litprism-backup.zip', media_type='application/zip',
                                background=BackgroundTask(folder.cleanup))
        except (BackupError, OSError) as exc:
            folder.cleanup()
            raise HTTPException(400, str(exc)) from exc

    def prune():
        for token, entry in list(previews.items()):
            if time.monotonic()-entry['time'] > 3600:
                entry['path'].unlink(missing_ok=True)
                del previews[token]

    @app.post('/api/backups/inspect')
    async def inspect(file: Annotated[UploadFile, File()]):
        # Upload filenames never become paths; each token belongs to this app instance.
        with lock:
            prune()
            if len(previews) >= 3:
                raise HTTPException(429, '待恢复备份过多，请完成恢复或一小时后重试。')
        folder = cfg.cache_path/'backup-previews'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/(uuid.uuid4().hex+'.zip')
        retained = False
        try:
            total = 0
            with path.open('xb') as stream:
                while chunk := await file.read(1024**2):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise BackupError('浏览器上传超过 2 GiB，请使用 CLI 恢复。')
                    stream.write(chunk)
            manifest = await run_in_threadpool(inspect_backup, path)
            token = uuid.uuid4().hex
            with lock:
                previews[token] = {'path': path, 'time': time.monotonic(), 'content_hash': manifest['content_hash']}
                retained = True
            return {'token': token, 'manifest': manifest}
        except (BackupError, OSError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        finally:
            if not retained:
                path.unlink(missing_ok=True)
            await file.close()

    @app.post('/api/backups/restore')
    def restore(req: dict):
        with lock:
            prune()
            item = previews.get(req.get('token'))
        if not item:
            raise HTTPException(409, '备份预检已失效，请重新选择文件。')
        try:
            result = restore_backup(item['path'], req.get('target') or '', current_root=cfg.data_path,
                                    expected_hash=item['content_hash'])
            with lock:
                previews.pop(req['token'], None)
            item['path'].unlink(missing_ok=True)
            return result
        except (BackupError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete('/api/backups/previews/{token}')
    def discard_preview(token: str):
        with lock:
            item = previews.pop(token, None)
            if item:
                item['path'].unlink(missing_ok=True)
        return {'ok': True}

    @app.post('/api/backups/migrate')
    def migrate(req: dict):
        try:
            return migrate_data(kb, cfg, req.get('target') or '', include_originals=True)
        except (BackupError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/api/backups/activate')
    def activate(req: dict):
        with lock:
            manager = getattr(app.state, 'tasks', None)
            active = False
            if manager:
                with manager.lock:
                    active = any(t['status'] in ('queued', 'running', 'cancelling') for t in manager.items.values())
            if state['mutations'] > 1 or active:
                raise HTTPException(409, '仍有任务或数据操作未结束，请完成后再切换。')
            state['switching'] = True
        try:
            switch = activate_directory(cfg, req.get('target') or '')
            with lock:
                state['switch'] = switch
            return status()
        except (BackupError, OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            with lock:
                state['switching'] = False

    @app.post('/api/backups/cancel-switch')
    def cancel():
        with lock:
            if not state['switch']:
                raise HTTPException(409, '没有待生效的目录切换。')
            try:
                cancel_activation(state['switch'])
            except (BackupError, OSError) as exc:
                raise HTTPException(409, str(exc)) from exc
            state['switch'] = None
            return status()
