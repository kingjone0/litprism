"""Hash-bound authored quote-coverage controls; never automatic entailment.

Reads frozen JSON only: no database, credentials, network or model calls. Witness
spans are explicitly chosen by the operator for these examples. Containment says
only that a chosen quote includes that span, not that arbitrary prose is true.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tools')]
from quote_generation_offline_acceptance import digest, read, write, sha, replay  # noqa: E402
from litprism.qa_claims import locate  # noqa: E402
from litprism.prompts import QA_SYSTEM, QA_QUOTE_CHECK  # noqa: E402


def witness(source, quote):
    if not quote or source['text'].count(quote) != 1:
        raise ValueError('Authored witness must name a unique literal source span')
    start = source['text'].index(quote)
    return {'source': source['tag'], 'text_sha256': digest(source['text']),
            'start': start, 'end': start+len(quote), 'quote': quote}


def annotated_coverage(unit, sources, witnesses):
    """Only check containment of operator-specified spans, never infer facets."""
    by_tag = {s['tag']: s for s in sources}
    quotes = unit.get('quotes')
    selected = []
    for q in quotes if isinstance(quotes, list) else []:
        source = by_tag.get(q.get('source')) if isinstance(q, dict) else None
        if source:
            match = locate(source['text'], q.get('quote'))
            if match: selected.append({'source': source['tag'], **match})
    rows = []
    for item in witnesses:
        source = by_tag.get(item['source'])
        if (not source or digest(source['text']) != item['text_sha256']
                or source['text'][item['start']:item['end']] != item['quote']):
            raise ValueError('Authored witness source/span changed')
        covered = any(q['source'] == item['source'] and q['start'] <= item['start']
                      and q['end'] >= item['end'] for q in selected)
        rows.append({'source': item['source'], 'start': item['start'], 'end': item['end'], 'covered': covered})
    return {'state': 'covered' if rows and all(r['covered'] for r in rows) else 'incomplete' if rows else 'not_assessed',
            'witnesses': rows, 'operator_annotated': True, 'semantic_status': 'unreviewed'}


def apply_control(value, control):
    index = control['unit_index']
    if type(index) is not int or not 0 <= index < len(value['answer_units']):
        raise ValueError('Invalid original unit index')
    if digest(value['answer_units'][index]) != control['original_unit_sha256']:
        raise ValueError('Original unit changed')
    replacement = control['replacement']
    if not isinstance(replacement, dict) or not replacement or set(replacement)-{'text', 'subject', 'basis', 'sources', 'quotes'}:
        raise ValueError('Control must explicitly replace answer-unit fields only')
    result = deepcopy(value)
    result['answer_units'][index].update(deepcopy(replacement))
    return result


def run(source, output, controls_file):
    source, output = source.resolve(), output.resolve()
    if (ROOT not in source.parents or ROOT not in output.parents or source == output
            or not source.name.startswith('_selftest.') or not output.name.startswith('_selftest.')):
        raise ValueError('Use distinct workspace _selftest roots')
    spec = read(controls_file)
    files = [controls_file, source/'manifest.json', source/'operator-assessment.json', source/'summary.json']
    for case in ('confirmed', 'revised', 'restricted'):
        files += [source/case/name for name in ('raw-qa.txt', 'parsed.json', 'frozen-request.json', 'result.json')]
    hashes = {str(p.resolve()): sha(p) for p in files}
    if spec['source_files_sha256'] != {str(p.resolve()): sha(p) for p in files if p != controls_file}:
        raise ValueError('Frozen real results changed')
    # Reject invalid controls before creating an output directory.
    results = []
    for control in spec['controls']:
        folder = source/control['case']
        original = read(folder/'parsed.json'); sources = read(folder/'frozen-request.json')['sources']
        variant = apply_control(original, control)
        index = control['unit_index']
        old_audit, new_audit = replay(original, sources), replay(variant, sources)
        if old_audit['blocking_issues'] or new_audit['blocking_issues']:
            raise ValueError('Expected valid original quotes and explicit located control')
        old_coverage = annotated_coverage(original['answer_units'][index], sources, control['original_witnesses'])
        new_coverage = annotated_coverage(variant['answer_units'][index], sources, control['variant_witnesses'])
        if old_coverage['state'] != 'incomplete' or new_coverage['state'] != 'covered':
            raise ValueError('Authored coverage expectation not met')
        results.append({'id': control['id'], 'case': control['case'], 'operator_note': control['note'],
                        'original_coverage': old_coverage, 'variant_coverage': new_coverage,
                        'original_audit': old_audit, 'variant_audit': new_audit, 'variant': variant,
                        'new_generation': False, 'user_approval': False, 'production_auto_repair': False})
    output.mkdir(exist_ok=False)
    for row in results: write(output/(row['id']+'.json'), row)
    unchanged = all(sha(Path(p)) == expected for p, expected in hashes.items())
    if not unchanged: raise ValueError('Frozen evidence changed during replay')
    summary = {'model_calls': 0, 'database_opened': False, 'original_files_unchanged': unchanged,
               'authored_controls': len(results), 'semantic_passed': None, 'coverage_inferred_from_prose': False,
               'note': 'Only operator-selected witness containment verified; not new model adherence or automatic semantics.',
               'source_files_sha256': hashes,
               'implementation_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in
                   (Path(__file__).resolve(), ROOT/'src/litprism/prompts.py', ROOT/'src/litprism/qa_claims.py')},
               'rows': [{k: r[k] for k in ('id', 'case', 'original_coverage', 'variant_coverage')} for r in results]}
    write(output/'summary.json', summary)
    write(output/'generation-contract.json', {'system': QA_SYSTEM, 'final_check': QA_QUOTE_CHECK,
                                             'not_a_dispatched_request': True})
    return {k: v for k, v in summary.items() if k not in ('source_files_sha256', 'implementation_sha256', 'rows')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--controls', type=Path, required=True)
    args = parser.parse_args()
    import json
    print(json.dumps(run(args.source, args.output, args.controls), ensure_ascii=True))
