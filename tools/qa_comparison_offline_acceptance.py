"""Hash-bound, operator-authored comparison controls; not an entailment judge.

Reads frozen JSON only. Dimension membership and attribution labels are authored
annotations, never inferred from keywords or used as production response gates.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from quote_coverage_offline_acceptance import annotated_coverage  # noqa: E402
from quote_generation_offline_acceptance import digest, read, write, sha, replay  # noqa: E402
from litprism.context.meter import Meter  # noqa: E402
from litprism.prompts import QA_SYSTEM, QA_QUOTE_CHECK  # noqa: E402


def bound_unit(units, unit_hash):
    matches = [u for u in units if digest(u) == unit_hash]
    if len(matches) != 1:
        raise ValueError('Authored unit binding changed or is ambiguous')
    return matches[0]


def assess(value, sources, slots, attributions=()):
    """Verify explicit bindings and witness containment, not prose meaning."""
    units = value['answer_units']
    rows, ids = [], set()
    for slot in slots:
        if not slot['id'] or slot['id'] in ids:
            raise ValueError('Authored slots require unique IDs')
        ids.add(slot['id'])
        selected = [bound_unit(units, h) for h in slot['unit_sha256']]
        quotes = [q for u in selected for q in u.get('quotes', [])]
        coverage = annotated_coverage({'quotes': quotes}, sources, slot['witnesses'])
        if not selected:
            state = 'unanswered'
        elif all(u.get('basis') == 'not_located' for u in selected):
            state = 'not_located'
        elif coverage['state'] == 'covered':
            state = 'covered'
        else:
            state = coverage['state']
        rows.append({'id': slot['id'], 'object': slot['object'], 'dimension': slot['dimension'],
                     'state': state, 'answer_present': bool(selected), 'witness_coverage': coverage,
                     'operator_assigned_dimension': True})
    judgments = []
    for item in attributions:
        unit = bound_unit(units, item['unit_sha256'])
        by_tag = {s['tag']: s for s in sources}
        source_hashes = {tag: digest(by_tag[tag]['text']) for tag in unit['sources'] if tag in by_tag}
        if set(source_hashes) != set(unit['sources']) or item.get('source_text_sha256') != source_hashes:
            raise ValueError('Authored attribution source changed')
        if item['label'] not in ('supported_wording', 'needs_narrower_wording', 'unreviewed'):
            raise ValueError('Unsupported authored attribution label')
        judgments.append({**item, 'operator_authored': True, 'automatically_inferred': False})
    state = ('covered' if rows and all(r['state'] == 'covered' for r in rows)
             else 'incomplete' if any(r['state'] in ('unanswered', 'not_located', 'incomplete') for r in rows)
             else 'not_assessed')
    return {'state': state, 'slots': rows, 'attribution_annotations': judgments,
            'attribution_state': 'operator_annotated' if judgments else 'not_assessed',
            'semantic_status': 'unreviewed', 'production_gate': False}


def apply_control(value, control):
    if digest(value) != control['original_answer_sha256']:
        raise ValueError('Frozen answer changed')
    operation = control['operation']
    index, unit = operation['unit_index'], operation['unit']
    if (type(index) is not int or not isinstance(unit, dict)
            or set(unit) != {'text', 'subject', 'basis', 'sources', 'quotes'}):
        raise ValueError('Invalid authored operation')
    result = deepcopy(value)
    units = result['answer_units']
    if operation['kind'] == 'insert' and 0 <= index <= len(units):
        units.insert(index, deepcopy(unit))
    elif operation['kind'] == 'replace' and 0 <= index < len(units):
        if digest(units[index]) != operation['original_unit_sha256']:
            raise ValueError('Frozen replacement unit changed')
        units[index] = deepcopy(unit)
    else:
        raise ValueError('Invalid authored operation/index')
    return result


def prepare_request(messages, before):
    if (not before['system'] or not before['final_check'] or len(messages) != 2
            or [m['role'] for m in messages] != ['system', 'user']
            or not messages[0]['content'].startswith(before['system'])
            or not messages[1]['content'].endswith(before['final_check'])):
        raise ValueError('Frozen generation boundary differs; do not guess')
    updated = deepcopy(messages)
    updated[0]['content'] = QA_SYSTEM + messages[0]['content'][len(before['system']):]
    updated[1]['content'] = messages[1]['content'][:-len(before['final_check'])] + QA_QUOTE_CHECK
    return updated


def validate_paths(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if (any(ROOT not in p.parents or not p.name.startswith('_selftest.') for p in (source, output))
            or source == output or source in output.parents or output in source.parents or output.exists()):
        raise ValueError('Use new, independent workspace _selftest output')
    return source, output


def run(source, output, controls_file):
    source, output = validate_paths(source, output)
    controls_file = Path(controls_file).resolve()
    spec = read(controls_file)
    files = [source / n for n in ('manifest.json', 'operator-assessment.json', 'summary.json')]
    for case in ('x1', 'x2'):
        files += [source / case / n for n in ('raw-qa.txt', 'requests.json', 'result.json')]
    hashes = {str(p): sha(p) for p in files}
    if spec['source_files_sha256'] != hashes:
        raise ValueError('Frozen real inputs changed')
    hashes[str(controls_file)] = sha(controls_file)
    results = []
    for control in spec['controls']:
        if control['case'] not in ('x1', 'x2'):
            raise ValueError('Unknown frozen case')
        folder = source / control['case']
        value, sources = read(folder / 'raw-qa.txt'), read(folder / 'result.json')['review']['sources']
        variant = apply_control(value, control)
        old_audit, new_audit = replay(value, sources), replay(variant, sources)
        if old_audit['blocking_issues'] or new_audit['blocking_issues']:
            raise ValueError('Unlocated source in authored comparison control')
        old = assess(value, sources, control['original_slots'], control.get('original_attributions', []))
        new = assess(variant, sources, control['variant_slots'], control.get('variant_attributions', []))
        if old['state'] != control['expected_original_state'] or new['state'] != control['expected_variant_state']:
            raise ValueError('Authored comparison expectation differs')
        results.append({'id': control['id'], 'case': control['case'], 'note': control['note'],
                        'original': old, 'variant_assessment': new, 'variant': variant,
                        'original_location_audit': old_audit, 'variant_location_audit': new_audit,
                        'new_generation': False, 'user_approval': False, 'production_auto_repair': False})
    prepared = []
    for case in ('x1', 'x2'):
        requests = read(source / case / 'requests.json')
        if len(requests) != 1 or requests[0]['task'] != 'qa':
            raise ValueError('Expected exactly one frozen QA')
        messages = requests[0]['messages']
        updated = prepare_request(messages, spec['generation_before'])
        meter = Meter()
        settings = next(c['settings'] for c in read(source / case / 'result.json')['calls'] if c['task'] == 'qa')
        available = settings['model_window_tokens'] - settings['max_tokens']
        estimate = sum(meter.estimate_message(m) for m in updated) + 8
        if estimate > available:
            raise ValueError('Updated frozen request does not fit captured model limits')
        prepared.append({'case': case, 'messages': updated, 'not_dispatched': True,
                         'original_estimated_tokens': sum(meter.estimate_message(m) for m in messages) + 8,
                         'updated_estimated_tokens': estimate, 'available_input_tokens': available})
    output.mkdir()
    for row in results:
        write(output / (row['id'] + '.json'), row)
    for row in prepared:
        write(output / (row['case'] + '-prepared-request.json'), row)
    assert all(sha(Path(p)) == h for p, h in hashes.items())
    summary = {'authored_controls': len(results), 'original_files_unchanged': True,
               'model_calls': 0, 'search_calls': 0, 'database_opened': False, 'credential_access': False,
               'comparison_dimensions_inferred': False, 'attribution_automatically_judged': False,
               'semantic_passed': None, 'model_adherence_verified': False, 'source_files_sha256': hashes,
               'implementation_sha256': {str(p.relative_to(ROOT)): sha(p) for p in
                   (Path(__file__).resolve(), ROOT / 'src/litprism/prompts.py', ROOT / 'src/litprism/qa_claims.py')},
               'rows': [{k: r[k] for k in ('id', 'original', 'variant_assessment')} for r in results],
               'prepared_requests': [{k: r[k] for k in ('case', 'original_estimated_tokens', 'updated_estimated_tokens', 'available_input_tokens', 'not_dispatched')} for r in prepared]}
    write(output / 'summary.json', summary)
    return {k: v for k, v in summary.items() if k not in ('source_files_sha256', 'implementation_sha256', 'rows')}


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'controls'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output, args.controls), ensure_ascii=True))
