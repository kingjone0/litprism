"""Durable single-paper analysis budgets and exact, run-local response reuse.

File locks survive neither process exit nor crash: a new process may recover a
run, but an in-flight request's cost/outcome remains unknown until explicitly
authorized for retry. Keys, headers and reasoning traces are never journaled.
"""
from __future__ import annotations

import copy
import json
import math
import uuid
from contextlib import contextmanager
from pathlib import Path

from .evidence import require_citable
from .knowledge import now
from .materials import MaterialChanged
from .semantics import payload_hash
from .usage import UsageTracker

# 阶段调用预算已取消：保留字段只为兼容旧记录与用量展示，0 表示不限制。
DEFAULT_BUDGETS = {'extract': 0, 'concepts': 0, 'semantic_review': 0, 'semantic_cross_review': 0}
PIPELINE_VERSION = 'analysis-journal-v2-materials'


class RunStopped(RuntimeError):
    stop_analysis_run = True


class RetryRequired(RunStopped):
    pass


class RunConflict(RunStopped):
    pass


class RunCallFailed(RunStopped):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def budgets(value=None, previous=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value)-DEFAULT_BUDGETS.keys():
        raise ValueError('阶段预算必须是已知阶段的对象。')
    result = dict(previous or DEFAULT_BUDGETS)
    for key, limit in value.items():
        if type(limit) is not int or not 0 <= limit <= 10000:
            raise ValueError('每阶段预算须为 0–10000 的整数（逻辑调用次数）。')
        if previous and limit < previous[key]:
            raise ValueError('恢复只允许保持或增加原预算，不允许降低。')
        result[key] = limit
    return result


def _analysis_hash(kb, pid):
    return payload_hash((kb.get_analysis(pid) or {}).get('payload') or {})


def binding(kb, cfg, client, pid, with_concepts, semantic_review=True):
    from .config import config_to_dict
    from .providers import apply_active_window
    paper = kb.get_paper(pid)
    if not paper or paper.get('deleted_at'):
        raise RunConflict('论文不存在或已回收，不能恢复。')
    try:
        require_citable(paper.get('verify_level'))
    except ValueError as exc:
        raise RunConflict('来源已不可引用，不能恢复。') from exc
    cfg = copy.deepcopy(getattr(client, 'runtime_config', cfg))
    apply_active_window(cfg, kb)
    chunks = kb.get_chunks(pid)
    source = {'paper': {k: paper.get(k) for k in ('id', 'title', 'authors', 'year', 'abstract', 'doi', 'url', 'current_material_id', 'material_level')},
              'chunks': [{k: c.get(k) for k in ('id', 'page', 'section', 'text')} for c in chunks]}
    settings = getattr(client, 'cfg', cfg.llm)
    # Persist hashes of complete endpoint/settings; never plaintext credentials.
    params = {k: getattr(settings, k, None) for k in ('provider', 'model', 'base_url', 'temperature', 'max_tokens', 'timeout', 'json_mode',
                                                   'review_max_tokens', 'cross_review_max_tokens', 'review_read_timeout', 'model_max_output_tokens',
                                                   'model_window_tokens', 'automatic_output')}
    params['client_type'] = type(client).__module__+'.'+type(client).__qualname__
    params['context'] = config_to_dict(cfg)['context']
    params['with_concepts'] = with_concepts
    params['semantic_review'] = bool(semantic_review)
    params['supports_semantic_review'] = bool(getattr(client, 'supports_semantic_review', False))
    params['capability_missing'] = getattr(client, 'capability_missing', None)
    from .llm import _review_connect_timeout, _review_output_cap, _review_read_timeout
    # 只记录输出/等待设置；复核次数不再有内置上限（成本约束来自客户端 remaining_calls）。
    params['review_limits'] = ['automatic' if getattr(settings, 'automatic_output', False) else _review_output_cap(),
                               _review_read_timeout(getattr(settings, 'review_read_timeout', 60)),
                               _review_connect_timeout()]
    # Source builds bind implementation and prompts; frozen builds require an
    # explicit protocol bump when changing the pipeline (packaging not yet tested).
    root = Path(__file__).parent
    files = sorted(root.rglob('*.py'))
    code = [(p.relative_to(root).as_posix(), p.read_bytes().hex()) for p in files]
    manifest = {'version': PIPELINE_VERSION, 'source_hash': payload_hash(source),
                'settings_hash': payload_hash(params), 'pipeline_hash': payload_hash(code),
                'model': getattr(client, 'name', 'unknown'), 'with_concepts': with_concepts,
                'semantic_review': bool(semantic_review),
                'capability_missing': params.get('capability_missing')}
    return manifest, cfg


@contextmanager
def _run_lock(kb, rid, *, prefix='ar_', directory='analysis-run-locks'):
    # rid comes from a DB row, but still validate before forming a filesystem path.
    if not rid.startswith(prefix) or len(rid) != 35 or any(c not in '0123456789abcdef' for c in rid[3:]):
        raise RunConflict('无效运行 ID。')
    folder = kb.db_path.resolve().parent/directory
    folder.mkdir(exist_ok=True)
    handle = (folder/(rid+'.lock')).open('a+b')
    acquired = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RunConflict('同一运行正在执行，不能重复启动或恢复。') from exc
        yield
    finally:
        if acquired:
            handle.seek(0)
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class AnalysisRuns:
    def __init__(self, kb):
        self.kb = kb

    def row(self, rid):
        with self.kb._tx_lock:
            row = self.kb._con.execute('SELECT * FROM analysis_runs WHERE id=?', (rid,)).fetchone()
        if not row:
            raise KeyError('分析运行不存在。')
        return dict(row)

    def calls(self, rid):
        with self.kb._tx_lock:
            return [dict(r) for r in self.kb._con.execute(
                'SELECT * FROM analysis_run_calls WHERE run_id=? ORDER BY step,attempt', (rid,))]

    def create(self, cfg, client, pid, limits=None, with_concepts=True, semantic_review=True):
        if type(with_concepts) is not bool:
            raise ValueError('术语开关必须为布尔值。')
        if type(semantic_review) is not bool:
            raise ValueError('机器复核开关必须为布尔值。')
        limits = budgets(limits)
        manifest, _ = binding(self.kb, cfg, client, pid, with_concepts, semantic_review)
        rid, timestamp = 'ar_'+uuid.uuid4().hex, now()
        with self.kb.tx() as con:
            con.execute('INSERT INTO analysis_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (rid, pid, _json(manifest), _analysis_hash(self.kb, pid), _json(limits),
                         'queued', None, None, timestamp, timestamp))
        return self.detail(rid)

    def update(self, rid, **values):
        allowed = {'status', 'stage', 'stop_reason', 'budgets', 'analysis_hash'}
        if set(values)-allowed:
            raise ValueError('Invalid run update')
        values['updated_at'] = now()
        with self.kb.tx() as con:
            con.execute('UPDATE analysis_runs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                        (*values.values(), rid))

    def usage(self, rid, only_semantic=False):
        tracker = UsageTracker()
        for call in self.calls(rid):
            if only_semantic and not call['task'].startswith('semantic_'):
                continue
            tracker.add(json.loads(call['usage']) if call['usage'] else None, task=call['task'],
                        failed=call['status'] != 'succeeded')
        return tracker.to_dict()

    def detail(self, rid):
        row = self.row(rid)
        manifest, limits = json.loads(row['binding']), json.loads(row['budgets'])
        usage = self.usage(rid)
        spent = {k: usage['per_task'].get(k, {}).get('calls', 0) for k in DEFAULT_BUDGETS}
        calls = self.calls(rid)
        latest = {c['step']: c for c in calls}
        from .candidate_snapshots import by_run
        candidate = by_run(self.kb, row['paper_id'], rid)
        return {k: row[k] for k in ('id', 'paper_id', 'status', 'stage', 'stop_reason', 'created_at', 'updated_at')} | {
            'model': manifest['model'], 'with_concepts': manifest['with_concepts'],
            'semantic_review': manifest.get('semantic_review', True),
            'capability_missing': manifest.get('capability_missing'),
            'candidate_snapshot_id': (candidate or {}).get('id'),
            'candidate_count': len(((candidate or {}).get('payload') or {}).get('_candidates') or []),
            'budgets': limits, 'spent': spent, 'usage': usage,
            'retry_required': any(c['status'] != 'succeeded' for c in latest.values()),
            'steps': [{'step': c['step'], 'attempt': c['attempt'], 'task': c['task'], 'status': c['status'],
                       'error_type': c['error_type'], 'request_settings': json.loads(c['request_settings'] or '{}')} for c in calls]}

    def list(self, pid):
        with self.kb._tx_lock:
            ids = [r[0] for r in self.kb._con.execute(
                'SELECT id FROM analysis_runs WHERE paper_id=? ORDER BY rowid DESC LIMIT 30', (pid,))]
        return [self.detail(rid) for rid in ids]

    def execute(self, rid, cfg, client, *, limits=None, retry_failed=False):
        from .extract import analyze_paper
        from .tasks import TaskCancelled
        if type(retry_failed) is not bool:
            raise ValueError('失败重试选项必须为布尔值。')
        with _run_lock(self.kb, rid):
            row = self.row(rid)
            original = json.loads(row['binding'])
            try:
                current, cfg = binding(self.kb, cfg, client, row['paper_id'], original['with_concepts'],
                                   original.get('semantic_review', True))
            except (RunConflict, MaterialChanged):
                self.update(rid, status='stale', stop_reason='来源不存在、已回收或不可引用；需重新核对来源。')
                raise
            if current != original or _analysis_hash(self.kb, row['paper_id']) != row['analysis_hash']:
                self.update(rid, status='stale', stop_reason='原文、模型/协议/参数、实现或分析版本已变化；需新建运行。')
                raise RunConflict('原文、模型/协议/参数、实现或当前分析版本已变化；请新建运行，不能复用旧结果。')
            new_limits = budgets(limits, json.loads(row['budgets']))
            self.update(rid, status='running', stop_reason=None, budgets=_json(new_limits))
            journal = JournalClient(self, rid, client, new_limits, retry_failed)
            def finalize(payload):
                # Called inside the same transaction that publishes the analysis.
                fresh, _ = binding(self.kb, cfg, client, row['paper_id'], original['with_concepts'],
                               original.get('semantic_review', True))
                if fresh != original or _analysis_hash(self.kb, row['paper_id']) != row['analysis_hash']:
                    raise RunConflict('生成期间来源或分析版本已变化，未发布本次结果。')
                payload['_coverage']['usage'] = self.usage(rid)
                payload['_semantic_review']['usage'] = self.usage(rid, only_semantic=True)
                payload['_analysis_run_id'] = rid
                self.update(rid, analysis_hash=payload_hash(payload))
            try:
                payload = analyze_paper(self.kb, cfg, journal, row['paper_id'], force=True,
                                        with_concepts=original['with_concepts'], finalize=finalize,
                                        semantic_review=original.get('semantic_review', True))
                partial = (not payload['_coverage']['complete'] or not payload['_semantic_review'].get('passed')
                           or bool(payload.get('_concepts_error')))
                self.update(rid, status='partial' if partial else 'completed', stop_reason=None)
            except RetryRequired:
                self.update(rid, status='needs_retry', stop_reason='上次调用失败或中断，费用/结果可能未知；不会自动重复调用。')
            except TaskCancelled:
                self.update(rid, status='cancelled', stop_reason='已取消；已完成请求保留，恢复时精确复用。')
                raise
            except (RunConflict, MaterialChanged):
                self.update(rid, status='stale', stop_reason='运行期间来源、分析版本或恢复请求不一致；未发布结果。')
            except Exception as exc:
                self.update(rid, status='failed', stop_reason='运行停止：'+type(exc).__name__+'；原解读保留。')
            return self.detail(rid)


class JournalClient:
    def __init__(self, store, rid, client, limits, retry_failed):
        self.store, self.rid, self.client = store, rid, client
        # This client belongs to this task. Every physical send needs a journal
        # entry; automatic transport retries/fallbacks cannot bypass the budget.
        self.client.durable_single_attempt = True
        self.limits, self.retry_failed = limits, retry_failed
        self.name = client.name
        self.supports_semantic_review = getattr(client, 'supports_semantic_review', False)
        self.step = 0
        self.last_usage = None
        self.last_call_replayed = self.last_call_not_sent = False

    def chat_json(self, messages, *, task=None):
        from .tasks import checkpoint
        checkpoint()
        self.last_call_replayed = self.last_call_not_sent = False
        if task not in self.limits:
            self.last_call_not_sent = True
            raise RunConflict('未定义预算阶段，拒绝调用。')
        self.step += 1
        digest = payload_hash({'task': task, 'messages': messages})
        calls = self.store.calls(self.rid)
        history = [c for c in calls if c['step'] == self.step]
        attempt = history[-1]['attempt']+1 if history else 1
        if history:
            old = history[-1]
            same = old['request_hash'] == digest and old['task'] == task
            # A deliberately retried failure may produce a different accepted
            # extraction. Its downstream requests must be regenerated, never
            # fed cached responses belonging to the old branch.
            changed_parent = any(c['step'] < self.step and c['attempt'] > 1 and c['status'] == 'succeeded' for c in calls)
            if not same and not changed_parent:
                self.last_call_not_sent = True
                raise RunConflict('恢复请求与原记录不一致，未调用模型。')
            if same and old['status'] == 'succeeded':
                self.last_call_replayed = True
                self.last_usage = None
                return json.loads(old['response'])
            if same and not self.retry_failed:
                self.last_call_not_sent = True
                raise RetryRequired()
        # 预算不再是闸门：只统计用量，不因阶段次数停止（模型窗口已知后由内容决定能跑多少）。
        timestamp = now()
        with self.store.kb.tx() as con:
            con.execute('INSERT INTO analysis_run_calls (run_id,step,attempt,task,request_hash,status,response,usage,error_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (self.rid, self.step, attempt, task, digest, 'started', None, None, None, timestamp, timestamp))
        self.store.update(self.rid, stage=task)
        try:
            self.client.last_usage = None
            response = self.client.chat_json(messages, task=task)
            serialized = _json(response)
        except Exception as exc:
            from .llm import safe_failure
            self.last_failure = safe_failure(exc)
            self.last_usage = getattr(self.client, 'last_usage', None)
            self._finish(attempt, 'failed', None, type(exc).__name__)
            from .llm import OutputLimitError
            from .tasks import TaskCancelled
            if isinstance(exc, TaskCancelled):
                raise
            if isinstance(exc, OutputLimitError):
                raise OutputLimitError('该阶段输出被截断，结果不完整。') from exc
            raise RunCallFailed('模型调用失败；已记录异常类别，不自动重复调用。') from exc
        self.last_usage = getattr(self.client, 'last_usage', None)
        self._finish(attempt, 'succeeded', serialized, None)
        return response

    def _finish(self, attempt, status, response, error):
        # The provider's usage object is numeric metadata, never full raw output.
        raw = self.last_usage
        allowed = {'prompt_tokens', 'input_tokens', 'completion_tokens', 'output_tokens', 'total_tokens',
                   'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'cache_read_input_tokens',
                   'cache_creation_input_tokens', 'prompt_tokens_details', 'completion_tokens_details'}
        def numeric(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        details = {'cached_tokens', 'cache_hit_tokens', 'reasoning_tokens', 'accepted_prediction_tokens',
                   'rejected_prediction_tokens', 'audio_tokens'}
        usage = None
        settings = getattr(self.client, 'last_request_settings', {}) or {}
        safe_settings = {k: v for k, v in settings.items()
                         if k in ('max_tokens', 'estimated_input_tokens', 'input_safety_tokens', 'model_window_tokens',
                                  'model_max_output_tokens', 'attempts') and numeric(v)}
        if settings.get('output_mode') in ('automatic', 'legacy_unknown_capability'):
            safe_settings['output_mode'] = settings['output_mode']
        if status == 'failed' and getattr(self, 'last_failure', None):
            safe_settings['failure'] = self.last_failure
        if isinstance(raw, dict):
            usage = {}
            for key, value in raw.items():
                if key not in allowed:
                    continue
                if key.endswith('_details') and isinstance(value, dict):
                    usage[key] = {k: v for k, v in value.items() if k in details and numeric(v)}
                elif numeric(value):
                    usage[key] = value
        with self.store.kb.tx() as con:
            con.execute('UPDATE analysis_run_calls SET status=?,response=?,usage=?,error_type=?,updated_at=?,request_settings=? '
                        'WHERE run_id=? AND step=? AND attempt=?',
                        (status, response, _json(usage) if usage is not None else None, error, now(), _json(safe_settings),
                         self.rid, self.step, attempt))
