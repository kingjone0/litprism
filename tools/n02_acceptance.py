"""Freeze -> live N01 paper pipeline and controls -> offline quality report.

No live call without --live, no automatic retry, no user database migration.
Raw calls/PDF/database stay inside a new ignored _selftest.* output folder.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(ROOT/'src'))

from audit_long_paper import live_client, save  # noqa: E402
from audit_semantic_service import grade  # noqa: E402

from litprism.analysis_runs import AnalysisRuns  # noqa: E402
from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.ingest import chunk_document, load_document  # noqa: E402
from litprism.semantics import make_units, payload_hash, reconcile, review_units  # noqa: E402
from litprism.usage import UsageTracker  # noqa: E402

FIXTURE = ROOT/'tools/fixtures/semantic-real-cases.json'
CROSS = ROOT/'tools/fixtures/semantic-cross-cases.json'
LIMITS = {'extract': 12, 'concepts': 0, 'semantic_review': 48, 'semantic_cross_review': 40}
MODEL = 'openai:deepseek/deepseek-v4.1-flash'
ENDPOINT = 'https://api.commandcode.ai/provider/v1'
PROFILES = {
    'model-capacity': {'output': 384_000, 'window': 1_000_000, 'timeout': 300, 'read': 300, 'connect': 30, 'review_output': 384_000},
    'baseline': {'output': 8192, 'timeout': 180, 'read': 60, 'connect': 10, 'review_output': 4096},
    'thinking': {'output': 16384, 'timeout': 300, 'read': 180, 'connect': 30, 'review_output': 4096},
    'thinking-review': {'output': 16384, 'timeout': 300, 'read': 180, 'connect': 30, 'review_output': 8192},
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def implementation():
    paths = sorted((ROOT/'src/litprism').rglob('*.py')) + [Path(__file__), ROOT/'tools/audit_long_paper.py', ROOT/'tools/audit_semantic_service.py']
    return payload_hash({p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in paths})


def config(folder, profile='model-capacity'):
    if (folder/'manifest.json').exists():
        profile = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))['profile']
    settings = PROFILES[profile]
    cfg = Config()
    cfg.app.data_dir = str((folder/'data').resolve())
    cfg.context.window_tokens = settings.get('window', 65536)
    cfg.context.output_reserve_tokens = cfg.llm.max_tokens = settings['output']
    if profile == 'model-capacity':
        cfg.llm.model_window_tokens = 1_000_000
        cfg.llm.model_max_output_tokens = 384_000
        cfg.llm.automatic_output = True
        cfg.llm.review_read_timeout = 300
    return cfg


def prepare(pdf, folder, profile='model-capacity', controls_only=False):
    # Reject unrelated PDFs and existing output before touching any database.
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    digest = sha(pdf.read_bytes())
    if digest != fixture['papers']['fasttext']['sha256']:
        raise ValueError('Expected frozen fastText arXiv v3 PDF; SHA256 differs')
    if folder.exists() or not any(p.startswith('_selftest.') for p in folder.parts):
        raise ValueError('Output must be a new ignored _selftest.* folder')
    doc = load_document(pdf)
    if doc.page_count != 5:
        raise ValueError('Expected all five physical pages')
    settings = PROFILES[profile]
    cfg = config(folder, profile)
    chunks = chunk_document(doc, cfg.retrieval.chunk_size, cfg.retrieval.chunk_overlap)
    folder.mkdir(parents=True)
    (folder/'source.pdf').write_bytes(pdf.read_bytes())
    save(folder/'document.json', {'title': doc.title, 'pages': doc.page_count, 'warnings': doc.warnings,
                                 'page_text': [{'page': p.number, 'text': p.text} for p in doc.pages], 'chunks': chunks})
    manifest = {
        'version': 1, 'paper': 'fasttext-v3', 'url': fixture['papers']['fasttext']['url'],
        'pdf_sha256': digest, 'pages': 5, 'document_sha256': sha((folder/'document.json').read_bytes()),
        'fixture_sha256': sha(FIXTURE.read_bytes()), 'cross_fixture_sha256': sha(CROSS.read_bytes()),
        'implementation_sha256': implementation(), 'model': MODEL, 'endpoint': ENDPOINT,
        'protocol': 'openai_chat_completions', 'profile': profile, 'temperature': 0, 'max_output_tokens': settings['output'],
        'timeout_seconds': settings['timeout'], 'review_read_timeout': settings['read'],
        'review_connect_timeout': settings['connect'], 'review_output_tokens': settings['review_output'],
        'mode': 'controls_only' if controls_only else 'paper_and_controls',
        'window_tokens': settings.get('window', 65536), 'paper_stage_limits': LIMITS,
        'control_requests': len(fixture['cases'])+len(json.loads(CROSS.read_text(encoding='utf-8'))['cases']),
        'thresholds': {'false_accepts': 0, 'positive_release_min': .8, 'cross_errors': 0,
                       'full_extraction_coverage': True, 'full_cross_coverage': True,
                       'paper_pending': 0, 'unjudged_generated_claims': 0},
        'reference_scope': 'Fixed developer regression samples, not independent blind evaluation',
        'scope': 'Five physical pages; references excluded by the ordinary production parser; concepts disabled',
    }
    save(folder/'manifest.json', manifest)
    return manifest


def verify(folder):
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    expected = {'pdf_sha256': sha((folder/'source.pdf').read_bytes()),
                'document_sha256': sha((folder/'document.json').read_bytes()),
                'fixture_sha256': sha(FIXTURE.read_bytes()), 'cross_fixture_sha256': sha(CROSS.read_bytes()),
                'implementation_sha256': implementation()}
    if any(manifest[k] != v for k, v in expected.items()):
        raise ValueError('Frozen input, fixture or implementation changed; prepare a new experiment')
    settings = PROFILES.get(manifest.get('profile'))
    if settings is None:
        raise ValueError('Unknown frozen profile')
    fixed = {'model': MODEL, 'endpoint': ENDPOINT, 'protocol': 'openai_chat_completions',
             'temperature': 0, 'max_output_tokens': settings['output'], 'timeout_seconds': settings['timeout'],
             'review_read_timeout': settings['read'], 'review_connect_timeout': settings['connect'],
             'review_output_tokens': settings['review_output'], 'window_tokens': settings.get('window', 65536),
             'control_requests': len(json.loads(FIXTURE.read_text(encoding='utf-8'))['cases'])
             + len(json.loads(CROSS.read_text(encoding='utf-8'))['cases'])}
    if any(manifest.get(k) != v for k, v in fixed.items()):
        raise ValueError('Frozen service or request policy changed')
    if manifest.get('mode') not in ('controls_only', 'paper_and_controls'):
        raise ValueError('Unknown frozen experiment mode')
    if manifest['paper_stage_limits'] != LIMITS or manifest['thresholds'] != {
        'false_accepts': 0, 'positive_release_min': .8, 'cross_errors': 0,
        'full_extraction_coverage': True, 'full_cross_coverage': True,
        'paper_pending': 0, 'unjudged_generated_claims': 0,
    }:
        raise ValueError('Frozen budget or quality thresholds changed')
    return manifest


def quality(payload, local_rows, cross_rows, *, local_planned=18, cross_planned=5, adjudication=None):
    """Never infer semantic truth from the model's own supported status."""
    payload = payload or {}
    semantic = payload.get('_semantic_review') or {}
    coverage = payload.get('_coverage') or {}
    items = semantic.get('items') or []
    controls = grade(local_rows, local_planned)
    controls['passed'] &= len({r['id'] for r in local_rows}) == len(local_rows)
    controls.update(attempted=len(local_rows), completed=sum(bool(r['complete']) for r in local_rows))
    positives = [r for r in local_rows if r['expected'] == 'supported']
    controls.update(positive_blocked=sum(not r['released'] for r in positives),
                    positive_contradicted=sum(r['raw_verdict'] == 'contradicted' for r in positives),
                    abstentions=sum(r['raw_verdict'] == 'needs_review' for r in local_rows))
    cross_errors = [r['id'] for r in cross_rows if r['complete'] and r['found'] != r['expected_conflict']]
    cross_ok = (len(cross_rows) == cross_planned and len({r['id'] for r in cross_rows}) == len(cross_rows)
                and all(r['complete'] for r in cross_rows) and not cross_errors)
    # Optional independent adjudication is bound to this exact generated payload.
    # It is evaluation data only: it never confirms production knowledge.
    judged = {}
    if adjudication is not None:
        if adjudication.get('analysis_hash') != payload_hash(payload) or not adjudication.get('reviewer'):
            raise ValueError('Adjudication must identify its reviewer and exact analysis version')
        by_id = {i['id']: i for i in items}
        for row in adjudication.get('items', []):
            cid = row['id']
            if cid in judged or cid not in by_id or row.get('text_sha256') != sha(by_id[cid]['text'].encode()):
                raise ValueError('Unknown, duplicate or stale adjudicated claim')
            if row.get('verdict') not in ('supported', 'contradicted', 'insufficient') or not row.get('basis', '').strip():
                raise ValueError('Adjudication requires a verdict and source basis')
            judged[cid] = row['verdict']
    false_release = [i['id'] for i in items if i['status'] == 'machine_supported'
                     and i['id'] in judged and judged[i['id']] != 'supported']
    false_rejection = [i['id'] for i in items if i['status'] != 'machine_supported' and judged.get(i['id']) == 'supported']
    unknown = len(items)-len(judged)
    pipeline_complete = bool(items) and coverage.get('complete') is True and (semantic.get('coverage') or {}).get('complete') is True
    checks = {'frozen_controls': controls['passed'], 'cross_controls': bool(cross_ok),
              'pipeline_complete': pipeline_complete, 'paper_machine_gate': semantic.get('passed') is True,
              'generated_adjudication_complete': bool(items) and unknown == 0,
              'no_generated_false_release': not false_release, 'no_generated_false_rejection': not false_rejection}
    return {'passed': all(checks.values()), 'checks': checks, 'local_controls': controls,
            'cross_controls': {'planned': cross_planned, 'attempted': len(cross_rows),
                               'completed': sum(bool(r['complete']) for r in cross_rows),
                               'error_ids': cross_errors, 'incomplete_ids': [r['id'] for r in cross_rows if not r['complete']],
                               'passed': bool(cross_ok)},
            'paper': {'claims': len(items), 'status_counts': dict(Counter(i['status'] for i in items)),
                      'uncovered_units': coverage.get('uncovered_units'), 'extraction_complete': coverage.get('complete'),
                      'cross_coverage': semantic.get('coverage'), 'pending': semantic.get('pending'),
                      'adjudicated': len(judged), 'unjudged': unknown, 'false_release_ids': false_release,
                      'false_rejection_ids': false_rejection,
                      'precision': None if not judged else 'See claim-level adjudication; partial labels are not whole-paper precision',
                      'recall': None, 'note': 'No omission denominator: recall unknown; pending/unjudged cannot pass.'}}


def local_control(case, sources, cfg, client):
    excerpts = [{**sources[sid], 'id': sid, 'paper_id': sources[sid]['paper'],
                 'paper_title': sources[sid]['paper'], 'tag': f'P{i}'} for i, sid in enumerate(case['source_ids'], 1)]
    items = make_units([('key_conclusions', case['text'], [e['tag'] for e in excerpts])], excerpts, 1)
    if len(items) != 1:
        raise ValueError('Fixed control must yield one claim')
    error = None
    client._n02_last_response = None
    try:
        review_units(items, excerpts, cfg, client)
    except Exception as exc:
        error = type(exc).__name__
    # Invalid schema and missing/unknown IDs cannot become a completed sample.
    verdict = items[0].get('status')
    raw = getattr(client, '_n02_last_response', None) or {}
    reviews = raw.get('reviews') if isinstance(raw, dict) else None
    matches = [r for r in reviews if isinstance(r, dict) and r.get('id') == items[0]['id']] if isinstance(reviews, list) else []
    selected = matches[0] if len(matches) == 1 else {}
    valid = (isinstance(selected.get('reason'), str) and bool(selected['reason'].strip())
             and (selected.get('verdict') != 'supported' or bool(items[0].get('quotes'))))
    return {'id': case['id'], 'expected': case['expected'], 'raw_verdict': selected.get('verdict'),
            'released': verdict == 'machine_supported', 'error_type': error,
            'complete': bool(valid) and not error and not items[0].get('review_error')
            and selected.get('verdict') in ('supported', 'contradicted', 'needs_review'), 'item': items[0]}


def run(folder, client):
    manifest = verify(folder)
    if client.name != manifest['model'] or client.cfg.effective_base_url != manifest['endpoint']:
        raise ValueError('Model or endpoint differs from frozen protocol')
    if (client.cfg.temperature != manifest['temperature'] or client.cfg.max_tokens != manifest['max_output_tokens']
            or client.cfg.timeout != manifest['timeout_seconds']):
        raise ValueError('Model parameters differ from frozen protocol')
    if manifest['profile'] == 'model-capacity' and not client.cfg.automatic_output:
        raise ValueError('Automatic allocation must be enabled for the model-capacity profile')
    # Exclusive start is intentionally not a resume: report is always offline;
    # interrupted paid requests must never be silently repeated by this tool.
    with (folder/'started.json').open('x', encoding='utf-8') as handle:
        json.dump({'manifest_sha256': sha((folder/'manifest.json').read_bytes())}, handle)
    review_env = {'LITPRISM_REVIEW_READ_TIMEOUT': str(manifest['review_read_timeout']),
                  'LITPRISM_REVIEW_CONNECT_TIMEOUT': str(manifest['review_connect_timeout']),
                  'LITPRISM_REVIEW_MAX_TOKENS': str(manifest['review_output_tokens'])}
    previous_env = {k: os.environ.get(k) for k in review_env}
    client.durable_single_attempt = True
    usage, calls = UsageTracker(), []
    original = client.chat_json
    maximum = (sum(manifest['paper_stage_limits'].values()) if manifest['mode'] == 'paper_and_controls' else 0)+manifest['control_requests']
    context = {'scope': 'paper', 'case_id': None}
    def observed(messages, *, task=None):
        if len(calls) >= maximum:
            raise RuntimeError('Acceptance request ceiling reached')
        event = {'sequence': len(calls)+1, 'task': task, 'messages': messages, 'status': 'started', **context}
        calls.append(event)
        path = folder/f'call-{len(calls):03}.json'
        save(path, event)
        started = time.monotonic()
        client.last_usage = None
        client.last_request_settings = {}
        client._n02_last_response = None
        try:
            result = original(messages, task=task)
            client._n02_last_response = result
            event.update(status='succeeded', response=result)
            return result
        except Exception as exc:
            event.update(status='failed', error_type=type(exc).__name__, http_status=getattr(exc, 'http_status', None),
                         cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None)
            raise
        finally:
            event['seconds'] = round(time.monotonic()-started, 3)
            single = UsageTracker()
            single.add(getattr(client, 'last_usage', None), task=task, failed=event['status'] != 'succeeded')
            event['reported_usage'] = single.to_dict()
            settings = getattr(client, 'last_request_settings', {})
            event['request_settings'] = {k: settings[k] for k in ('task', 'timeout', 'temperature', 'max_tokens', 'attempts',
                'output_mode', 'estimated_input_tokens', 'input_safety_tokens', 'model_window_tokens', 'model_max_output_tokens') if k in settings}
            usage.merge(single)
            save(path, event)
            save(folder/'usage.json', usage.to_dict())
            print(json.dumps({'call': event['sequence'], 'task': task, 'status': event['status'], 'seconds': event['seconds']}), flush=True)
    cfg = config(folder)
    # Same effective generation settings as the actual client; never save cfg.
    cfg.llm = copy.deepcopy(client.cfg)
    kb = KnowledgeBase(cfg) if manifest['mode'] == 'paper_and_controls' else None
    client.chat_json = observed
    os.environ.update(review_env)
    local_rows, cross_rows, payload, result = [], [], None, None
    started = time.monotonic()
    try:
        if kb is not None:
            doc = json.loads((folder/'document.json').read_text(encoding='utf-8'))
            pid = kb.add_paper(title=doc['title'], verify_level='manual_confirmed', url=manifest['url'])
            kb.add_chunks(pid, doc['chunks'])
            store = AnalysisRuns(kb)
            rid = store.create(cfg, client, pid, manifest['paper_stage_limits'], with_concepts=False)['id']
            save(folder/'run.json', {'run_id': rid, 'paper_id': pid})
            result = store.execute(rid, cfg, client)
            save(folder/'run-detail.json', result)
            saved = kb.get_analysis(pid)
            if saved:
                payload = saved['payload']
                save(folder/'analysis.json', payload)
            if result['status'] not in ('completed', 'partial'):
                raise RuntimeError('Paper pipeline stopped before publication; controls not expanded')
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        for case in fixture['cases']:
            context.update(scope='local_control', case_id=case['id'])
            local_rows.append(local_control(case, fixture['sources'], cfg, client))
            save(folder/'local-controls.json', local_rows)
        for case in json.loads(CROSS.read_text(encoding='utf-8'))['cases']:
            context.update(scope='cross_control', case_id=case['id'])
            items = [{'id': f'cl{i}', 'text': text, 'batch': i+1, 'field': 'summary', 'facts': [],
                      'status': 'needs_review', 'reason': 'test candidate'} for i, text in enumerate(case['claims'])]
            try:
                differences, coverage = reconcile(items, cfg, client)
            except Exception as exc:
                differences, coverage = [], {'complete': False, 'error_type': type(exc).__name__}
            cross_rows.append({'id': case['id'], 'expected_conflict': case['expected_conflict'],
                               'found': bool(differences), 'complete': coverage['complete'], 'coverage': coverage,
                               'differences': differences})
            save(folder/'cross-controls.json', cross_rows)
    except Exception as exc:
        save(folder/'failure.json', {'error_type': type(exc).__name__})
    finally:
        save(folder/'timing.json', {'elapsed_seconds': round(time.monotonic()-started, 3),
                                   'request_seconds': round(sum(c.get('seconds', 0) for c in calls), 3)})
        if kb is not None:
            kb.close()
        client.chat_json = original
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return report(folder)


def report(folder, adjudication=None):
    def read(name, default):
        path = folder/name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    payload = read('analysis.json', None)
    started = read('started.json', None)
    if started and started['manifest_sha256'] != sha((folder/'manifest.json').read_bytes()):
        raise ValueError('Experiment manifest changed after paid run started')
    assessment = quality(payload, read('local-controls.json', []), read('cross-controls.json', []), adjudication=adjudication)
    recovery = read('recovery-policy.json', None)
    replayed = 0 if recovery is None else recovery.get('confirmed_replayed_requests')
    summary = {'quality': assessment, 'run': read('run-detail.json', None), 'failure': read('failure.json', None),
               'usage': read('usage.json', None), 'timing': read('timing.json', None),
               'billing': {'currency_cost': None, 'note': 'Reported token usage is not a complete provider invoice'},
               'live_requests': len(list(folder.glob('call-*.json'))), 'replayed_requests': replayed,
               'reporter_implementation_sha256': implementation(),
               'reference_scope': 'Fixed developer controls, no new blind/human quality sample'}
    if summary['failure'] or not summary['run'] or summary['run']['status'] not in ('completed', 'partial'):
        summary['quality']['passed'] = False
    mode = read('manifest.json', {}).get('mode', 'paper_and_controls')
    summary['scope'] = mode
    summary['scope_passed'] = (assessment['checks']['frozen_controls'] and assessment['checks']['cross_controls']
                               if mode == 'controls_only' else assessment['passed']) and not summary['failure']
    save(folder/'summary.json', summary)
    if payload:
        save(folder/'adjudication-template.json', {'analysis_hash': payload_hash(payload), 'reviewer': '',
             'items': [{'id': i['id'], 'text_sha256': sha(i['text'].encode()), 'text': i['text'],
                        'verdict': '', 'basis': '', 'sources': i.get('quotes', [])}
                       for i in (payload.get('_semantic_review') or {}).get('items', [])]})
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['prepare', 'run', 'report'])
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--pdf', type=Path)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--provider-root', type=Path, default=Path('E:/codex-litprism-data'))
    ap.add_argument('--adjudication', type=Path)
    ap.add_argument('--profile', choices=list(PROFILES), default='model-capacity', help='Frozen generation/timeout profile (prepare only)')
    ap.add_argument('--controls-only', action='store_true', help='Prepare the frozen control suite without rerunning paper generation')
    args = ap.parse_args()
    if args.action == 'prepare':
        if not args.pdf:
            ap.error('prepare requires --pdf')
        result = prepare(args.pdf, args.output.resolve(), args.profile, args.controls_only)
    elif args.action == 'run':
        if not args.live:
            ap.error('run requires --live and explicit paid-call authorization')
        manifest = verify(args.output)
        client = live_client(args.provider_root)
        client.cfg.max_tokens = manifest['max_output_tokens']
        client.cfg.timeout = manifest['timeout_seconds']
        if manifest['profile'] == 'model-capacity':
            client.cfg.model_window_tokens = manifest['window_tokens']
            client.cfg.model_max_output_tokens = manifest['max_output_tokens']
            client.cfg.automatic_output = True
        result = run(args.output, client)
    else:
        labels = json.loads(args.adjudication.read_text(encoding='utf-8')) if args.adjudication else None
        result = report(args.output, labels)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if args.action == 'prepare' or result['scope_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
