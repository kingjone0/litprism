"""Persistent QA endpoints; read/reopen never initiates a model call."""
import json
import threading

from fastapi import HTTPException

from ..analysis_runs import RunConflict, _run_lock
from ..chat_runs import ChatRuns, request_input


def register_chat_runs(app, kb, cfg, task_llm, process, messages):
    store = ChatRuns(kb)
    jobs, lock = {}, threading.RLock()
    app.state.chat_runs = store

    def row(rid):
        try:
            return store.row(rid)
        except KeyError as exc:
            raise HTTPException(404, '对话请求不存在。') from exc

    def active(rid):
        job = app.state.tasks.items.get(jobs.get(rid)) or {}
        return job.get('status') in ('queued', 'running', 'cancelling')

    def read(rid):
        saved = row(rid)
        interrupted = False
        with lock:
            # Capture liveness before the persisted snapshot. If completion races
            # this read, keep polling once more rather than pair old state with
            # a newer "inactive" flag and prematurely report the old outcome.
            was_active = active(rid)
            if saved['status'] in ('queued', 'running') and not was_active:
                try:
                    with _run_lock(kb, rid, prefix='cr_', directory='chat-run-locks'):
                        # No automatic send after restart, even if never dispatched.
                        if row(rid)['status'] in ('queued', 'running'):
                            interrupted = True
                except RunConflict:
                    pass
            result = store.detail(rid) | {'task_id': jobs.get(rid), 'active': was_active}
            if interrupted:
                result.update(status='interrupted', stop_reason='执行已中断；已保存响应可复用，未完成调用的结果/费用可能未知。')
            return result

    def schedule(rid, retry=False, client=None):
        with lock:
            saved = row(rid)
            if saved['status'] == 'completed' or active(rid):
                return {'run_id': rid, 'task_id': jobs.get(rid)}
            client = client or task_llm()
            task = app.state.tasks.submit(lambda: store.execute(rid, cfg, client, process, retry=retry))
            jobs[rid] = task['id']
            return {'run_id': rid, 'task_id': task['id']}

    @app.get('/api/conversations/{cid}/chat-runs')
    def listing(cid: str):
        if not kb.resolve_conversation(cid):
            raise HTTPException(404, '会话不存在。')
        with kb._tx_lock:
            ids = [r[0] for r in kb._con.execute('SELECT id FROM chat_runs WHERE conversation_id=? ORDER BY rowid DESC LIMIT 30', (cid,))]
        return {'items': [read(rid) for rid in ids]}

    @app.post('/api/conversations/{cid}/chat-runs')
    def create(cid: str, req: dict):
        try:
            incoming = request_input(req, cfg)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with lock:
            # Repeated receipt lookup must work even if credentials were removed.
            with kb._tx_lock:
                old = kb._con.execute('SELECT * FROM chat_runs WHERE conversation_id=? AND request_id=?', (cid, req.get('request_id') if isinstance(req.get('request_id'), str) else '')).fetchone()
            if old:
                previous = json.loads(old['request'])['input']
                if previous != incoming:
                    raise HTTPException(409, '请求标识已用于不同问题。')
                return {'run_id': old['id'], 'task_id': jobs.get(old['id'])}
            try:
                client = task_llm()
                rid, _ = store.create(cid, req, cfg, client)
                return schedule(rid, client=client)
            except (ValueError, RunConflict) as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get('/api/chat-runs/{rid}')
    def detail(rid: str):
        return read(rid)

    @app.get('/api/chat-runs/{rid}/result')
    def result(rid: str):
        saved = row(rid)
        if saved['status'] != 'completed':
            raise HTTPException(409, '请求尚未发布结果，请查看状态后决定是否续验。')
        result = json.loads(saved['result'])
        current = messages(saved['conversation_id'])
        message = next((m for m in current['items'] if m['ref'] == result['assistant']['ref']), None)
        if message is None:
            raise HTTPException(409, '原回复已不存在，不能替换为其他回复。')
        result['assistant'] = message
        result['status'] = message['status']
        # Client renders current checked links; do not return stale cached source data.
        result['sources'] = [] if message['status'] != 'answered' else result['sources']
        return result

    @app.post('/api/chat-runs/{rid}/resume')
    def resume(rid: str, req: dict):
        if type(req.get('retry_failed', False)) is not bool:
            raise HTTPException(400, '重试选项必须为布尔值。')
        current = read(rid)
        if current['status'] == 'stale':
            raise HTTPException(409, '请求依据已变化，请按当前材料重新提问。')
        if current['retry_required'] and not req.get('retry_failed'):
            raise HTTPException(409, '未完成调用的结果和费用可能未知；请明确允许重新请求。')
        return schedule(rid, req.get('retry_failed', False))

    @app.post('/api/chat-runs/{rid}/stop')
    def stop(rid: str):
        with lock:
            current = read(rid)
            if current['active']:
                raise HTTPException(409, '请求仍在执行，请先取消后续处理。')
            if current['status'] == 'completed':
                raise HTTPException(409, '结果已保存，无需结束请求。')
            try:
                with _run_lock(kb, rid, prefix='cr_', directory='chat-run-locks'):
                    store.update(rid, 'cancelled', '已结束此请求；原始响应和未知用量保留，可另行提问。')
            except RunConflict as exc:
                raise HTTPException(409, str(exc)) from exc
        return read(rid)
