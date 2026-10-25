"""Offline retrieval comparison over frozen cases; never grades generated answers.

The baseline directory contains explicitly frozen repository Python sources.
All corpus opens use a disposable copy; no provider or secret store is used.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
from types import ModuleType
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def isolated(path):
    path = Path(path).resolve()
    if ROOT not in path.parents or not path.name.startswith('_selftest.'):
        raise ValueError('Use an isolated _selftest root inside this workspace')
    return path


def load_baseline(directory, name):
    module = ModuleType('litprism._retrieval_baseline_' + name)
    module.__package__ = 'litprism'
    path = Path(directory) / (name + '.py')
    exec(compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec'), module.__dict__)
    return module


class Capture:
    # No per-case paid expansion exists for the original scoped 24 questions.
    name = 'mock:offline-retrieval-capture'

    def __init__(self):
        self.messages = None

    def chat_json(self, messages, *, task=None):
        if task != 'qa' or self.messages is not None:
            raise AssertionError('Unexpected offline task or duplicate capture')
        self.messages = deepcopy(messages)
        return {'answer': '', 'insufficient_evidence': True}


def inspect_delivery(kb, cfg, result, model, *, scope, expected_papers, witnesses, external=()):
    excerpts = result.excerpts
    delivered = {e['chunk_id'] for e in excerpts}
    paper_ids = {e['paper_id'] for e in excerpts if e.get('paper_id')}
    from litprism.context.meter import Meter
    meter = Meter(cfg)
    estimate = sum(meter.estimate_message(m) for m in model.messages) + 8 if model.messages else 0
    allowed = {r['id'] for r in kb._con.execute(
        "SELECT id FROM papers WHERE deleted_at IS NULL AND verify_level IN ('verified','manual_confirmed')")}
    current = {c['id'] for pid in allowed for c in kb.get_chunks(pid)}
    full = result.retrieval.get('full_material_paper_ids', [])
    expected_papers = set(expected_papers)
    return {'mode': result.retrieval['mode'], 'delivered_chunks': len(excerpts),
            'chunk_ids': [e['chunk_id'] for e in excerpts], 'paper_ids': sorted(paper_ids),
            'expected_paper_ids': sorted(expected_papers),
            'expected_papers_missing': sorted(expected_papers - paper_ids),
            'additional_paper_ids_not_automatically_irrelevant': sorted(paper_ids - expected_papers),
            'additional_paper_characters': sum(len(e['text']) for e in excerpts if e.get('paper_id') and e['paper_id'] not in expected_papers),
            'witnesses_delivered': sorted(set(witnesses) & delivered),
            'witnesses_missing': sorted(set(witnesses) - delivered),
            'strict_scope_ok': scope is None or paper_ids <= set(scope),
            'current_trusted_materials_only': all(e['chunk_id'] in current for e in excerpts if e.get('paper_id')),
            'external_preserved': {e['id'] for e in external} <= delivered,
            'full_material_record_correct': all({c['id'] for c in kb.get_chunks(pid)} <= delivered for pid in full),
            'request_within_window': estimate <= cfg.context.available_tokens,
            'estimated_input_tokens': estimate, 'available_input_tokens': cfg.context.available_tokens,
            'full_material_paper_ids': full, 'window_limited': result.retrieval.get('window_limited', False),
            'restored_chunk_ids': result.retrieval.get('restored_chunk_ids', []),
            'answer_quality': 'not_evaluated', 'refusal_quality': 'not_evaluated'}


def compare(before, after):
    return {'new_missing_witnesses': sorted(set(after['witnesses_missing']) - set(before['witnesses_missing'])),
            'recovered_witnesses': sorted(set(before['witnesses_missing']) - set(after['witnesses_missing'])),
            'new_missing_papers': sorted(set(after['expected_papers_missing']) - set(before['expected_papers_missing'])),
            'estimated_input_delta': after['estimated_input_tokens'] - before['estimated_input_tokens']}


def run(source, output, baseline, expectations):
    import yaml
    from litprism.config import _from_dict
    from litprism.db import KnowledgeBase
    from litprism.agents import QAAgent
    from litprism import qa_retrieval, search

    source, output, baseline = isolated(source), isolated(output), isolated(baseline)
    if output.exists() or output in (source, baseline) or source in output.parents or baseline in output.parents or output in source.parents or output in baseline.parents:
        raise ValueError('Output must be a new independent directory')
    paths = [source / n for n in ('manifest.json', 'cases.json', 'qa-results.json', 'semantic-audit.json', 'config/config.yaml')]
    paths += [baseline / n for n in ('query.py', 'qa_retrieval.py', 'provenance.json')]
    paths += [Path(expectations).resolve(), Path(__file__).resolve()]
    original_hashes = {str(p): sha(p) for p in paths}
    spec, historical, manifest, authored = read(paths[1]), read(paths[2]), read(paths[0]), read(expectations)
    if len(spec['cases']) != 24 or len({c['id'] for c in spec['cases']}) != 24:
        raise ValueError('Expected the unique original 24 cases')
    if sha(source / 'cases.json') != manifest['cases_sha256']:
        raise ValueError('Original cases hash differs')
    aliases = {p['alias']: p['paper_id'] for p in manifest['papers']}
    previous = {r['id']: r for r in historical['rows']}
    baseline_query = load_baseline(baseline, 'query')
    baseline_retrieval = load_baseline(baseline, 'qa_retrieval')
    output.mkdir()
    (output / 'data').mkdir()
    src = sqlite3.connect((source / 'data/litprism.db').as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(output / 'data/litprism.db')
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    cfg = _from_dict(yaml.safe_load((source / 'config/config.yaml').read_text(encoding='utf-8')))
    cfg.app.data_dir, cfg.export.notes_dir = str(output / 'data'), str(output / 'notes')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = manifest['window_tokens']
    cfg.llm.model_max_output_tokens = cfg.llm.max_tokens = cfg.context.output_reserve_tokens = manifest['max_output_tokens']
    cfg.llm.automatic_output = True
    cfg.context.overflow_retries = 0
    cfg.qa.evidence_anchor_trial = False
    implementation = {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / 'src').rglob('*.py')}
    write(output / 'manifest.json', {'source_files_sha256': original_hashes, 'implementation_sha256': implementation,
                                    'baseline_provenance': read(baseline / 'provenance.json'),
                                    'expansion_mode': 'deterministic_only_no_per_case_model_expansion_record',
                                    'real_model_calls': 0, 'real_search_calls': 0, 'semantic_passed': None})

    def no_network(*args, **kwargs):
        raise AssertionError('Network forbidden in offline retrieval matrix')

    rows, boundaries = [], []
    with patch.object(socket.socket, 'connect', no_network):
        kb = KnowledgeBase(cfg)
        try:
            for paper in manifest['papers']:
                chunks = kb.get_chunks(paper['paper_id'])
                digest = hashlib.sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                if digest != paper['source_hash']:
                    raise ValueError('Frozen indexed corpus differs')
            all_current = {c['id'] for pid in aliases.values() for c in kb.get_chunks(pid)}
            if not all(set(v) <= all_current for v in authored['witnesses'].values()):
                raise ValueError('Authored witness is not a current indexed chunk')

            def evaluate(case, variant, scope, *, context_cfg=None, external=()):
                local = context_cfg or cfg
                old = previous[case['id']]
                request = kb._con.execute('SELECT user_message FROM chat_runs WHERE id=?', (old['run_id'],)).fetchone()
                user = json.loads(request[0])
                expected = [aliases[a] for a in case.get('expect_aliases', [])]
                if case.get('kind') == 'cross':
                    expected = scope or list(aliases.values()) if variant == 'original' else [aliases[a] for a in case.get('scope_aliases', aliases)]
                witnesses = authored['witnesses'].get(case['id'], [])
                arms = {}
                for arm in ('before', 'after'):
                    model = Capture()
                    with ExitStack() as stack:
                        if arm == 'before':
                            stack.enter_context(patch.object(qa_retrieval, 'retrieve', baseline_retrieval.retrieve))
                            stack.enter_context(patch.object(search, 'expand_query', baseline_query.expand_query))
                        result = QAAgent(kb, local, model).ask(case['question'], paper_ids=scope,
                            conversation_id=old['conversation_id'], before_seq=user['seq'],
                            strict_scope=scope is not None, external=external)
                    arms[arm] = inspect_delivery(kb, local, result, model, scope=scope,
                                                expected_papers=expected, witnesses=witnesses, external=external)
                row = {'id': case['id'], 'variant': variant, 'question': case['question'],
                       'scope': scope, 'kind': case['kind'], **arms, 'delta': compare(arms['before'], arms['after'])}
                rows.append(row)
                write(output / 'rows.json', rows)
                return row

            for case in spec['cases']:
                scope = previous[case['id']]['scope']
                evaluate(case, 'original', scope)
                # The variants change only UI scope. Explicit source exclusions
                # and the f4 scope-switch instruction keep their original scope.
                if scope is not None and case['kind'] != 'refuse' and case['id'] != 'f4':
                    evaluate(case, 'default', None)

            from litprism.scope import chat_paper_scope, paper_scope
            for value in ({}, {'paper_ids': []}):
                assert chat_paper_scope(kb, value) is None
            assert paper_scope(kb, {'paper_ids': []}) == []
            try:
                chat_paper_scope(kb, {'paper_ids': ['missing-paper']})
            except ValueError:
                boundaries.append({'control': 'invalid_selection', 'passed': True})
            else:
                raise AssertionError('Invalid selection became unrestricted')
            boundaries.append({'control': 'web_empty_unrestricted_internal_empty_preserved', 'passed': True})

            # Budget controls use the real corpus but deliberately smaller
            # test-only windows; no persistent user/model configuration changes.
            for case_id in ('b1', 'x4'):
                case = next(c for c in spec['cases'] if c['id'] == case_id)
                limited = deepcopy(cfg)
                limited.llm.model_window_tokens = limited.context.window_tokens = 16000
                limited.llm.max_tokens = limited.llm.model_max_output_tokens = limited.context.output_reserve_tokens = 4096
                evaluate(case, 'limited_window', None, context_cfg=limited)
        finally:
            kb.close()
    summary = {'original_cases': sum(r['variant'] == 'original' for r in rows),
               'default_scope_variants': sum(r['variant'] == 'default' for r in rows),
               'limited_window_controls': sum(r['variant'] == 'limited_window' for r in rows),
               'rows': len(rows), 'new_missing_witness_rows': [r['id'] + ':' + r['variant'] for r in rows if r['delta']['new_missing_witnesses']],
               'after_missing_witness_rows': [r['id'] + ':' + r['variant'] for r in rows if r['after']['witnesses_missing']],
               'original_scope_or_material_failures': [r['id'] for r in rows if r['variant'] == 'original' and not (
                   r['after']['strict_scope_ok'] and r['after']['current_trusted_materials_only'])],
               'within_window': all(r['after']['request_within_window'] for r in rows),
               'boundaries': boundaries, 'real_model_calls': 0, 'real_search_calls': 0,
               'semantic_passed': None, 'answer_quality': 'not_evaluated',
               'original_inputs_unchanged': all(sha(Path(p)) == h for p, h in original_hashes.items())}
    write(output / 'summary.json', summary)
    assert summary['original_inputs_unchanged']
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'baseline', 'expectations'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output, args.baseline, args.expectations), ensure_ascii=True))
