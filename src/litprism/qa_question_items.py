"""Bind model-declared question items to displayed units, never infer coverage.

The model may omit required items or link irrelevant text. Those semantic and
question-completeness questions remain unknown even when all links are valid.
This diagnostic cannot promote knowledge, rewrite prose or change reply status.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .qa_answer import span


def record(question, data, answer, audited_units):
    result = {'version': 1, 'state': 'missing', 'origin': 'model_declared',
              'question_sha256': hashlib.sha256(question.encode()).hexdigest(),
              'answer_sha256': hashlib.sha256(answer.encode()).hexdigest(),
              'question_item_completeness': 'unknown', 'semantic_status': 'unreviewed',
              'items': [], 'issues': [], 'production_gate': False}
    if 'question_items' not in data:
        return result
    declared = data['question_items']
    if not isinstance(declared, list) or not declared:
        result.update(state='invalid', issues=['question_items_requires_nonempty_list'])
        return result
    ids = set()
    for item in declared:
        if (not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                                              for k in ('id', 'object', 'aspect'))):
            result.update(state='invalid', issues=['question_item_requires_id_object_aspect'])
            return result
        if item['id'] in ids:
            result.update(state='invalid', issues=['duplicate_question_item_id'])
            return result
        ids.add(item['id'])
        result['items'].append({k: item[k] for k in ('id', 'object', 'aspect')})
    units = data.get('answer_units')
    if not isinstance(units, list) or len(units) != len(audited_units):
        result.update(state='unavailable', issues=['answer_units_binding_required'])
        return result
    links = {key: [] for key in ids}
    for ordinal, (unit, audited) in enumerate(zip(units, audited_units), 1):
        binding = audited.get('answer_binding') or {}
        bound = binding.get('span') or {}
        start, end = bound.get('start'), bound.get('end')
        if (not isinstance(unit, dict) or binding.get('method') != 'answer_units'
                or binding.get('state') != 'bound' or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(answer)
                or not isinstance(unit.get('text'), str) or answer[start:end] != unit['text'].strip()
                or audited.get('claim') != answer[start:end] or bound != span(answer, start, end)):
            result['issues'].append(f'unit_{ordinal}_binding_mismatch')
            continue
        refs = unit.get('addresses', [])
        if not isinstance(refs, list) or any(not isinstance(key, str) for key in refs):
            result['issues'].append(f'unit_{ordinal}_addresses_requires_string_list')
            continue
        if len(set(refs)) != len(refs):
            result['issues'].append(f'unit_{ordinal}_duplicate_reference')
        for key in dict.fromkeys(refs):
            if key not in ids:
                result['issues'].append(f'unit_{ordinal}_unknown_reference')
                continue
            links[key].append({'ordinal': ordinal, 'answer_span': deepcopy(bound),
                               'basis': audited.get('basis', ''),
                               'source_record_state': audited.get('state', 'unknown')})
    for item in result['items']:
        selected = links[item['id']]
        gaps = sum(u['basis'] == 'not_located' for u in selected)
        item['units'] = selected
        item['state'] = ('unmapped' if not selected else 'gap_only' if gaps == len(selected)
                         else 'text_and_gap' if gaps else 'text_linked')
    result['state'] = 'invalid' if result['issues'] else 'recorded'
    return result
