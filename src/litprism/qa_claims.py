"""Locate model-proposed claim/quote pairs, without certifying entailment.

Whitespace and explicit PDF typography may differ. Source metadata comes from the
delivered inventory; the model's subject and basis remain review suggestions.
"""
from __future__ import annotations

import re

from .textutil import parse_citations


def _canonical(text):
    chars, positions = [], []
    ligatures = str.maketrans({'ﬁ':'fi','ﬂ':'fl','ﬀ':'ff','ﬃ':'ffi','ﬄ':'ffl'})
    for m in re.finditer(r'(?<=[A-Za-z])-[ \t]*\r?\n[ \t]*(?=[A-Za-z])|\u00ad|\s+|.', text, re.S):
        token = m.group()
        if token == '\u00ad' or token.startswith('-') and '\n' in token:
            continue
        value = ' ' if token.isspace() else token.translate(ligatures)
        for char in value:
            chars.append(char)
            positions.append((m.start(),m.end()))
    return ''.join(chars), positions


def locate(text, quote):
    if not isinstance(quote, str) or not quote.strip():
        return None
    match = re.search(r'\s+'.join(re.escape(w) for w in quote.split()), text)
    if match:
        return {'start': match.start(), 'end': match.end(), 'quote': match.group()}
    haystack, offsets = _canonical(text)
    needle = _canonical(quote)[0].strip()
    start = haystack.find(needle) if needle else -1
    if start < 0:
        return None
    a,b = offsets[start][0], offsets[start+len(needle)-1][1]
    return {'start':a,'end':b,'quote':text[a:b], 'normalization':'typography'}


def audit_map(answer, proposed, excerpts):
    """Missing/legacy maps are explicit unknowns, not semantic approval or refusal.

    Auxiliary structure errors warn; unverifiable proposed quotations hold the draft. A located
    quotation only proves its bytes occurred in a delivered source, never that
    the source discusses the claimed subject or supports the claim.
    """
    result = {'version': 1, 'state': 'missing', 'semantic_status': 'unreviewed',
              'items': [], 'issues': [], 'blocking_issues': [], 'warnings': []}
    if proposed is None or proposed == []:
        return result
    if not isinstance(proposed, list):
        result.update(state='invalid', issues=['陈述来源对应记录不是数组，请人工核对草稿。'])
        result['warnings'] = result['issues'][:]
        return result
    pool = {e['tag']: e for e in excerpts}
    used = {tag for tag, _ in parse_citations(answer)}
    for pos, raw in enumerate(proposed, 1):
        item = {'id': f'claim-{pos}', 'claim': '', 'subject': '', 'basis': '',
                'sources': [], 'quotes': [], 'issues': [], 'state': 'unlocated'}
        result['items'].append(item)
        issues = item['issues']
        if not isinstance(raw, dict):
            issues.append('陈述记录不是对象。')
            continue
        claim = raw.get('claim')
        if isinstance(claim, str):
            item['claim'] = claim
        extended = any(k in raw for k in ('subject', 'basis', 'quotes'))
        if extended and not locate(answer, claim):
            issues.append('陈述未对应草稿中的原句。')
        tags = raw.get('sources')
        if not isinstance(tags, list) or any(not isinstance(t, str) or t not in pool for t in tags):
            issues.append('来源编号不属于本轮实际送达来源。')
            tags = []
        item['sources'] = list(dict.fromkeys(tags))
        if any(t not in used for t in tags):
            issues.append('对应来源未在草稿中引用。')
        local_tags = {t for t, _ in parse_citations(item['claim'])}
        if local_tags and local_tags != set(tags):
            issues.append('陈述原句的编号与对应记录不一致。')
        if not extended:
            item['state'] = 'legacy'
            continue
        subject, basis = raw.get('subject'), raw.get('basis')
        if not isinstance(subject, str) or not subject.strip():
            issues.append('缺少所谈对象。')
        else:
            item['subject'] = subject
        if basis not in ('direct', 'inference', 'not_located'):
            issues.append('依据类型必须为直接描述、推断或尚未定位。')
        else:
            item['basis'] = basis
        quotes = raw.get('quotes')
        if not isinstance(quotes, list):
            issues.append('原句记录不是数组。')
            quotes = []
        quoted = set()
        for q in quotes:
            if not isinstance(q, dict) or not isinstance(q.get('source'), str) or q['source'] not in tags:
                issues.append('原句引用了未绑定的来源。')
                continue
            tag = q['source']
            found = locate(pool[tag]['text'], q.get('quote'))
            if not found:
                issues.append(f'[{tag}] 中无法定位所给原句（仅允许空白及明确排版等价）。')
                continue
            # Ignore model-supplied title/page/chunk/trust/verification fields.
            item['quotes'].append({'source': tag, **found})
            quoted.add(tag)
        if basis == 'not_located':
            if tags or quotes:
                issues.append('尚未定位的记录不能同时声称已有支持原句。')
        elif not tags or quoted != set(tags):
            issues.append('每处支持来源须绑定可定位原句。')
        item['state'] = 'not_located' if basis == 'not_located' else 'located'
    for item in result['items']:
        if item['issues']:
            item['state'] = 'invalid'
            result['issues'].extend(f"{item['id']}：{s}" for s in dict.fromkeys(item['issues']))
        item['blocking_issues'] = [s for s in item['issues'] if '中无法定位所给原句' in s]
        item['warnings'] = [s for s in item['issues'] if s not in item['blocking_issues']]
        result['blocking_issues'].extend(f"{item['id']}：{s}" for s in item['blocking_issues'])
        if any(q.get('normalization') for q in item['quotes']):
            item['warnings'].append('原句按明确排版等价定位；展示保留原始字符。')
        result['warnings'].extend(f"{item['id']}：{s}" for s in item['warnings'])
    result['state'] = ('invalid' if result['issues'] else
                       'legacy' if any(i['state'] == 'legacy' for i in result['items']) else 'recorded')
    return result
