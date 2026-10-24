"""Replay frozen counterexamples without model calls or rewriting previous results.

Controls refine ONLY independently locatable literal quotes. Legacy paragraph
anchor choices are not semantically narrowed. Structured answer-unit controls
test identity by construction, not a new model's adherence or quality.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from litprism.qa_answer import compile_answer  # noqa: E402
from litprism.qa_anchors import _paragraphs, build  # noqa: E402
from litprism.qa_claims import audit_map, locate  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refine_located_quotes(mapping, sources):
    """A scripted location control, never a repair of wrong source or semantics."""
    variant = copy.deepcopy(mapping)
    pool = {s['tag']: s for s in sources}
    controls = []
    for item in variant if isinstance(variant, list) else []:
        if not isinstance(item, dict) or item.get('basis') == 'metadata' or not isinstance(item.get('quotes'), list):
            continue
        quotes = []
        for q in item['quotes']:
            s = pool.get(q.get('source')) if isinstance(q, dict) else None
            found = locate(s['text'], q.get('quote')) if s and 'anchor_id' not in q else None
            if not found:
                quotes.append(q)
                continue
            chosen = [a for a in build(s) if a['start'] < found['end'] and a['end'] > found['start']]
            # Every original non-whitespace character must remain covered.
            covered = all(s['text'][p].isspace() or any(a['start'] <= p < a['end'] for a in chosen)
                          for p in range(found['start'], found['end']))
            if not covered or not chosen:
                quotes.append(q)
                continue
            coarse = [a for a in _paragraphs(s) if a['start'] < found['end'] and a['end'] > found['start']]
            controls.append({'source': s['tag'], 'literal_quote': found,
                             'coarse_characters': sum(len(a['quote']) for a in coarse),
                             'fine_characters': sum(len(a['quote']) for a in chosen),
                             'fine_anchors': [a['id'] for a in chosen]})
            quotes.extend({'source': s['tag'], 'anchor_id': a['id']} for a in chosen)
        item['quotes'] = quotes
    return variant, controls


def sources_for(root, group, row):
    db = root / group / row['arm'] / 'data/litprism.db'
    con = sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        sources = copy.deepcopy(row['review']['sources'])
        for s in sources:
            detail = row['source_details'].get(s['chunk_id'], {})
            s.update(acquired_at=detail.get('acquired_at'), url=detail.get('url'),
                     dependency_status=detail.get('dependency_status'))
            if s['source_kind'] != 'paper':
                source = con.execute('SELECT source_id,text FROM evidence_chunks WHERE id=?', (s['chunk_id'],)).fetchone()
                assert source is not None and source['text'] == s['text']
                s['source_id'] = source['source_id']
        return sources
    finally:
        con.close()


def run(source, output, prior=None):
    source, output = source.resolve(), output.resolve()
    if not source.name.startswith('_selftest.') or not output.name.startswith('_selftest.') or source == output:
        raise ValueError('Read a frozen isolated root and write a separate new isolated root')
    frozen = {p: sha(source / p) for p in ('manifest.json', 'cases.json', 'results.json', 'development-review.json')}
    data = json.loads((source / 'results.json').read_text(encoding='utf-8'))
    previous = json.loads((source / 'development-review.json').read_text(encoding='utf-8'))
    spec = json.loads((source / 'cases.json').read_text(encoding='utf-8'))
    groups = {c['id']: g['id'] for g in spec['groups'] for c in g['cases']}
    output.mkdir(parents=True, exist_ok=False)
    rows, unit_controls = [], []
    for row in data['rows']:
        sources = sources_for(source, groups[row['id']], row)
        answer, mapping, _, protocol = compile_answer(row['raw_value'])
        audit = audit_map(answer, mapping, sources, anchor_trial=row['arm'] == 'anchors')
        old = row['review']['evidence_audit']
        assert audit['blocking_issues'] == old['blocking_issues']
        # Historical selections keep the same exact bytes and offsets.
        for a, b in zip(audit['items'], old['items']):
            assert [(q['source'], q['quote'], q.get('start'), q.get('end')) for q in a['quotes']] == [
                (q['source'], q['quote'], q.get('start'), q.get('end')) for q in b['quotes']]
            if a['answer_binding']['state'] == 'bound':
                s = a['answer_binding']['span']
                assert answer[s['start']:s['end']] == s['text'] == a['claim'].strip()
                u = {k: copy.deepcopy(a[k]) for k in ('subject', 'basis', 'sources')}
                u['text'] = s['text']
                # Use the original proposal, not backfilled quote metadata.
                u['quotes'] = copy.deepcopy(mapping[int(a['id'].split('-')[-1])-1].get('quotes'))
                body, proposed, bindings, new_protocol = compile_answer({'answer_units': [u]})
                control = audit_map(body, proposed, sources, anchor_trial=row['arm'] == 'anchors',
                                    answer_bindings=bindings, answer_protocol=new_protocol)
                assert control['items'][0]['answer_binding']['method'] == 'answer_units'
                assert control['semantic_status'] == 'unreviewed'
                unit_controls.append({'case': row['id'], 'arm': row['arm'], 'claim': a['id'],
                    'method': 'scripted_single_unit_from_exact_existing_body_span_not_new_generation',
                    'answer': body, 'audit': control})
        variant, controls = refine_located_quotes(mapping, sources)
        fine_audit = audit_map(answer, variant, sources, anchor_trial=True)
        assert fine_audit['blocking_issues'] == audit['blocking_issues']
        rows.append({'case': row['id'], 'arm': row['arm'], 'original_answer': answer, 'original_protocol': protocol,
                     'raw_replay': audit, 'fine_scripted_control': fine_audit, 'controls': controls,
                     'original_unmatched_not_automatically_repaired': True,
                     'new_model_quality_sample': False})
    negative = None
    if prior:
        prior = prior.resolve()
        if not prior.name.startswith('_selftest.'):
            raise ValueError('Prior negative must also be isolated')
        before = sha(prior / 'qa-results.json')
        old = json.loads((prior / 'qa-results.json').read_text(encoding='utf-8'))
        x1 = next(r for r in old['rows'] if r['id'] == 'x1')
        con = sqlite3.connect((prior / 'data/litprism.db').as_uri()+'?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        try:
            value = json.loads(con.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa'", (x1['run_id'],)).fetchone()[0])['value']
            sources = [dict(con.execute('SELECT * FROM chunks WHERE id=?', (cid,)).fetchone(),
                            chunk_id=cid, tag=f'P{i}') for i, cid in enumerate(x1['retrieval']['delivered_chunk_ids'], 1)]
        finally:
            con.close()
        variant, _ = refine_located_quotes(value['evidence_map'], sources)
        negative = audit_map(value['answer'], variant, sources, anchor_trial=True)
        assert any('[P77]' in s for s in negative['blocking_issues']) and sha(prior / 'qa-results.json') == before
    assert all(sha(source / p) == value for p, value in frozen.items())
    controls = [c for r in rows for c in r['controls']]
    summary = {'model_calls': 0, 'new_generation': False, 'new_model_quality_sample': False,
               'semantic_passed': None, 'raw_responses_replayed': len(rows),
               'scripted_unit_controls': len(unit_controls), 'literal_quote_refinement_controls': len(controls),
               'coarse_control_characters': sum(c['coarse_characters'] for c in controls),
               'fine_control_characters': sum(c['fine_characters'] for c in controls),
               'unmatched_or_ambiguous': sum(i['answer_binding']['state'] != 'bound' for r in rows for i in r['raw_replay']['items']),
               'raw_quote_blockers_retained': sum(len(r['raw_replay']['blocking_issues']) for r in rows),
               'prior_P77_still_blocked': negative is not None,
               'prior_semantic_findings': [r for r in previous['reviews'] if r['key_point_judgment'] == 'needs_review'],
               'original_results_unchanged': True, 'daily_database_opened': False, 'source_sha256': frozen,
               'code_sha256': {str(p.relative_to(ROOT)): sha(p) for p in [*sorted((ROOT / 'src/litprism').rglob('*.py')), Path(__file__)]}}
    (output / 'comparison.json').write_text(json.dumps(dict(summary, rows=rows, answer_unit_controls=unit_controls,
        prior_negative=negative), ensure_ascii=False, indent=2), encoding='utf-8')
    return {k: v for k, v in summary.items() if k not in ('code_sha256', 'source_sha256', 'prior_semantic_findings')}


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior', type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.input, args.output, args.prior), ensure_ascii=False))
