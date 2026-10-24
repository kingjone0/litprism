"""Three frozen first-attempt QA calls. Input drift is blocked BEFORE dispatch.

Only local isolated backups and DPAPI-protected provider refs are used. No daily
database, real search, extraction, query expansion, retries or automatic release.
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
import anchor_live_acceptance as paired  # noqa: E402
import chat_mixed_acceptance as mixed  # noqa: E402


def fingerprints():
    paths = [p for p in (ROOT / 'src').rglob('*') if p.suffix in ('.py', '.js', '.css')]
    paths += [Path(__file__), ROOT / 'tools/answer_units_live_cases.json', ROOT / 'tools/anchor_live_acceptance.py',
              ROOT / 'tools/chat_mixed_acceptance.py']
    return {p.relative_to(ROOT).as_posix(): mixed.sha(p) for p in sorted(paths)}


def material(messages):
    return messages[-1]['content'].split('【片段】\n', 1)[1].split('\n\n【程序送达记录】', 1)[0]


def validate_dispatch(task, messages, frozen, already_sent):
    if task != 'qa' or already_sent:
        raise RuntimeError('Unplanned task or repeated QA blocked before dispatch')
    if hashlib.sha256(material(messages).encode()).hexdigest() != frozen['material_sha256']:
        raise RuntimeError('Frozen source input changed; blocked before dispatch')


def freeze_question(kb, cfg, real, question, paper_ids):
    from litprism.agents import QAAgent
    class CaptureOnly:
        name = real.name
        runtime_config = copy.deepcopy(getattr(real, 'runtime_config', cfg))
        def __init__(self):
            self.calls = []
        def chat_json(self, messages, *, task=None):
            self.calls.append({'task': task, 'messages': copy.deepcopy(messages)})
            raise RuntimeError('Offline input freeze: no provider dispatch')
    capture = CaptureOnly()
    result = QAAgent(kb, cfg, capture).ask(question, paper_ids=paper_ids, strict_scope=False)
    if [c['task'] for c in capture.calls] != ['qa'] or not result.excerpts:
        raise ValueError('Input cannot be frozen without auxiliary model calls')
    return {'question': question, 'paper_ids': paper_ids, 'sources': result.excerpts,
            'retrieval': result.retrieval, 'material_sha256': hashlib.sha256(material(capture.calls[0]['messages']).encode()).hexdigest(),
            'messages': capture.calls[0]['messages'], 'provider_calls': 0}


def prepare(home, source, snapshots):
    import yaml
    from litprism.config import _from_dict, config_to_dict
    from litprism.llm import build_active_llm
    cases = ROOT / 'tools/answer_units_live_cases.json'
    mixed.prepare(home, source, snapshots, cases)
    spec = json.loads(cases.read_text(encoding='utf-8'))
    m = json.loads((home / 'manifest.json').read_text(encoding='utf-8'))
    cfg = _from_dict(yaml.safe_load((home / 'config/config.yaml').read_text(encoding='utf-8')))
    papers = {p['alias']: p['paper_id'] for p in m['papers']}
    frozen = {}
    for group in spec['groups']:
        folder = home / group['id']
        c, kb = paired.clone(folder, home / 'data/litprism.db', cfg, home)
        c.qa.evidence_anchor_trial = True
        try:
            for alias, pid in papers.items():
                if alias not in group['available']:
                    kb.update_paper(pid, verify_level='unchecked')
            real = build_active_llm(kb, c)
            case = group['cases'][0]
            frozen[group['id']] = freeze_question(kb, c, real, case['question'], [papers[a] for a in group['focus']])
            (folder / 'config').mkdir()
            (folder / 'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(c), allow_unicode=True), encoding='utf-8')
            mixed.write(folder / 'frozen-input.json', frozen[group['id']])
        finally:
            kb.close()
    m.update(code_sha256=fingerprints(), planned_calls=3, model_task_limits={'qa': 1}, search_mode='not_requested',
             authorization='User requested the proposed three first-attempt QA probes; fixed acceptance to completion',
             source_results_sha256=mixed.sha(source / 'qa-results.json'),
             frozen_input_sha256={g['id']: mixed.sha(home / g['id'] / 'frozen-input.json') for g in spec['groups']},
             policy='No auxiliary tasks/resends; input mismatch blocked before send; preserve all first failures',
             semantic_passed=None)
    mixed.write(home / 'manifest.json', m)
    (home / 'executed-tool.py').write_bytes(Path(__file__).read_bytes())
    (home / 'executed-paired-helper.py').write_bytes((ROOT / 'tools/anchor_live_acceptance.py').read_bytes())


def run(home):
    import yaml
    from fastapi.testclient import TestClient
    from litprism.config import _from_dict
    from litprism.db import KnowledgeBase
    from litprism.llm import OpenAICompatClient
    from litprism.web import server
    m = json.loads((home / 'manifest.json').read_text(encoding='utf-8'))
    if m['code_sha256'] != fingerprints() or mixed.sha(home / 'cases.json') != m['cases_sha256']:
        raise ValueError('Frozen code or cases changed')
    for group, digest in m['frozen_input_sha256'].items():
        if mixed.sha(home / group / 'frozen-input.json') != digest:
            raise ValueError('Frozen input changed')
    with (home / 'started.json').open('x', encoding='utf-8') as f:
        json.dump({'started': time.time(), 'never_repeat': True}, f)
    spec = json.loads((home / 'cases.json').read_text(encoding='utf-8'))
    original_kb, original_chat = server.KnowledgeBase, OpenAICompatClient.chat
    rows = []
    for group in spec['groups']:
        if m['code_sha256'] != fingerprints():
            raise RuntimeError('Implementation changed during acceptance')
        folder = home / group['id']
        frozen = json.loads((folder / 'frozen-input.json').read_text(encoding='utf-8'))
        cfg = _from_dict(yaml.safe_load((folder / 'config/config.yaml').read_text(encoding='utf-8')))
        kb, app = KnowledgeBase(cfg), None
        requests = []
        def capture(self, messages, **kwargs):
            validate_dispatch(kwargs.get('task'), messages, frozen, len(requests))
            if not getattr(self, 'durable_single_attempt', False):
                raise RuntimeError('Non-durable client blocked before dispatch')
            requests.append({'task': kwargs.get('task'), 'messages': copy.deepcopy(messages), 'single_attempt': True})
            mixed.write(folder / 'requests.json', requests)
            text = original_chat(self, messages, **kwargs)
            (folder / 'raw-completion.txt').write_text(text, encoding='utf-8')
            return text
        try:
            server.KnowledgeBase = lambda _: kb
            OpenAICompatClient.chat = capture
            app = server.create_app(cfg)
            client = TestClient(app)
            cid = kb.add_conversation('Units/fine live '+group['id'], scope={'paper_ids': frozen['paper_ids'], 'mode': 'focus'})
            case = group['cases'][0]
            print(json.dumps({'stage': 'start', 'id': case['id']}), flush=True)
            stamp = time.monotonic()
            response = client.post('/api/conversations/'+cid+'/chat-runs', json={
                'question': case['question'], 'request_id': 'units-fine-fixed-'+case['id'], 'web_search': False})
            response.raise_for_status()
            rid = response.json()['run_id']
            state = mixed.wait(client, rid, stamp)
            row = mixed.inspect_result(kb, client, cid, rid, case, state, time.monotonic()-stamp)
            saved = kb._con.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa' AND status='succeeded'", (rid,)).fetchone()
            value = json.loads(saved['response'])['value'] if saved else None
            audit = row['review'].get('evidence_audit', {})
            row.update(raw_value=value, measurements=paired.measurements(value, audit), request_count=len(requests))
            if value is not None:
                actual = row['review'].get('sources', [])
                same = len(actual) == len(frozen['sources']) and all(
                    all(a.get(k) == b.get(k) for k in ('tag', 'chunk_id', 'paper_id', 'material_id', 'page', 'text'))
                    for a, b in zip(actual, frozen['sources']))
                items = audit.get('items', [])
                row['mechanical_checks'] = {'frozen_sources_delivered': same,
                    'one_first_qa_only': len(requests) == state['usage']['calls'] == 1 and all(c['task'] == 'qa' for c in state['calls']),
                    'answer_units_observed': audit.get('answer_protocol') == 'answer_units_v1',
                    'all_answer_spans_bound': bool(items) and all(i.get('answer_binding', {}).get('state') == 'bound' for i in items),
                    'fine_anchors_observed': any(q.get('anchor_version') == 2 for i in items for q in i['quotes']),
                    'no_legacy_anchor_selections': not any(q.get('anchor_version') == 1 for i in items for q in i['quotes']),
                    'citations_resolve': bool(row['citation_audit']) and all(c['valid'] for c in row['citation_audit']),
                    'no_location_blockers': not audit.get('blocking_issues')}
            mixed.write(folder / 'result.json', row)
            rows.append(row)
            mixed.write(home / 'progress.json', {'rows': rows})
            print(json.dumps({'stage': 'done', 'id': case['id'], 'status': row['status'], 'seconds': row['elapsed_seconds'],
                              'calls': row['usage']['calls'], 'tokens': row['usage']['input']+row['usage']['output'],
                              'measurements': {k:v for k,v in (row['measurements'] or {}).items() if k in ('claims_proposed', 'anchors_resolved', 'displayed_quote_characters')},
                              'failed_checks': [k for k,v in row['mechanical_checks'].items() if not v]}), flush=True)
        finally:
            server.KnowledgeBase, OpenAICompatClient.chat = original_kb, original_chat
            if app:
                app.state.tasks.pool.shutdown(wait=True)
            kb.close()
    usage = {k: sum(r['usage'][k] for r in rows) for k in ('calls', 'input', 'output', 'unknown_usage_calls', 'failed_calls')}
    result = {'planned_calls': 3, 'observed_turns': len(rows), 'usage': usage, 'code_unchanged': fingerprints() == m['code_sha256'],
              'original_results_unchanged': mixed.sha(Path(m['source_root']) / 'qa-results.json') == m['source_results_sha256'],
              'daily_database_opened': False, 'semantic_passed': None, 'rows': rows}
    mixed.write(home / 'results.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'rows'}), flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--snapshots', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--live', action='store_true')
    args = parser.parse_args()
    home = args.output.resolve()
    if not home.name.startswith('_selftest.'):
        parser.error('Output must be isolated')
    os.environ['LITPRISM_HOME'] = str(home)
    if args.prepare:
        if not args.source_root or not args.snapshots:
            parser.error('Need existing isolated source and frozen snapshots')
        prepare(home, args.source_root.resolve(), args.snapshots.resolve())
        print('Three inputs frozen, zero provider calls.')
    else:
        run(home)
