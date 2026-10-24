"""Fixed paired real-model acceptance; never retries or automatically certifies semantics.

Both arms clone a single scenario database, including identical external snapshot
IDs. Only the internal anchor flag differs. All raw messages/results remain local.
This replays already acquired public abstracts, not a live-search quality test.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
import chat_mixed_acceptance as mixed  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fingerprints():
    paths = [p for p in (ROOT / 'src').rglob('*') if p.suffix in ('.py', '.js', '.css')]
    paths += [Path(__file__), ROOT / 'tools/anchor_live_cases.json', ROOT / 'tools/chat_mixed_acceptance.py']
    return {p.relative_to(ROOT).as_posix(): mixed.sha(p) for p in sorted(paths)}


def delivery_signature(sources):
    # Per-request grant IDs/timestamps differ; source identity, order, text, trust,
    # material and physical page must not. Do not compare only cited sources.
    return digest([{k: s.get(k) for k in ('tag', 'chunk_id', 'paper_id', 'page', 'material_id',
                    'source_hash', 'title', 'text', 'source_kind', 'trust_level')} for s in sources]) if sources else None


def task_limits(group):
    # Unrestricted retrieval also makes a paid query-expansion call. This is a
    # different task, not a retry. Preserve this distinction in the frozen plan.
    return {'qa': 1, **({'expand': 1} if not group['focus'] else {})}


def claim_dispatch(counts, limits, task):
    if counts.get(task, 0) >= limits.get(task, 0):
        raise RuntimeError('Unplanned model task or repeated task blocked before dispatch')
    counts[task] = counts.get(task, 0) + 1


def measurements(value, audit):
    if value is None:
        return None  # A service failure is unknown, not zero errors or a pass.
    proposed = value.get('answer_units') if 'answer_units' in value else value.get('evidence_map')
    records = proposed if isinstance(proposed, list) else []
    raw = [q for item in records if isinstance(item, dict) and isinstance(item.get('quotes'), list)
           for q in item['quotes'] if isinstance(q, dict)]
    located = [q for item in audit.get('items', []) for q in item.get('quotes', [])]
    return {'claims_proposed': len(records), 'quotes_proposed': len(raw),
            'anchors_proposed': sum('anchor_id' in q for q in raw),
            'raw_quotes_proposed': sum('quote' in q and 'anchor_id' not in q for q in raw),
            'quotes_located': len(located),
            'anchors_resolved': sum(q.get('kind') == 'source_anchor' for q in located),
            'location_blockers': audit.get('blocking_issues', []),
            'mapping_warnings': audit.get('warnings', []),
            'backfilled_characters': sum(len(q.get('quote', '')) for q in located if q.get('kind') == 'source_anchor'),
            'displayed_quote_characters': sum(len(q.get('quote', '')) for q in located),
            'semantic_passed': None, 'human_review_time_seconds': None,
            'cost_limit': 'characters are a workload proxy, not measured human review time'}


def clone(home, seed, base_cfg, secret_home):
    from litprism.config import _from_dict, config_to_dict
    from litprism.db import KnowledgeBase
    from litprism.secrets import SecretStore
    cfg = _from_dict(config_to_dict(base_cfg))
    cfg.app.data_dir, cfg.export.notes_dir = str(home / 'data'), str(home / 'notes')
    mixed.backup_readonly(seed, home / 'data/litprism.db')
    kb = KnowledgeBase(cfg)
    source, target = SecretStore(secret_home / 'data'), SecretStore(home / 'data')
    for p in list(kb._con.execute('SELECT id,api_key_ref FROM providers')):
        secret = source.get(p['api_key_ref'])
        if not secret or target.backend == 'weak':
            kb.close()
            raise ValueError('Secure credential unavailable; no mock fallback')
        ref = target.set('anchor_acceptance', secret)
        del secret
        with kb.tx() as con:
            con.execute('UPDATE providers SET api_key_ref=? WHERE id=?', (ref, p['id']))
    return cfg, kb


def prepare(home, source, snapshots, cases):
    mixed.prepare(home, source, snapshots, cases)
    manifest = json.loads((home / 'manifest.json').read_text(encoding='utf-8'))
    groups = json.loads(cases.read_text(encoding='utf-8'))['groups']
    limits = {g['id']: task_limits(g) for g in groups}
    manifest.update(code_sha256=fingerprints(), planned_qa_calls=manifest['planned_turns'] * 2,
                    planned_calls=2 * sum(sum(t.values()) for t in limits.values()), model_task_limits=limits,
                    protocol='six independent paired scenarios, alternating arm order, one attempt per arm',
                    authorization='User: fixed acceptance to completion, then explicitly requested verification; no manual token cap',
                    source_results_sha256=mixed.sha(source / 'qa-results.json'),
                    search_quality_tested=False, semantic_passed=None)
    mixed.write(home / 'manifest.json', manifest)


def run(home):
    import yaml
    from fastapi.testclient import TestClient
    from litprism.config import _from_dict
    from litprism.knowledge import create_source, get_source
    from litprism.llm import OpenAICompatClient
    from litprism.web import server, evidence_routes
    manifest = json.loads((home / 'manifest.json').read_text(encoding='utf-8'))
    if (manifest['code_sha256'] != fingerprints() or mixed.sha(home / 'cases.json') != manifest['cases_sha256']
            or mixed.sha(home / 'web-snapshots.json') != manifest['snapshots_sha256']):
        raise ValueError('Frozen inputs or implementation changed')
    with (home / 'started.json').open('x', encoding='utf-8') as f:
        json.dump({'started': time.time(), 'no_repeat': True}, f)
    spec = json.loads((home / 'cases.json').read_text(encoding='utf-8'))
    snapshots = mixed.validate_snapshots(json.loads((home / 'web-snapshots.json').read_text(encoding='utf-8')))
    cfg = _from_dict(yaml.safe_load((home / 'config/config.yaml').read_text(encoding='utf-8')))
    papers = {p['alias']: p['paper_id'] for p in manifest['papers']}
    original_kb, original_discover, original_chat = server.KnowledgeBase, evidence_routes.discover_web, OpenAICompatClient.chat
    rows, pairs = [], []
    failures = 0
    for index, group in enumerate(spec['groups']):
        if failures >= 3:
            break
        if manifest['code_sha256'] != fingerprints():
            raise RuntimeError('Frozen code changed during acceptance')
        case = group['cases'][0]
        seed_home = home / group['id'] / 'seed'
        seed_cfg, seed_kb = clone(seed_home, home / 'data/litprism.db', cfg, home)
        web_ids = []
        try:
            for alias, pid in papers.items():
                if alias not in group['available']:
                    seed_kb.update_paper(pid, verify_level='unchecked')
            for key in case['web']:
                s = snapshots[key]
                web_ids.append(create_source(seed_kb, kind='web', title=s['title'], url=s['url'], content=s['content'])['id'])
        finally:
            seed_kb.close()
        arms = ('quotes', 'anchors') if index % 2 == 0 else ('anchors', 'quotes')
        pair_rows = []
        for arm in arms:
            if failures >= 3:
                break
            arm_home = home / group['id'] / arm
            arm_cfg, kb = clone(arm_home, seed_home / 'data/litprism.db', seed_cfg, seed_home)
            arm_cfg.qa.evidence_anchor_trial = arm == 'anchors'
            app = None
            requests, searches, dispatched = [], [], {}
            def capture(self, messages, **kwargs):
                claim_dispatch(dispatched, manifest['model_task_limits'][group['id']], kwargs.get('task'))
                requests.append({'task': kwargs.get('task'), 'messages': copy.deepcopy(messages),
                                 'single_attempt': bool(getattr(self, 'durable_single_attempt', False))})
                mixed.write(arm_home / 'requests.json', requests)
                return original_chat(self, messages, **kwargs)
            def replay(_kb, _cfg, query):
                searches.append({'query': query, 'snapshot_ids': case['web']})
                return {'items': [get_source(_kb, sid) for sid in web_ids]}
            try:
                server.KnowledgeBase = lambda _: kb
                evidence_routes.discover_web, OpenAICompatClient.chat = replay, capture
                app = server.create_app(arm_cfg)
                client = TestClient(app)
                cid = kb.add_conversation('Anchor paired ' + group['id'] + ' ' + arm,
                    scope={'paper_ids': [papers[a] for a in group['focus']], 'mode': 'focus'})
                print(json.dumps({'stage': 'start', 'case': case['id'], 'arm': arm}), flush=True)
                stamp = time.monotonic()
                r = client.post('/api/conversations/' + cid + '/chat-runs', json={
                    'question': case['question'], 'request_id': 'anchor-fixed-' + case['id'] + '-' + arm,
                    'web_search': bool(case['web'])})
                r.raise_for_status()
                rid = r.json()['run_id']
                state = mixed.wait(client, rid, stamp)
                row = mixed.inspect_result(kb, client, cid, rid, case, state, time.monotonic() - stamp)
                saved = kb._con.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa' AND status='succeeded'", (rid,)).fetchone()
                value = json.loads(saved['response']).get('value') if saved else None
                audit = row['review'].get('evidence_audit', {})
                sources = row['review'].get('sources', [])
                row.update(arm=arm, raw_value=value, request_count=len(requests), searches=searches,
                           delivery_signature=delivery_signature(sources), measurements=measurements(value, audit),
                           first_attempt_only=len(requests) == state['usage']['calls'] and dispatched.get('qa') == 1
                               and all(x['single_attempt'] for x in requests))
                row['mechanical_checks'] = {
                    'first_attempt_only': row['first_attempt_only'],
                    'citable_references_resolve': all(c['valid'] for c in row['citation_audit']),
                    'answered_has_citations': row['status'] != 'answered' or bool(row['citation_audit']),
                    'focus_policy_preserved': row['scope_snapshot'] is None or mixed.scope_policy_matches(row['scope_snapshot'], expected_strict=False),
                    'search_performed_when_requested': bool(searches) == bool(case['web']),
                    'external_sources_still_pending': all(r[0] == 'pending' for r in kb._con.execute("SELECT status FROM evidence_sources WHERE kind='web'")),
                }
                if arm == 'anchors' and value is not None:
                    row['mechanical_checks']['trial_activated'] = row['retrieval'].get('evidence_protocol') == 'anchors_trial_v2'
                mixed.write(arm_home / 'result.json', row)
                rows.append(row)
                pair_rows.append(row)
                mixed.write(home / 'progress.json', {'rows': rows, 'pairs': pairs})
                print(json.dumps({'stage': 'done', 'case': case['id'], 'arm': arm, 'status': row['status'],
                                  'seconds': row['elapsed_seconds'], 'tokens': state['usage']['input'] + state['usage']['output'],
                                  'anchors': (row['measurements'] or {}).get('anchors_resolved'),
                                  'location_blockers': len((row['measurements'] or {}).get('location_blockers', []))}), flush=True)
                failures = failures + 1 if state['status'] != 'completed' else 0
            finally:
                server.KnowledgeBase, evidence_routes.discover_web, OpenAICompatClient.chat = original_kb, original_discover, original_chat
                if app:
                    app.state.tasks.pool.shutdown(wait=True)
                kb.close()
        signatures = [r['delivery_signature'] for r in pair_rows]
        pairs.append({'case': case['id'], 'both_observed': len(pair_rows) == 2,
                      'same_delivery': len(signatures) == 2 and signatures[0] is not None and signatures[0] == signatures[1],
                      'semantic_passed': None})
        mixed.write(home / 'progress.json', {'rows': rows, 'pairs': pairs})
    usage = {k: sum(r['usage'][k] for r in rows) for k in ('calls', 'input', 'output', 'unknown_usage_calls', 'failed_calls')}
    source = Path(manifest['source_root'])
    result = {'planned_calls': manifest['planned_calls'], 'observed_turns': len(rows), 'usage': usage,
              'code_unchanged': fingerprints() == manifest['code_sha256'],
              'original_results_unchanged': mixed.sha(source / 'qa-results.json') == manifest['source_results_sha256'],
              'daily_database_opened': False, 'search_quality_tested': False, 'semantic_passed': None,
              'rows': rows, 'pairs': pairs}
    mixed.write(home / 'results.json', result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('rows', 'pairs')}), flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--snapshots', type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--prepare', action='store_true')
    modes.add_argument('--live', action='store_true')
    args = parser.parse_args()
    home = args.output.resolve()
    if not home.name.startswith('_selftest.'):
        parser.error('Output must be isolated _selftest.*')
    os.environ['LITPRISM_HOME'] = str(home)
    if args.prepare:
        if not args.source_root or not args.snapshots:
            parser.error('Preparation requires existing isolated source and actual frozen snapshots')
        prepare(home, args.source_root.resolve(), args.snapshots.resolve(), ROOT / 'tools/anchor_live_cases.json')
        print('Frozen six paired scenarios; zero model calls during preparation.')
    else:
        run(home)
