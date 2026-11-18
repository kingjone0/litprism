"""Fixed short/long-paper Web QA acceptance. Raw evidence stays in _selftest.*.

prepare is offline. live requires --live, a frozen manifest, and a read-only
isolated credential source. No retries, search, semantic model judge, or release
into the daily knowledge base. Operator assessment is separate from mechanics.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
SPEC = ROOT / 'tools/fixtures/g04-quality-cases.json'
ENDPOINT = 'https://api.commandcode.ai/provider/v1'
MODEL = 'deepseek/deepseek-v4.1-flash'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation():
    paths = [p for p in (ROOT / 'src').rglob('*') if p.suffix in ('.py', '.js', '.css')]
    paths += [Path(__file__).resolve(), SPEC, ROOT / 'tools/chat_mixed_acceptance.py']
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(paths)}


def isolated(path):
    path = path.resolve()
    if ROOT not in path.parents or not any(p.startswith('_selftest.') for p in path.parts):
        raise ValueError('Use a workspace _selftest.* root')
    return path


def config(home):
    from litprism.config import Config
    cfg = Config()
    cfg.app.data_dir = str(home / 'data')
    cfg.llm.timeout = 900
    cfg.context.window_tokens = 1_000_000
    cfg.context.output_reserve_tokens = 384_000
    cfg.context.overflow_retries = 0
    cfg.qa.evidence_anchor_trial = True
    return cfg


def case_scope(paper_id):
    # Focus may add other papers; this batch promises strict single-paper input.
    return {'paper_ids': [paper_id], 'mode': 'strict'}


def freeze_question(kb, cfg, real, question, paper_ids):
    from litprism.agents import QAAgent
    from answer_units_live_acceptance import material

    class Capture:
        name = real.name
        runtime_config = real.runtime_config

        def chat_json(self, messages, *, task=None):
            captured.append({'task': task, 'messages': copy.deepcopy(messages)})
            raise RuntimeError('Offline capture, no dispatch')

    captured = []
    result = QAAgent(kb, cfg, Capture()).ask(question, paper_ids=paper_ids, strict_scope=True)
    if [c['task'] for c in captured] != ['qa'] or not result.excerpts:
        raise ValueError('Unexpected auxiliary call or empty input')
    return {'question': question, 'paper_ids': paper_ids, 'sources': result.excerpts,
            'retrieval': result.retrieval, 'messages': captured[0]['messages'], 'provider_calls': 0,
            'material_sha256': hashlib.sha256(material(captured[0]['messages']).encode()).hexdigest()}


def prepare(home, sources, *, regression=False):
    from litprism.db import KnowledgeBase
    from litprism.providers import ProviderStore
    from litprism.llm import build_active_llm
    home = isolated(home)
    if (home / 'manifest.json').exists() or (home / 'data').exists():
        raise ValueError('Already prepared; never overwrite frozen evidence')
    spec = read(SPEC)
    cases = [c for c in spec['cases'] if not regression or c['id'] in ('L1', 'L3')]
    for alias, pdf in sources.items():
        if digest(pdf) != spec['papers'][alias]['sha256']:
            raise ValueError('PDF hash differs from the frozen paper version')
        doc = read(home / alias / 'document.json')
        if doc['pdf_sha256'] != digest(pdf) or doc['pages'] != spec['papers'][alias]['pages']:
            raise ValueError('Document/page binding changed')
    cfg = config(home)
    kb = KnowledgeBase(cfg)
    try:
        providers = ProviderStore(kb, cfg.data_path)
        provider = providers.create(name='G04 isolated service', base_url=ENDPOINT,
                                    api_key='g04-offline-placeholder', chat_model=MODEL)
        providers.set_models(provider.id, [{'model_id': MODEL, 'window_tokens': 1_000_000,
                                           'max_output_tokens': 384_000}])
        providers.set_active_model(provider.id, MODEL)
        real = build_active_llm(kb, cfg)
        papers = {}
        for alias, source in spec['papers'].items():
            doc = read(home / alias / 'document.json')
            pid = kb.add_paper(title=source['title'], url=source['url'],
                               verify_level='verified', material_level='fulltext')
            kb.add_chunks(pid, doc['chunks'])
            papers[alias] = pid
        frozen = {}
        for case in cases:
            folder = home / case['id']
            folder.mkdir()
            value = freeze_question(kb, real.runtime_config, real, case['question'], [papers[case['paper']]])
            expected_ids = {c['id'] for c in kb.get_chunks(papers[case['paper']])}
            if {c['chunk_id'] for c in value['sources']} != expected_ids:
                raise ValueError('Not all indexed current chunks fit; do not claim full delivery')
            write(folder / 'frozen-input.json', value)
            frozen[case['id']] = digest(folder / 'frozen-input.json')
        write(home / 'manifest.json', {'version': 1, 'spec_sha256': digest(SPEC),
              'implementation': implementation(), 'provider_id': provider.id, 'papers': papers,
              'endpoint': ENDPOINT, 'model': MODEL, 'planned_calls': len(cases),
              'case_ids': [c['id'] for c in cases],
              'frozen_inputs': frozen,
              'documents': {a: digest(home / a / 'document.json') for a in papers},
              'pdf_paths': {a: str(p.resolve()) for a, p in sources.items()},
              'policy': 'Fixed first attempts; strict scope; stop on 401/403; no search or retry',
              'budget': 'User authorized completion of fixed acceptance; automatic model output allocation',
              'reference_scope': ('L1/L3 repair regression, not a new independent quality sample'
                                  if regression else spec['scope']),
              'human_approval': False, 'semantic_passed': None})
        print(json.dumps({'prepared': len(frozen), 'calls': 0, 'chunks': {
            a: len(kb.get_chunks(p)) for a, p in papers.items()}}), flush=True)
    finally:
        kb.close()


def verify(home):
    m = read(home / 'manifest.json')
    if m['spec_sha256'] != digest(SPEC) or m['implementation'] != implementation():
        raise ValueError('Frozen specification or implementation changed')
    ids = m.get('case_ids')
    if (ids not in (['S1', 'S2', 'S3', 'L1', 'L2', 'L3'], ['L1', 'L3'])
            or (m['endpoint'], m['model'], m['planned_calls']) != (ENDPOINT, MODEL, len(ids))
            or list(m['frozen_inputs']) != ids):
        raise ValueError('Frozen dispatch scope changed')
    for case, expected in m['frozen_inputs'].items():
        if digest(home / case / 'frozen-input.json') != expected:
            raise ValueError('Frozen input changed')
    for alias, expected in m['documents'].items():
        if digest(home / alias / 'document.json') != expected:
            raise ValueError('Frozen document changed')
        if digest(Path(m['pdf_paths'][alias])) != read(SPEC)['papers'][alias]['sha256']:
            raise ValueError('Frozen PDF changed')
    return m


def live(home, credential_root):
    from fastapi.testclient import TestClient
    from litprism.db import KnowledgeBase
    from litprism.providers import ProviderStore
    from litprism.secrets import SecretStore
    from litprism.llm import OpenAICompatClient, safe_failure
    from litprism.web import server
    from answer_units_live_acceptance import validate_dispatch
    from chat_mixed_acceptance import inspect_result, wait
    home, credential_root = isolated(home), isolated(credential_root)
    m = verify(home)
    with (home / 'started.json').open('x', encoding='utf-8') as stream:
        json.dump({'time': time.time(), 'manifest_sha256': digest(home / 'manifest.json'),
                   'never_repeat': True}, stream)
    # Read the already-isolated provider database without KnowledgeBase/migrations.
    with sqlite3.connect((credential_root / 'litprism.db').as_uri() + '?mode=ro', uri=True) as con:
        row = next(r for r in con.execute('SELECT base_url,api_key_ref,chat_model FROM providers')
                   if r[0].rstrip('/') == ENDPOINT and r[2] == MODEL)
    key = SecretStore(credential_root).get(row[1])
    if not key:
        raise ValueError('Archived credential unavailable')
    cfg = config(home)
    kb = KnowledgeBase(cfg)
    providers = ProviderStore(kb, cfg.data_path)
    providers.update(m['provider_id'], api_key=key)
    del key
    original_kb, original_chat = server.KnowledgeBase, OpenAICompatClient.chat
    current, requests, deltas, rows = {}, [], [], []

    def capture(self, messages, **kwargs):
        validate_dispatch(kwargs.get('task'), messages, current['frozen'], len(requests))
        if not self.durable_single_attempt:
            raise ValueError('Non-durable dispatch blocked')
        record = {'task': kwargs['task'], 'messages': copy.deepcopy(messages), 'status': 'started'}
        requests.append(record)
        write(current['folder'] / 'requests.json', requests)
        callback = kwargs.get('on_delta')
        started = time.monotonic()

        def delta(kind, text):
            deltas.append({'kind': kind, 'text': text, 'seconds': round(time.monotonic()-started, 4)})
            if callback:
                callback(kind, text)

        if callback:
            kwargs['on_delta'] = delta
        try:
            text = original_chat(self, messages, **kwargs)
            (current['folder'] / 'raw-completion.txt').write_text(text, encoding='utf-8')
            record['status'] = 'succeeded'
            return text
        except Exception as exc:
            record.update(status='failed', failure=safe_failure(exc))
            raise
        finally:
            record.update(seconds=round(time.monotonic()-started, 3), usage=self.last_usage,
                          settings=self.last_request_settings)
            write(current['folder'] / 'requests.json', requests)
            write(current['folder'] / 'deltas.json', deltas)

    app = None
    try:
        server.KnowledgeBase = lambda _: kb
        OpenAICompatClient.chat = capture
        app = server.create_app(cfg)
        client = TestClient(app)
        cases = [c for c in read(SPEC)['cases'] if c['id'] in m['case_ids']]
        for case in cases:
            verify(home)
            requests.clear(); deltas.clear()
            folder = home / case['id']
            current.update(folder=folder, frozen=read(folder / 'frozen-input.json'))
            cid = kb.add_conversation('G04 '+case['id'], scope=case_scope(m['papers'][case['paper']]))
            print(json.dumps({'stage': 'start', 'id': case['id']}), flush=True)
            stamp = time.monotonic()
            response = client.post('/api/conversations/'+cid+'/chat-runs', json={
                'question': case['question'], 'request_id': 'g04-fixed-'+case['id'], 'web_search': False})
            response.raise_for_status()
            rid = response.json()['run_id']
            state = wait(client, rid, stamp)
            row = inspect_result(kb, client, cid, rid, case, state, time.monotonic()-stamp)
            saved = kb._con.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa' AND status='succeeded'", (rid,)).fetchone()
            write(folder / 'parsed.json', json.loads(saved[0])['value'] if saved else None)
            row['request_count'] = len(requests)
            row['stream_events'] = {kind: sum(d['kind'] == kind for d in deltas) for kind in ('content','reasoning')}
            write(folder / 'result.json', row)
            rows.append(row)
            write(home / 'results.json', {'rows': rows, 'semantic_passed': None, 'human_approval': False})
            print(json.dumps({'stage': 'done', 'id': case['id'], 'status': row['status'],
                              'run_status': state['status'], 'seconds': row['elapsed_seconds'],
                              'usage': row['usage']}), flush=True)
            failures = [r.get('failure') or {} for r in requests]
            if any(f.get('http_status') in (401, 403) for f in failures):
                write(home / 'blocked.json', {'reason': 'provider_authorization', 'after': case['id'],
                      'remaining': [c['id'] for c in cases if c['id'] not in {r['id'] for r in rows}],
                      'never_automatically_retry': True})
                break
    finally:
        server.KnowledgeBase, OpenAICompatClient.chat = original_kb, original_chat
        if app:
            app.state.tasks.pool.shutdown(wait=True)
        kb.close()
    return len(rows) == m['planned_calls'] and all(r['run_status'] == 'completed' for r in rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'live'])
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--fasttext', type=Path)
    parser.add_argument('--gpt3', type=Path)
    parser.add_argument('--credential-root', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--regression', action='store_true', help='Prepare only the fixed L1/L3 regression pair')
    args = parser.parse_args()
    os.environ['LITPRISM_HOME'] = str(isolated(args.home))
    if args.action == 'prepare':
        if not args.fasttext or not args.gpt3:
            parser.error('prepare needs both frozen PDFs and prepared documents')
        prepare(args.home.resolve(), {'fasttext': args.fasttext, 'gpt3': args.gpt3}, regression=args.regression)
    else:
        if not args.live or not args.credential_root:
            parser.error('live needs explicit --live and an isolated credential source')
        raise SystemExit(0 if live(args.home.resolve(), args.credential_root) else 1)
