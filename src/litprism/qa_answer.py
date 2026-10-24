"""One displayed answer, with server-owned offsets; never fuzzy semantic matching."""
from __future__ import annotations

from copy import deepcopy
import hashlib


def span(answer, start, end):
    bound = f'{hashlib.sha256(answer.encode()).hexdigest()}:{start}:{end}'
    return {'id': 'ans_'+hashlib.sha256(bound.encode()).hexdigest()[:24],
            'start': start, 'end': end, 'text': answer[start:end]}


def compile_answer(data):
    """New units render text and mapping together. Legacy output remains readable.

    The saved model response is not changed. Model-supplied offsets/IDs/claim
    copies are ignored. Conflicting parallel answers never choose a silent winner.
    """
    if 'answer_units' not in data:
        answer = data.get('answer')
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('模型没有返回非空回答文本。')
        return answer.strip(), data.get('evidence_map'), None, 'legacy_answer_v1'
    units = data['answer_units']
    if not isinstance(units, list) or not units or any(
        not isinstance(u, dict) or not isinstance(u.get('text'), str) or not u['text'].strip() for u in units
    ):
        raise ValueError('回答单元必须是非空数组，每项包含非空text。')
    texts = [u['text'].strip() for u in units]
    answer = '\n\n'.join(texts)
    if ('answer' in data and data['answer'] != answer) or 'evidence_map' in data:
        raise ValueError('回答单元与独立正文/陈述记录并存，请只返回一份回答。')
    records, bindings = [], []
    start = 0
    for ordinal, (u, text) in enumerate(zip(units, texts), 1):
        records.append({'claim': text, **{k: deepcopy(u.get(k)) for k in ('subject', 'basis', 'sources', 'quotes')}})
        bindings.append({'state': 'bound', 'method': 'answer_units', 'ordinal': ordinal,
                         'span': span(answer, start, start+len(text))})
        start += len(text)+2
    return answer, records, bindings, 'answer_units_v1'


def bind(answer, claim):
    """Literal identity only. Repeated occurrences remain ambiguous, not first-hit."""
    if not isinstance(claim, str) or not claim.strip():
        return {'state': 'unmatched'}
    needle = claim.strip()
    start = answer.find(needle)
    if start < 0:
        return {'state': 'unmatched', 'occurrences': 0}
    if answer.find(needle, start+1) >= 0:
        return {'state': 'ambiguous', 'occurrences_at_least': 2}
    return {'state': 'bound', 'method': 'literal', 'span': span(answer, start, start+len(needle))}
