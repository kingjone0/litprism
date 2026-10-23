"""Durable Web QA attempts. Unknown outcomes require explicit retry, never guessing free."""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from pathlib import Path

from .analysis_runs import RunConflict, RetryRequired, _run_lock
from .knowledge import now
from .semantics import payload_hash
from .usage import UsageTracker


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def runtime(cfg, client):
    result = copy.deepcopy(getattr(client, 'runtime_config', cfg))
    result.context.overflow_retries = 0
    return result


def request_input(req, cfg):
    question = req.get('question')
    if not isinstance(question, str) or not question.strip():
        raise ValueError('问题不能为空。')
    top_k = req.get('top_k', cfg.retrieval.top_k)
    if type(top_k) is not int or not 1 <= top_k <= 50 or type(req.get('web_search', False)) is not bool:
        raise ValueError('召回数必须为1–50的整数，网页搜索开关须为布尔值。')
    return {'question': question.strip(), 'top_k': top_k, 'web_search': req.get('web_search', False)}


def binding(kb, cfg, client, conv_id, scope, user_seq):
    from .config import config_to_dict
    from .scope import paper_scope
    conv = kb.resolve_conversation(conv_id)
    ids = paper_scope(kb, scope) if scope.get('mode') == 'strict' else None
    papers = [dict(r) for r in kb._con.execute('SELECT * FROM papers ORDER BY id')
              if ids is None or r['id'] in ids]
    sources = [{k: p.get(k) for k in ('id', 'title', 'year', 'verify_level', 'deleted_at', 'current_material_id', 'material_level')}
               | {'chunks': kb.get_chunks(p['id'])} for p in papers]
    from .provenance import availability
    knowledge = [dict(r) | {'dependency_status': availability(kb,r['id']), 'chunks': [dict(c) for c in kb._con.execute(
        'SELECT id,text FROM evidence_chunks WHERE source_id=? ORDER BY ordinal', (r['id'],))]}
        for r in kb._con.execute("SELECT id,content_hash,status FROM evidence_sources WHERE status='manual_confirmed' ORDER BY id")]
    history = [m for m in kb.get_messages(conv_id) if m['seq'] < user_seq]
    # New turns, scope edits, human decisions and compaction all invalidate a resume.
    state = {'scope': conv['scope'], 'sources': sources, 'knowledge': knowledge, 'history': history,
             'tail': max((m['seq'] for m in kb.get_messages(conv_id)), default=0),
             'reviews': [dict(r) for r in kb._con.execute('SELECT * FROM semantic_reviews ORDER BY id')],
             'memory': [dict(r) for r in kb._con.execute('SELECT * FROM memories ORDER BY id')],
             'blocks': [dict(r) for r in kb._con.execute('SELECT * FROM compaction_blocks WHERE conversation_id=? ORDER BY id', (conv_id,))]}
    settings = getattr(client, 'cfg', cfg.llm)
    params = {k: getattr(settings, k, None) for k in ('provider', 'model', 'base_url', 'temperature', 'max_tokens',
              'timeout', 'json_mode', 'model_window_tokens', 'model_max_output_tokens', 'automatic_output')}
    params.update(context=config_to_dict(cfg)['context'], retrieval=config_to_dict(cfg)['retrieval'],
                  client=type(client).__module__+'.'+type(client).__qualname__, name=client.name)
    root = Path(__file__).parent
    code = [(p.relative_to(root).as_posix(), p.read_bytes().hex()) for p in sorted(root.rglob('*.py'))]
    return {'state': payload_hash(state), 'settings': payload_hash(params), 'code': payload_hash(code), 'model': client.name}


class ChatRuns:
    def __init__(self, kb):
        self.kb = kb

    def row(self, rid):
        with self.kb._tx_lock:
            r = self.kb._con.execute('SELECT * FROM chat_runs WHERE id=?', (rid,)).fetchone()
            if not r:
                raise KeyError('对话请求不存在。')
            return dict(r)

    def calls(self, rid):
        with self.kb._tx_lock:
            return [dict(r) for r in self.kb._con.execute('SELECT * FROM chat_run_calls WHERE run_id=? ORDER BY step,attempt', (rid,))]

    def update(self, rid, status, reason=None):
        with self.kb.tx() as con:
            con.execute('UPDATE chat_runs SET status=?,stop_reason=?,updated_at=? WHERE id=?', (status, reason, now(), rid))

    def usage(self, rid):
        usage = UsageTracker()
        for c in self.calls(rid):
            if c['task'] != 'web_search':
                usage.add(json.loads(c['usage']) if c['usage'] else None, task=c['task'], failed=c['status'] != 'succeeded')
        return usage.to_dict()

    def create(self, conv_id, req, cfg, client):
        from .scope import paper_scope, chat_strict
        key = req.get('request_id')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
            raise ValueError('请求标识无效，请重新打开会话后发送。')
        request = request_input(req, cfg)
        with self.kb.tx() as con:
            old = con.execute('SELECT * FROM chat_runs WHERE conversation_id=? AND request_id=?', (conv_id, key)).fetchone()
            if old:
                if json.loads(old['request'])['input'] != request:
                    raise RunConflict('同一请求标识不能用于不同问题。')
                return old['id'], False
            conv = self.kb.resolve_conversation(conv_id)
            if not conv:
                raise ValueError('会话不存在。')
            if con.execute("SELECT 1 FROM chat_runs WHERE conversation_id=? AND status IN ('queued','running')", (conv_id,)).fetchone():
                raise RunConflict('该会话已有未完成请求，请先查看运行记录。')
            scope = json.loads(conv['scope'] or '{}') if isinstance(conv['scope'], str) else conv['scope'] or {}
            ids = paper_scope(self.kb, scope)
            strict = chat_strict(scope, request['question']) or ids == []
            rid = 'cr_'+uuid.uuid4().hex
            user = self.kb.append_message(conv_id, 'user', request['question'], meta={
                'scope_snapshot': {'scope': scope, 'paper_ids': ids, 'strict': strict}, 'chat_run_id': rid})
            cfg = runtime(cfg, client)
            bound = binding(self.kb, cfg, client, conv_id, scope, user['seq'])
            con.execute('INSERT INTO chat_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
                rid, conv_id, key, dump({'input': request, 'scope': scope}), dump(bound), dump(user),
                'queued', None, None, now(), now()))
        return rid, True

    def detail(self, rid):
        row = self.row(rid)
        calls = self.calls(rid)
        latest = {c['step']: c for c in calls}
        return {k: row[k] for k in ('id', 'conversation_id', 'status', 'stop_reason', 'created_at', 'updated_at')} | {
            'question': json.loads(row['request'])['input']['question'], 'model': json.loads(row['binding'])['model'],
            'usage': self.usage(rid), 'retry_required': any(c['status'] != 'succeeded' for c in latest.values()),
            'calls': [{k: c[k] for k in ('step', 'attempt', 'task', 'status', 'error_type')} |
                      {'settings': json.loads(c['request_settings'] or '{}')} for c in calls]}

    def execute(self, rid, cfg, client, process, *, retry=False):
        from .tasks import TaskCancelled, checkpoint
        with _run_lock(self.kb, rid, prefix='cr_', directory='chat-run-locks'):
            row = self.row(rid)
            if row['status'] == 'completed':
                return self.detail(rid)
            request, user = json.loads(row['request']), json.loads(row['user_message'])
            cfg = runtime(cfg, client)
            def validate():
                with self.kb._tx_lock:
                    current = binding(self.kb, cfg, client, row['conversation_id'], request['scope'], user['seq'])
                if current != json.loads(row['binding']):
                    raise RunConflict('来源、范围、历史、模型或实现已变化；请重新提问。')
            try:
                validate()
                latest = {c['step']: c for c in self.calls(rid)}
                if not retry and any(c['status'] != 'succeeded' for c in latest.values()):
                    raise RetryRequired('先前请求失败或中断，结果和费用可能未知。请明确选择重新请求。')
                self.update(rid, 'running')
                journal = ChatJournal(self, rid, client, cfg, retry)
                journal.validate = validate
                def validate_publish():
                    if journal.conflict:
                        raise RunConflict(journal.conflict)
                    validate()
                execution = {'client': journal, 'user': user, 'rid': rid, 'validate': validate_publish,
                             'publish': lambda result: self.publish(rid, result), 'usage': lambda: self.usage(rid)}
                process(row['conversation_id'], request['input'], execution=execution)
            except RetryRequired:
                self.update(rid, 'needs_retry', '先前调用失败或中断，结果/费用可能未知；需明确允许重新请求。')
            except RunConflict as exc:
                self.update(rid, 'stale', str(exc))
            except Exception as exc:
                if self.row(rid)['status'] == 'completed':
                    return self.detail(rid)
                try:
                    checkpoint()
                except TaskCancelled:
                    exc = TaskCancelled()
                self.update(rid, 'cancelled' if isinstance(exc, TaskCancelled) else 'failed',
                            '已停止，保存的响应可复用；未完成调用结果/费用可能未知。')
            return self.detail(rid)

    def publish(self, rid, result):
        # Caller holds the message/review transaction: never publish a reply twice.
        self.kb._con.execute("UPDATE chat_runs SET status='completed',result=?,stop_reason=NULL,updated_at=? WHERE id=?",
                             (dump(result), now(), rid))


class ChatJournal:
    def __init__(self, store, rid, client, cfg, retry):
        self.store, self.rid, self.client, self.runtime_config = store, rid, client, cfg
        self.name, self.retry, self.step = client.name, retry, 0
        client.durable_single_attempt = True
        self.last_usage = None
        self.last_reasoning = ''
        self.last_request_settings = {}
        self.last_call_replayed = self.last_call_not_sent = False
        self.conflict = None

    def chat_json(self, messages, *, task=None):
        return self.invoke(task, messages, lambda: self.client.chat_json(messages, task=task))

    def invoke(self, task, request, fn):
        from .tasks import checkpoint
        from .llm import safe_failure
        self.last_call_replayed = False
        self.last_call_not_sent = True
        checkpoint()
        if self.conflict:
            self.last_call_not_sent = True
            raise RunConflict(self.conflict)
        if hasattr(self, 'validate'):
            self.validate()
        self.step += 1
        self.last_call_replayed = self.last_call_not_sent = False
        self.last_usage, self.last_reasoning, self.last_request_settings = None, '', {}
        digest = payload_hash({'task': task, 'request': request})
        history = [c for c in self.store.calls(self.rid) if c['step'] == self.step]
        attempt = history[-1]['attempt']+1 if history else 1
        if history:
            old = history[-1]
            if old['request_hash'] != digest:
                self.last_call_not_sent = True
                self.conflict = '恢复请求与已存请求不一致；未发起调用，请重新提问。'
                raise RunConflict(self.conflict)
            if old['status'] == 'succeeded':
                self.last_call_replayed = True
                self.last_request_settings = json.loads(old['request_settings'] or '{}')
                receipt = json.loads(old['response'])
                self.last_reasoning = receipt.get('reasoning', '')
                return receipt['value']
            if not self.retry:
                self.last_call_not_sent = True
                raise RetryRequired()
        with self.store.kb.tx() as con:
            con.execute('INSERT INTO chat_run_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                self.rid, self.step, attempt, task, digest, dump(request), 'started', None, None, None, None, now(), now()))
        status, response, error = 'succeeded', None, None
        try:
            self.client.last_usage = None
            self.client.last_request_settings = {}
            self.client.last_reasoning = ''
            response = fn()
            if task == 'qa' and (not isinstance(response, dict) or not isinstance(response.get('answer'), str)
                                or not response['answer'].strip()
                                or ('insufficient_evidence' in response and type(response['insufficient_evidence']) is not bool)):
                raise ValueError('响应格式不完整。')
            return response
        except Exception as exc:
            status, error = 'failed', type(exc).__name__
            self.last_request_settings['failure'] = safe_failure(exc)
            raise
        finally:
            # Numeric metadata only; never serialize arbitrary provider fields/errors.
            def numeric(v):
                return type(v) in (int, float) and math.isfinite(v) and v >= 0
            raw = getattr(self.client, 'last_usage', None) if task != 'web_search' else None
            keys = ('prompt_tokens', 'input_tokens', 'completion_tokens', 'output_tokens', 'total_tokens',
                    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
            self.last_usage = {k:v for k,v in raw.items() if k in keys and numeric(v)} if isinstance(raw, dict) else None
            if isinstance(raw, dict):
                for name in ('prompt_tokens_details', 'completion_tokens_details'):
                    value = raw.get(name)
                    if isinstance(value, dict):
                        self.last_usage[name] = {k:v for k,v in value.items() if k in (
                            'cached_tokens','cache_hit_tokens','reasoning_tokens','accepted_prediction_tokens',
                            'rejected_prediction_tokens','audio_tokens') and numeric(v)}
            settings = getattr(self.client, 'last_request_settings', {}) or {}
            self.last_request_settings.update({k:v for k,v in settings.items() if k in (
                'max_tokens','estimated_input_tokens','model_window_tokens','model_max_output_tokens','attempts','timeout') and numeric(v)})
            if settings.get('output_mode') in ('automatic', 'legacy_unknown_capability'):
                self.last_request_settings['output_mode'] = settings['output_mode']
            self.last_reasoning = getattr(self.client, 'last_reasoning', '') if status == 'succeeded' else ''
            with self.store.kb.tx() as con:
                con.execute('UPDATE chat_run_calls SET status=?,response=?,usage=?,request_settings=?,error_type=?,updated_at=? '
                            'WHERE run_id=? AND step=? AND attempt=?', (
                    status, dump({'value':response,'reasoning':self.last_reasoning}) if status == 'succeeded' else None,
                    dump(self.last_usage) if self.last_usage is not None else None, dump(self.last_request_settings), error,
                    now(), self.rid, self.step, attempt))
