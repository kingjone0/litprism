"""Fixed durable-Web acceptance with real models and explicitly replayed web snapshots.

Only accepts a previously manifested _selftest.* source root. The original DB is
opened read-only for online backup; all migrations and review controls use copies.
Raw inputs, responses, credentials and reports stay in the ignored output root.
This does not certify Tavily acquisition or general semantic correctness.
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
sys.path.insert(0, str(ROOT / 'src'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def scope_policy_matches(snapshot, *, expected_strict):
    """Expected policy comes from the frozen scenario, never the production parser."""
    return isinstance(snapshot, dict) and snapshot.get('strict') is expected_strict


def fingerprints():
    paths = [p for p in (ROOT / 'src').rglob('*') if p.suffix in ('.py', '.js', '.css')]
    paths += [Path(__file__), ROOT / 'tools/chat_mixed_cases.json']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def validate_snapshots(items):
    from urllib.parse import urlparse
    if not isinstance(items, list) or not items:
        raise ValueError('Frozen actual web snapshots required')
    ids = set()
    for item in items:
        if item['id'] in ids or urlparse(item['url']).scheme != 'https':
            raise ValueError('Duplicate snapshot or invalid source URL')
        ids.add(item['id'])
        if (not item.get('obtained_at') or not item.get('acquisition') or
                hashlib.sha256(item['content'].encode()).hexdigest() != item['content_sha256']):
            raise ValueError('Snapshot bytes or acquisition provenance missing/changed')
    return {item['id']: item for item in items}


def backup_readonly(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('Refusing to replace an existing acceptance database')
    src = sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def prepare(home, source, snapshots, cases):
    import yaml
    from litprism.config import _from_dict, config_to_dict
    from litprism.db import KnowledgeBase
    from litprism.providers import ProviderStore
    from litprism.secrets import SecretStore
    source = source.resolve()
    if not source.name.startswith('_selftest.') or not (source / 'manifest.json').is_file():
        raise ValueError('Only an existing isolated, manifested acceptance root is allowed')
    if (home / 'manifest.json').exists():
        raise ValueError('Acceptance root already frozen; no implicit repeat')
    if source == home or source in home.parents:
        raise ValueError('Output must be independent of source acceptance root')
    items = validate_snapshots(json.loads(snapshots.read_text(encoding='utf-8')))
    spec = json.loads(cases.read_text(encoding='utf-8'))
    for group in spec['groups']:
        for case in group['cases']:
            if any(key not in items for key in case['web']):
                raise ValueError('Case references absent snapshot')
    prior = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    cfg = _from_dict(yaml.safe_load((source / 'config/config.yaml').read_text(encoding='utf-8')))
    if cfg.llm.api_key:
        raise ValueError('Plaintext configuration credentials are prohibited')
    home.mkdir(parents=True, exist_ok=True)
    backup_readonly(source / 'data/litprism.db', home / 'data/litprism.db')
    cfg.app.data_dir = str(home / 'data')
    cfg.export.notes_dir = str(home / 'notes')
    cfg.llm.timeout = cfg.llm.review_read_timeout = 900
    cfg.context.overflow_retries = 0
    for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
        getattr(cfg.sources, name).enabled = False
    cfg.websearch.api_key_ref = ''
    cfg.websearch.provider = 'tavily'
    kb = KnowledgeBase(cfg)
    try:
        store = ProviderStore(kb)
        active, model = store.get_active()
        if not active or not model:
            raise ValueError('Prior selected real model required')
        secret = SecretStore(source / 'data').get(active.api_key_ref)
        if not secret or store.secrets.backend == 'weak':
            raise ValueError('Secure stored model credential unavailable')
        ref = store.secrets.set('n04_mixed', secret)
        del secret
        with kb.tx() as con:
            con.execute('UPDATE providers SET api_key_ref=? WHERE id=?', (ref, active.id))
        if kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]:
            raise ValueError('Source corpus must not contain pre-existing test knowledge')
        materials = []
        for paper in prior['papers']:
            chunks = kb.get_chunks(paper['paper_id'])
            if (len(chunks) != paper['chunks'] or
                    kb.get_paper(paper['paper_id'])['current_material_id'] != paper['material_id']):
                raise ValueError('Frozen paper material changed')
            materials.append(dict(paper, source_hash=hashlib.sha256(
                json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()))
        (home / 'config').mkdir(exist_ok=True)
        (home / 'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True), encoding='utf-8')
        (home / 'cases.json').write_bytes(cases.read_bytes())
        if snapshots.resolve() != (home / 'web-snapshots.json').resolve():
            (home / 'web-snapshots.json').write_bytes(snapshots.read_bytes())
        write(home / 'manifest.json', {
            'purpose': spec['purpose'], 'authorization': 'Fixed acceptance to completion; no per-task manual token cap',
            'model': model, 'base_url': active.base_url, 'schema': kb._con.execute('PRAGMA user_version').fetchone()[0],
            'source_root': str(source), 'papers': materials, 'cases_sha256': sha(home / 'cases.json'),
            'snapshots_sha256': sha(home / 'web-snapshots.json'), 'code_sha256': fingerprints(),
            'planned_turns': sum(len(g['cases']) for g in spec['groups']),
            'search_mode': 'actual_public_abstract_snapshots_replayed_at_discover_web_boundary',
            'tavily_live_quality': 'not_tested_no_configured_key', 'daily_database_opened': False,
            'human_actions': 'scripted operator controls in isolated copies, not user approvals',
            'policy': 'one first attempt per turn; preserve service failures; no paid retry; stop after three consecutive service failures; dependent actions require a saved reply',
        })
    finally:
        kb.close()


def citation_audit(kb, review, draft):
    from litprism.textutil import parse_citations
    from litprism.qa_reviews import resolve
    pool = {e['tag']: e for e in review.get('sources', [])}
    out = []
    # Resolve against the frozen server-owned references, not the model's map.
    payload = json.loads(kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (review['id'],)).fetchone()[0]) if review else {}
    refs = {e['tag']: e for e in payload.get('_citations', [])}
    for tag, page in parse_citations(draft):
        issue = None
        try:
            source = resolve(kb, refs[tag], payload['scope'])
            if page is not None and str(page) != str(source.get('page')):
                raise ValueError('Explicit wrong page')
        except (ValueError, KeyError) as exc:
            issue = str(exc)
        out.append({'tag': tag, 'valid': issue is None, 'issue': issue,
                    'kind': pool.get(tag, {}).get('source_kind'), 'ref': refs.get(tag, {}).get('chunk_id')})
    return out


def inspect_result(kb, client, cid, rid, case, state, seconds):
    result = client.get('/api/chat-runs/' + rid + '/result').json() if state['status'] == 'completed' else {}
    msg = result.get('assistant') or {}
    review = client.get('/api/chat-reviews/' + msg['qa_review_id']).json() if msg.get('qa_review_id') else {}
    draft = review.get('draft') or msg.get('content', '')
    cites = citation_audit(kb, review, draft)
    details = {link['ref']: client.get('/api/evidence/' + link['ref']).json() for link in msg.get('source_links', [])}
    return {'id': case['id'], 'question': case['question'], 'expect': case['expect'], 'conversation_id': cid,
            'run_id': rid, 'run_status': state['status'], 'stop_reason': state.get('stop_reason'),
            'status': result.get('status', 'error'), 'answer': msg.get('content', ''), 'draft': draft,
            'usage': state['usage'], 'calls': state['calls'], 'elapsed_seconds': round(seconds, 3),
            'source_links': msg.get('source_links', []), 'source_details': details,
            'source_warning': msg.get('source_warning'), 'citation_audit': cites,
            'review': review, 'retrieval': msg.get('retrieval', {}), 'scope_snapshot': msg.get('scope_snapshot'),
            'guard_issues': msg.get('guard_issues', []), 'audit_warnings': msg.get('audit_warnings', []),
            'assistant_ref': msg.get('ref'), 'mechanical_checks': {}, 'semantic_judgment': None}


def wait(client, rid, started):
    while True:
        response = client.get('/api/chat-runs/' + rid)
        response.raise_for_status()
        state = response.json()
        if not state['active']:
            return state
        if time.monotonic() - started > 1900:
            raise RuntimeError('Observer wait elapsed; retained run must not be resent')
        time.sleep(.25)


def confirm(client, row, paper_id, text):
    review = client.get('/api/chat-reviews/' + row['review']['id']).json()
    selected = [e for e in review['sources'] if e.get('paper_id') == paper_id and e['citable']]
    if not selected:
        raise ValueError('No current paper source available for scripted review control')
    # Require the direct abstract statement; a first-page author/header chunk is
    # not interchangeable with the actual passage supporting this control.
    needle = ('jointly conditioning on both left and right context' if 'BERT' in text
              else 'dispensing with recurrence and convolutions')
    source = next((e for e in selected if needle in ' '.join(e.get('text', '').split())), None)
    if source is None:
        raise ValueError('Direct supporting abstract passage not delivered; do not manufacture an operator approval')
    response = client.post('/api/chat-reviews/' + review['id'] + '/review', json={
        'action': 'confirm', 'note': '隔离验收的脚本化操作员控制：逐字核对首段原文；不是用户人工裁决。',
        'reviewed_text': text + ' [' + source['tag'] + ']', 'source_tags': [source['tag']],
        'snapshot_hash': review['snapshot_hash'], 'review_token': review['review_token']})
    response.raise_for_status()
    return response.json()['knowledge_id']


def run(home):
    import yaml
    from fastapi.testclient import TestClient
    from litprism.config import _from_dict
    from litprism.db import KnowledgeBase
    from litprism.knowledge import create_source, get_source, review_source
    from litprism.secrets import SecretStore
    from litprism.web import server, evidence_routes
    from litprism import qa_reviews
    from litprism.context.dialogue import build_dialogue
    manifest = json.loads((home / 'manifest.json').read_text(encoding='utf-8'))
    if fingerprints() != manifest['code_sha256'] or sha(home / 'cases.json') != manifest['cases_sha256'] or sha(home / 'web-snapshots.json') != manifest['snapshots_sha256']:
        raise ValueError('Frozen inputs or implementation changed')
    marker = home / 'started.json'
    with marker.open('x', encoding='utf-8') as f:
        json.dump({'started': time.time(), 'policy': 'Never rerun an existing acceptance root'}, f)
    snapshots = validate_snapshots(json.loads((home / 'web-snapshots.json').read_text(encoding='utf-8')))
    spec = json.loads((home / 'cases.json').read_text(encoding='utf-8'))
    papers = {p['alias']: p['paper_id'] for p in manifest['papers']}
    original_kb, original_discover, original_save = server.KnowledgeBase, evidence_routes.discover_web, qa_reviews.save
    rows, events, lifecycle = [], [], []
    consecutive_failures = 0
    started = time.monotonic()
    for group in spec['groups']:
        if consecutive_failures >= 3:
            break
        if fingerprints() != manifest['code_sha256']:
            raise RuntimeError('Frozen implementation changed during acceptance')
        group_home = home / group['id']
        backup_readonly(home / 'data/litprism.db', group_home / 'data/litprism.db')
        cfg = _from_dict(yaml.safe_load((home / 'config/config.yaml').read_text(encoding='utf-8')))
        cfg.app.data_dir = str(group_home / 'data')
        cfg.export.notes_dir = str(group_home / 'notes')
        kb = KnowledgeBase(cfg)
        seed_store = SecretStore(home / 'data')
        secret_store = SecretStore(cfg.data_path)
        for provider in list(kb._con.execute('SELECT id,api_key_ref FROM providers')):
            secret = seed_store.get(provider['api_key_ref'])
            if not secret or secret_store.backend == 'weak':
                raise ValueError('Secure provider credential unavailable in acceptance copy')
            ref = secret_store.set('n04_mixed_group', secret)
            del secret
            with kb.tx() as con:
                con.execute('UPDATE providers SET api_key_ref=? WHERE id=?', (ref, provider['id']))
        app = None
        try:
            for alias, pid in papers.items():
                if alias not in group['available']:
                    kb.update_paper(pid, verify_level='unchecked')
            wrong = None
            if group.get('seed_conflict'):
                wrong = create_source(kb, kind='knowledge', title='BERT 预训练阅读笔记（2025-01-01）',
                    content='BERT 的编码器预训练只使用左侧上下文，不能同时利用左右文。该条记录日期为2025-01-01。')
                review_source(kb, wrong['id'], action='confirm', note='故意错误的隔离验收控制项，测试可信标签不能替代内容核对。', content_hash=wrong['content_hash'])
            server.KnowledgeBase = lambda _: kb
            app = server.create_app(cfg)
            client = TestClient(app)
            cid = kb.add_conversation('N04 mixed ' + group['id'], scope={
                'paper_ids': [papers[a] for a in group['focus']], 'mode': group.get('mode', 'focus')})
            curated = None
            for case in group['cases']:
                if consecutive_failures >= 3:
                    break
                if case.get('empty_scope'):
                    changed = client.patch('/api/conversations/' + cid, json={'scope': {'paper_ids': [], 'mode': case.get('scope_mode', group.get('mode', 'focus'))}})
                    changed.raise_for_status()
                def replay(_kb, _cfg, query):
                    if case.get('real_missing_search'):
                        events.append({'id': case['id'], 'mode': 'production_unconfigured_tavily'})
                        return original_discover(_kb, _cfg, query)
                    items = [snapshots[key] for key in case['web']]
                    events.append({'id': case['id'], 'mode': 'frozen_actual_abstract_replay', 'query': query,
                                   'snapshot_ids': case['web'], 'original_obtained_at': [i['obtained_at'] for i in items]})
                    return {'items': [create_source(_kb, kind='web', title=i['title'], url=i['url'], content=i['content']) for i in items]}
                evidence_routes.discover_web = replay
                if case.get('publication_fault'):
                    def fail_publish(*args, **kwargs):
                        raise RuntimeError('Acceptance injected interruption AFTER successful response persistence')
                    qa_reviews.save = fail_publish
                stamp = time.monotonic()
                before_events = len(events)
                print(json.dumps({'stage': 'start', 'id': case['id']}), flush=True)
                response = client.post('/api/conversations/' + cid + '/chat-runs', json={
                    'question': case['question'], 'request_id': 'mixed-fixed-' + case['id'],
                    'web_search': bool(case['web'] or case.get('real_missing_search'))})
                response.raise_for_status()
                rid = response.json()['run_id']
                state = wait(client, rid, stamp)
                if case.get('publication_fault'):
                    first = copy.deepcopy(state)
                    write(group_home / 'publication-first-failure.json', first)
                    qa_reviews.save = original_save
                    if state['status'] == 'failed' and state['calls'] and all(c['status'] == 'succeeded' for c in state['calls']):
                        calls_before = kb._con.execute('SELECT count(*) FROM chat_run_calls').fetchone()[0]
                        events_before = len(events)
                        # Idempotent repeated receipt and reopen do not dispatch.
                        client.get('/api/conversations/' + cid + '/chat-runs').raise_for_status()
                        client.post('/api/chat-runs/' + rid + '/resume', json={}).raise_for_status()
                        state = wait(client, rid, stamp)
                        lifecycle.append({'id': case['id'], 'initial_status': first['status'], 'final_status': state['status'],
                            'saved_calls_reused': kb._con.execute('SELECT count(*) FROM chat_run_calls').fetchone()[0] == calls_before,
                            'search_not_repeated': len(events) == events_before, 'first_usage': first['usage'], 'final_usage': state['usage']})
                row = inspect_result(kb, client, cid, rid, case, state, time.monotonic() - stamp)
                checks = row['mechanical_checks']
                if row['scope_snapshot'] is not None:
                    checks['requested_scope_policy_preserved'] = scope_policy_matches(
                        row['scope_snapshot'], expected_strict=case.get(
                            'expect_strict', case.get('scope_mode', group.get('mode')) == 'strict'))
                checks['all_actual_citations_valid'] = all(c['valid'] for c in row['citation_audit'])
                checks['answered_has_citations'] = row['status'] != 'answered' or bool(row['citation_audit'])
                checks['cited_tags_have_links'] = {c['tag'] for c in row['citation_audit']} <= {s.get('tag') for s in row['source_links']} if row['status'] == 'answered' else True
                checks['pending_web_not_global_trusted'] = not any(r[0] != 'pending' for r in kb._con.execute("SELECT status FROM evidence_sources WHERE kind='web'"))
                delivered = row['retrieval'].get('delivered_chunk_ids', [])
                if curated:
                    ids = [r[0] for r in kb._con.execute('SELECT id FROM evidence_chunks WHERE source_id=?', (curated,))]
                    if case.get('require_curated'):
                        checks['new_curated_actually_delivered'] = bool(set(ids) & set(delivered))
                    if case.get('exclude_curated'):
                        checks['invalid_curated_excluded'] = not set(ids) & set(delivered) and not get_source(kb, curated)['citable']
                if case.get('scope_mode', group.get('mode')) == 'strict':
                    checks['strict_no_search'] = len(events) == before_events
                    checks['strict_only_selected_paper'] = set(row['retrieval'].get('evidence_papers', [])) <= {papers['transformer']}
                if case.get('empty_scope'):
                    checks['empty_selection_is_unrestricted'] = (row['scope_snapshot'] or {}).get('paper_ids') is None
                    if case.get('scope_mode', group.get('mode')) != 'strict' and case['web']:
                        checks['empty_selection_permits_enabled_web'] = len(events) > before_events
                if case.get('real_missing_search'):
                    checks['missing_search_explicit_warning'] = bool(row['source_warning']) and not any(s['kind'] == 'web' for s in row['source_links'])
                rows.append(row)
                write(home / 'progress.json', {'rows': rows, 'events': events, 'lifecycle': lifecycle})
                print(json.dumps({'stage': 'done', 'id': case['id'], 'status': row['status'], 'seconds': row['elapsed_seconds'],
                    'calls': state['usage']['calls'], 'tokens': state['usage']['input'] + state['usage']['output'],
                    'mechanical_failures': [k for k, v in checks.items() if not v]}, ensure_ascii=False), flush=True)
                failed = state['status'] != 'completed'
                consecutive_failures = consecutive_failures + 1 if failed else 0
                if failed or not row['review']:
                    if case.get('after'):
                        lifecycle.append({'id': case['id'], 'dependent_actions': 'not_run_no_saved_reply'})
                        break
                    continue
                if case.get('after') in ('confirm_bert', 'confirm_and_resegment'):
                    pid = papers['bert'] if case['after'] == 'confirm_bert' else papers['transformer']
                    text = ('BERT 编码器预训练通过同时利用左右上下文学习深层双向表示。' if case['after'] == 'confirm_bert'
                            else 'Transformer 原论文提出基于注意力的基本架构，不依赖循环或卷积。')
                    curated = confirm(client, row, pid, text)
                    if wrong:
                        review_source(kb, wrong['id'], action='revoke', note='隔离验收脚本化操作员核对原文后撤销错误笔记。', content_hash=wrong['content_hash'])
                    lifecycle.append({'id': case['id'], 'curated': curated, 'action': 'scripted_confirm', 'user_approval': False})
                    if case['after'] == 'confirm_and_resegment':
                        chunks = kb.get_chunks(pid)
                        original = chunks[0]
                        split = original['text'].rfind(' ', 0, len(original['text']) // 2)
                        if split <= 0:
                            split = len(original['text']) // 2
                        revised = [dict(original, text=original['text'][:split]), dict(original, text=original['text'][split:]), *chunks[1:]]
                        for chunk in revised:
                            chunk.pop('id', None)
                        kb.add_chunks(pid, revised, level='fulltext', origin='acceptance resegmentation of identical original text')
                        lifecycle.append({'id': case['id'], 'version_changed': kb.get_paper(pid)['current_material_id'] != original['material_id'],
                            'knowledge_state': get_source(kb, curated)['effective_status'], 'original_text_unchanged_except_chunk_boundary': True})
                if case.get('after') == 'withdraw_bert':
                    kb.update_paper(papers['bert'], verify_level='unchecked')
                    context = build_dialogue(kb, cfg, cid, None)
                    messages = client.get('/api/conversations/' + cid + '/messages').json()['items']
                    lifecycle.append({'id': case['id'], 'action': 'withdraw_upstream', 'knowledge_state': get_source(kb, curated)['effective_status'],
                        'knowledge_citable': get_source(kb, curated)['citable'],
                        'remaining_assistant_history': [m['content'] for m in context.history if m['role'] == 'assistant'],
                        'reopened_statuses': [m.get('status') for m in messages if m['role'] == 'assistant']})
            write(group_home / 'run-complete.json', {'calls': [dict(r) for r in kb._con.execute('SELECT run_id,task,status,usage FROM chat_run_calls WHERE run_id LIKE ?', ('cr_%',))]})
        finally:
            qa_reviews.save, evidence_routes.discover_web, server.KnowledgeBase = original_save, original_discover, original_kb
            if app:
                app.state.tasks.pool.shutdown(wait=True)
            kb.close()
    usage = {k: sum(r['usage'][k] for r in rows) for k in ('calls', 'input', 'output', 'unknown_usage_calls', 'failed_calls')}
    summary = {'planned': manifest['planned_turns'], 'completed': len(rows), 'elapsed_seconds': round(time.monotonic() - started, 2),
        'usage': usage, 'all_mechanical_checks_passed': len(rows) == manifest['planned_turns'] and all(all(r['mechanical_checks'].values()) for r in rows),
        'all_requests_completed': len(rows) == manifest['planned_turns'] and all(r['run_status'] == 'completed' for r in rows),
        'semantic_passed': None, 'live_tavily_passed': None, 'search_mode': manifest['search_mode'],
        'rows': rows, 'events': events, 'lifecycle': lifecycle, 'code_unchanged': fingerprints() == manifest['code_sha256']}
    write(home / 'results.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('rows', 'events', 'lifecycle')}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--snapshots', type=Path)
    parser.add_argument('--cases', type=Path, default=ROOT / 'tools/chat_mixed_cases.json')
    parser.add_argument('--live', action='store_true', help='Run the frozen suite once using the selected paid model')
    args = parser.parse_args()
    home = args.output.resolve()
    if not home.name.startswith('_selftest.'):
        parser.error('Output must be an isolated _selftest.* root')
    os.environ['LITPRISM_HOME'] = str(home)
    if args.prepare:
        if not args.source_root or not args.snapshots or args.live:
            parser.error('--prepare needs source-root and snapshots, and cannot send live requests')
        prepare(home, args.source_root, args.snapshots, args.cases)
        print('Frozen acceptance prepared; zero live model calls.')
    elif args.live:
        run(home)
    else:
        parser.error('Choose --prepare or --live explicitly')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
