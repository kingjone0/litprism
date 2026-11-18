"""Freeze one short-paper N01 reading; live dispatch is explicit and durable.

This evaluates extraction candidates, not automatic semantic approval. Native
bounded repairs remain visible. No concepts, search, semantic judge, manual
token/stage limits, daily database, or automatic retry of an unknown request.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from g04_quality_acceptance import (  # noqa: E402
    ENDPOINT, MODEL, config, digest, implementation as base_implementation, isolated, read, write,
)

FIXTURE = ROOT / 'tools/fixtures/short-reading-quality.json'


def implementation():
    return base_implementation() | {
        str(Path(__file__).resolve().relative_to(ROOT)): digest(Path(__file__).resolve()),
        str(FIXTURE.relative_to(ROOT)): digest(FIXTURE)}


def planned_reading(kb, cfg, model, pid):
    from litprism.analysis_runs import binding
    from litprism.context.meter import Meter
    from litprism.gates import MAX_REPAIR_ROUNDS, repair_text
    from litprism.plan import build_plan
    from litprism.prompts import EXTRACT_SYSTEM, CONCEPTS_SYSTEM
    from litprism.reading import excerpt_context, build_messages

    bound, runtime = binding(kb, cfg, model, pid, False, False)
    paper, chunks = kb.get_paper(pid), kb.get_chunks(pid)
    system = EXTRACT_SYSTEM + '\n每个非空字段必须在 field_evidence 提供原文标记列表；无依据留空。'
    system += '\n每个独立句子就地附其直接依据，不把多个分号连接的结论统一在段末附一串引用；每句最多 3 个直接来源，复杂内容分成多句。'
    concepts = CONCEPTS_SYSTEM + '\n每条 definition 必须含本批原文引用 [P# p.N]；无页码不写页码，无依据不生成。'
    meter = Meter(runtime)
    plan = build_plan(paper, chunks, meter, available_tokens=runtime.context.available_tokens,
                      max_tokens=min(runtime.llm.max_tokens, runtime.context.output_reserve_tokens),
                      overhead_tokens=meter.estimate_text(max([system, concepts], key=len)) + 200)
    sources = [dict(c, tag=f'P{i}', paper_title=paper['title'], document_tag='D1')
               for i, c in enumerate(chunks, 1)]
    prefix = excerpt_context(sources)
    if plan.mode != 'whole' or not plan.directives:
        raise ValueError('Frozen short paper must fit the current whole-prefix path')
    if any(sum(meter.estimate_message(m) for m in build_messages(system, prefix, repair_text(d, 0)))
           > runtime.context.available_tokens for d in plan.directives):
        raise ValueError('Actual labelled request/repair does not fit')
    return {'plan': asdict(plan), 'bound': bound, 'sources': sources,
            'directives': [{'id': d.id, 'scope': d.scope, 'expected': list(d.expected),
                            'quota': d.quota, 'text': d.text} for d in plan.directives],
            'planned_extract_requests': len(plan.directives),
            'native_max_requests': len(plan.directives) * (MAX_REPAIR_ROUNDS + 1),
            'available_tokens': runtime.context.available_tokens}, prefix


def prepare(home, pdf):
    from litprism.db import KnowledgeBase
    from litprism.ingest import load_document, chunk_document
    from litprism.llm import build_active_llm
    from litprism.providers import ProviderStore

    home = isolated(home)
    if home.exists():
        raise ValueError('Use a new output directory; never overwrite evidence')
    fixture = read(FIXTURE)
    if digest(pdf) != fixture['paper']['pdf_sha256']:
        raise ValueError('PDF differs from frozen fastText v3')
    doc = load_document(pdf)
    if doc.page_count != fixture['paper']['physical_pages']:
        raise ValueError('Physical page count differs')
    home.mkdir(parents=True)
    cfg = config(home)
    chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
    (home / 'source.pdf').write_bytes(pdf.read_bytes())
    write(home / 'document.json', {'pages': doc.page_count, 'warnings': doc.warnings,
          'page_text': [{'page': p.number, 'text': p.text} for p in doc.pages], 'chunks': chunks})
    kb = KnowledgeBase(cfg)
    try:
        providers = ProviderStore(kb, cfg.data_path)
        provider = providers.create(name='N01 short isolated acceptance', base_url=ENDPOINT,
                                    chat_model=MODEL, api_key='offline-prepare-placeholder')
        providers.set_models(provider.id, [{'model_id': MODEL, 'window_tokens': 1_000_000,
                                           'max_output_tokens': 384_000}])
        providers.set_active_model(provider.id, MODEL)
        pid = kb.add_paper(title=fixture['paper']['title'], url=fixture['paper']['url'],
                           verify_level='verified', material_level='fulltext')
        kb.add_chunks(pid, chunks, level='fulltext')
        frozen, prefix = planned_reading(kb, cfg, build_active_llm(kb, cfg), pid)
        write(home / 'frozen-plan.json', frozen)
        (home / 'shared-prefix.txt').write_text(prefix, encoding='utf-8')
        write(home / 'manifest.json', {'version': 1, 'fixture_sha256': digest(FIXTURE),
              'implementation': implementation(), 'pdf_sha256': digest(home / 'source.pdf'),
              'document_sha256': digest(home / 'document.json'),
              'plan_sha256': digest(home / 'frozen-plan.json'),
              'prefix_sha256': digest(home / 'shared-prefix.txt'),
              'provider_id': provider.id, 'paper_id': pid,
              'endpoint': ENDPOINT, 'model': MODEL,
              'with_concepts': False, 'semantic_review': False,
              'planned_extract_requests': frozen['planned_extract_requests'],
              'native_max_requests': frozen['native_max_requests'],
              'budget': 'User authorized fixed acceptance to completion; normal automatic output allocation',
              'scope': fixture['scope'], 'human_approval': False, 'semantic_passed': None})
        print({'prepared': True, 'provider_calls': 0, 'current_chunks': len(chunks),
               'planned_extract_requests': frozen['planned_extract_requests'],
               'native_max_requests': frozen['native_max_requests']}, flush=True)
    finally:
        kb.close()


def verify(home):
    from litprism.gates import MAX_REPAIR_ROUNDS
    m = read(home / 'manifest.json')
    if (m['fixture_sha256'] != digest(FIXTURE) or m['implementation'] != implementation()
            or m['endpoint'] != ENDPOINT or m['model'] != MODEL
            or m['with_concepts'] is not False or m['semantic_review'] is not False):
        raise ValueError('Frozen implementation or dispatch scope changed')
    for field, filename in [('pdf_sha256', 'source.pdf'), ('document_sha256', 'document.json'),
                            ('plan_sha256', 'frozen-plan.json'), ('prefix_sha256', 'shared-prefix.txt')]:
        if m[field] != digest(home / filename):
            raise ValueError('Frozen reading input changed')
    plan = read(home / 'frozen-plan.json')
    if any(m[k] != plan[k] for k in ('planned_extract_requests', 'native_max_requests')):
        raise ValueError('Frozen plan count changed')
    if (plan['planned_extract_requests'] != len(plan['directives'])
            or plan['native_max_requests'] != len(plan['directives']) * (MAX_REPAIR_ROUNDS + 1)):
        raise ValueError('Dispatch count is not derived from the native reading plan')
    return m


def validate_dispatch(task, messages, prefix, sent, bound):
    from litprism.reading import TASK_SEPARATOR
    if task != 'extract' or sent >= bound:
        raise ValueError('Unexpected task or dispatch beyond the native reading plan')
    if not messages or not messages[-1].get('content', '').startswith(prefix + TASK_SEPARATOR):
        raise ValueError('Shared full-paper prefix changed before dispatch')


def live(home, credential_root):
    import copy
    from fastapi.testclient import TestClient
    from litprism.analysis_runs import AnalysisRuns
    from litprism.db import KnowledgeBase
    from litprism.llm import OpenAICompatClient, safe_failure
    from litprism.providers import ProviderStore
    from litprism.secrets import SecretStore
    from litprism.web import server

    home, credential_root = isolated(home), isolated(credential_root)
    m = verify(home)
    with (home / 'started.json').open('x', encoding='utf-8') as stream:
        import json
        json.dump({'manifest_sha256': digest(home / 'manifest.json'), 'never_repeat': True}, stream)
    with sqlite3.connect((credential_root / 'litprism.db').as_uri() + '?mode=ro', uri=True) as con:
        row = next(r for r in con.execute('SELECT base_url,api_key_ref,chat_model FROM providers')
                   if r[0].rstrip('/') == ENDPOINT and r[2] == MODEL)
    key = SecretStore(credential_root).get(row[1])
    if not key:
        raise ValueError('Saved credential unavailable')
    cfg = config(home)
    kb = KnowledgeBase(cfg)
    ProviderStore(kb, cfg.data_path).update(m['provider_id'], api_key=key)
    del key
    prefix = (home / 'shared-prefix.txt').read_text(encoding='utf-8')
    requests = []
    original_kb, original_chat = server.KnowledgeBase, OpenAICompatClient.chat

    def capture(self, messages, **kwargs):
        verify(home)
        from litprism.analysis_runs import binding
        if (self.cfg.effective_base_url.rstrip('/') != ENDPOINT or self.cfg.effective_model != MODEL
                or binding(kb, cfg, self, m['paper_id'], False, False)[0]
                   != read(home / 'frozen-plan.json')['bound']):
            raise ValueError('Actual source/model/settings binding changed before dispatch')
        validate_dispatch(kwargs.get('task'), messages, prefix, len(requests), m['native_max_requests'])
        if not self.durable_single_attempt:
            raise ValueError('Non-durable reading dispatch blocked')
        record = {'task': kwargs['task'], 'messages': copy.deepcopy(messages), 'status': 'started'}
        requests.append(record)
        path = home / f'call-{len(requests):02}.json'
        write(path, record)
        stamp = time.monotonic()
        try:
            text = original_chat(self, messages, **kwargs)
            (home / f'raw-{len(requests):02}.txt').write_text(text, encoding='utf-8')
            record['status'] = 'succeeded'
            return text
        except Exception as exc:
            record.update(status='failed', failure=safe_failure(exc))
            raise
        finally:
            record.update(seconds=round(time.monotonic()-stamp, 3), usage=self.last_usage,
                          settings=self.last_request_settings)
            write(path, record)
            print({'call': len(requests), 'task': record['task'], 'status': record['status'],
                   'seconds': record['seconds']}, flush=True)

    app, rid = None, None
    try:
        server.KnowledgeBase = lambda _: kb
        OpenAICompatClient.chat = capture
        app = server.create_app(cfg)
        client = TestClient(app)
        before = client.get('/api/paper/'+m['paper_id']+'/reading').json()
        write(home / 'reading-before.json', before)
        if not before['ready'] or before['material_id'] != read(home / 'frozen-plan.json')['sources'][0]['material_id']:
            raise ValueError('Current reading material differs')
        response = client.post('/api/paper/'+m['paper_id']+'/analysis-runs', json={
            'with_concepts': False, 'semantic_review': False, 'material_id': before['material_id']})
        response.raise_for_status()
        rid = response.json()['run_id']
        write(home / 'run.json', response.json())
        started = time.monotonic()
        while True:
            result = client.get('/api/analysis-runs/'+rid)
            result.raise_for_status()
            state = result.json()
            if not state['active']:
                break
            if time.monotonic()-started > m['native_max_requests'] * cfg.llm.timeout + 120:
                raise RuntimeError('Observer deadline; do not resend the retained run')
            time.sleep(.25)
        write(home / 'run-detail.json', state)
        write(home / 'calls.json', AnalysisRuns(kb).calls(rid))
        saved = kb.get_analysis(m['paper_id'])
        payload = saved['payload'] if saved else None
        write(home / 'analysis.json', payload)
        write(home / 'paper-view.json', client.get('/api/paper/'+m['paper_id']).json())
        summary = {'status': state['status'], 'requests': len(requests), 'usage': state['usage'],
                   'candidate_snapshot_id': state.get('candidate_snapshot_id'),
                   'candidate_count': state.get('candidate_count'),
                   'extraction_coverage': (payload or {}).get('_coverage'),
                   'citation_check': (payload or {}).get('_citation_check'),
                   'machine_semantic_status': ((payload or {}).get('_semantic_review') or {}).get('status'),
                   'code_unchanged': implementation() == m['implementation'],
                   'semantic_passed': None, 'human_approval': False}
        write(home / 'summary.json', summary)
        print({k: v for k, v in summary.items() if k not in ('extraction_coverage', 'citation_check')}, flush=True)
        return bool(payload) and state['status'] in ('completed', 'partial')
    finally:
        server.KnowledgeBase, OpenAICompatClient.chat = original_kb, original_chat
        if app:
            app.state.tasks.pool.shutdown(wait=True)
        kb.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'live'])
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--pdf', type=Path)
    parser.add_argument('--credential-root', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    os.environ['LITPRISM_HOME'] = str(isolated(args.home))
    if args.action == 'prepare':
        if not args.pdf:
            parser.error('prepare needs the frozen PDF')
        prepare(args.home, args.pdf)
    else:
        if not args.live or not args.credential_root:
            parser.error('Explicit --live and isolated credential source required')
        raise SystemExit(0 if live(args.home, args.credential_root) else 1)
