"""Zero-call comparison over frozen QA responses; never a new semantic quality sample.

Only independently locatable existing quotes may be converted to anchors. Unknown
quotes retain the original strict check; no nearest-source repair is performed.
An additional scripted same-source selection control measures backfill mechanics,
not whether a real model would select correct anchors.
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

from litprism.qa_anchors import build  # noqa: E402
from litprism.qa_claims import audit_map, locate  # noqa: E402
from litprism.qa_evidence import material_risks  # noqa: E402


def anchored_variant(mapping, sources):
    result = copy.deepcopy(mapping)
    pool = {e['tag']: e for e in sources}
    converted = 0
    if not isinstance(result, list):
        return result, converted
    for item in result:
        if not isinstance(item, dict) or not isinstance(item.get('quotes'), list) or item.get('basis') == 'metadata':
            continue
        for i, quote in enumerate(item['quotes']):
            e = pool.get(quote.get('source')) if isinstance(quote, dict) else None
            if not e:
                continue
            found = locate(e['text'], quote.get('quote'))
            if not found:
                continue
            anchor = next((a for a in build(e) if a['start'] <= found['start'] and a['end'] >= found['end']), None)
            if anchor:
                item['quotes'][i] = {'source': e['tag'], 'anchor_id': anchor['id']}
                converted += 1
    return result, converted


def compare(answer, mapping, sources):
    old = audit_map(answer, mapping, sources)
    variant, converted = anchored_variant(mapping, sources)
    trial = audit_map(answer, variant, sources, anchor_trial=True)
    # New original bytes must not hide an existing unlocated/incorrect quotation.
    assert set(old['blocking_issues']) == set(trial['blocking_issues'])
    return {'quote_path': old, 'anchor_path': trial, 'converted_located_quotes': converted,
            'semantic_status': 'unreviewed', 'new_generation': False}


def run(source, output, prior=None):
    source, output = source.resolve(), output.resolve()
    if not source.name.startswith('_selftest.') or not output.name.startswith('_selftest.'):
        raise ValueError('Use frozen isolated _selftest.* input and a new isolated output')
    raw = source / 'results.json'
    before = raw.read_bytes()
    data = json.loads(before)
    spec = json.loads((source / 'cases.json').read_text(encoding='utf-8'))
    groups = {c['id']: g['id'] for g in spec['groups'] for c in g['cases']}
    output.mkdir(parents=True, exist_ok=False)
    rows, controls = [], []
    for row in data['rows']:
        if not row.get('review'):
            continue
        conn = sqlite3.connect((source / groups[row['id']] / 'data/litprism.db').as_uri() + '?mode=ro', uri=True)
        try:
            value = json.loads(conn.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa'", (row['run_id'],)).fetchone()[0])['value']
        finally:
            conn.close()
        sources = []
        for e in row['review']['sources']:
            details = row['source_details'].get(e['chunk_id'], {})
            sources.append(dict(e, acquired_at=details.get('acquired_at'), url=details.get('url'),
                                dependency_status=details.get('dependency_status')))
        result = compare(value['answer'], value.get('evidence_map'), sources)
        result.update(id=row['id'], original_status=row['status'],
                      material_guard_now=material_risks(row['draft'], sources, row['retrieval']['evidence_manifest']))
        if row['id'] == 'a1':
            assert row['guard_issues'] and not result['material_guard_now']
        if row['id'] == 'v1':
            # Explicitly scripted selection of the SAME P9 source. This does not
            # predict a future model choice and never rewrites its original quote.
            e = next(e for e in sources if e['tag'] == 'P9')
            claim = next(c for c in value['evidence_map'] if c['sources'] == ['P9'])
            p = [dict(claim, quotes=[{'source': 'P9', 'anchor_id': build(e)[0]['id']}])]
            control = audit_map(value['answer'], p, sources, anchor_trial=True)
            assert not control['blocking_issues'] and control['semantic_status'] == 'unreviewed'
            controls.append({'case': 'v1', 'method': 'scripted_same_source_selection_not_new_model_output',
                             'audit': control, 'original_quote_errors_retained': result['quote_path']['blocking_issues']})
        rows.append(result)
    prior_proof = None
    if prior:
        prior = prior.resolve()
        if not prior.name.startswith('_selftest.'):
            raise ValueError('Prior source must also be isolated')
        old_raw = (prior / 'qa-results.json').read_bytes()
        x1 = next(r for r in json.loads(old_raw)['rows'] if r['id'] == 'x1')
        conn = sqlite3.connect((prior / 'data/litprism.db').as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            value = json.loads(conn.execute("SELECT response FROM chat_run_calls WHERE run_id=? AND task='qa'", (x1['run_id'],)).fetchone()[0])['value']
            sources = [dict(conn.execute('SELECT * FROM chunks WHERE id=?', (cid,)).fetchone(), chunk_id=cid, tag=f'P{i}')
                       for i, cid in enumerate(x1['retrieval']['delivered_chunk_ids'], 1)]
        finally:
            conn.close()
        prior_proof = compare(value['answer'], value.get('evidence_map'), sources)
        assert any('[P77]' in s for s in prior_proof['anchor_path']['blocking_issues'])
        assert (prior / 'qa-results.json').read_bytes() == old_raw
    assert raw.read_bytes() == before
    result = {'model_calls': 0, 'original_results_unchanged': True, 'new_model_quality_sample': False,
              'semantic_passed': None, 'scope': 'source location/backfill and material-subject checks only',
              'rows': rows, 'scripted_controls': controls, 'prior_x1': prior_proof,
              'source_result_sha256': hashlib.sha256(before).hexdigest(),
              'code_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in [*sorted((ROOT / 'src/litprism').rglob('*.py')), Path(__file__)]}}
    (output / 'comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior', type=Path)
    args = parser.parse_args()
    result = run(args.input, args.output, args.prior)
    print(json.dumps({'responses_compared': len(result['rows']), 'located_quotes_converted': sum(
        r['converted_located_quotes'] for r in result['rows']), 'original_results_unchanged': True,
        'model_calls': 0, 'semantic_passed': None}))
