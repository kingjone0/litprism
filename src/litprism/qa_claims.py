"""Locate model-proposed claim/quote pairs, without certifying entailment.

Whitespace and explicit PDF typography may differ. Source metadata comes from the
delivered inventory; the model's subject and basis remain review suggestions.
"""
from __future__ import annotations

import re

from .textutil import parse_citations


_LOCATION_NOTICES = frozenset({
    '原句按明确排版等价定位；展示保留原始字符。',
    '匹配程序记录，仅用于来源、获取时间或版本说明，不是论文原句或语义支持证明。',
    '试验锚点原文由程序回填；位置有效不代表对象归属或语义支持已通过。',
})


def presentation_warnings(messages):
    """Separate known explanations from defects, including saved legacy messages.

    This is a display projection only: immutable audits and blocking checks stay
    intact. Never classify an unknown warning as harmless by keyword matching.
    """
    warnings, notices = [], []
    for message in messages:
        text = re.sub(r'^claim-\d+：', '', message)
        (notices if text in _LOCATION_NOTICES else warnings).append(message)
    return warnings, notices


def _canonical(text, *, compound_hyphens=False):
    chars, positions = [], []
    # Recover a narrow PDF compound-word discrepancy in a long unique quote.
    # Do not collapse numbers, identifiers or ambiguous short prefixes (re-sign).
    missing = {m.start()+len(m[1]) for m in re.finditer(
        r'(?<![A-Za-z0-9_])([a-z]{4,})-([a-z]{4,})(?![A-Za-z0-9_])', text)} if compound_hyphens else set()
    ligatures = str.maketrans({'ﬁ':'fi','ﬂ':'fl','ﬀ':'ff','ﬃ':'ffi','ﬄ':'ffl'})
    for m in re.finditer(r'(?<=[A-Za-z])-[ \t]*\r?\n[ \t]*(?=[A-Za-z])|\u00ad|\s+|.', text, re.S):
        token = m.group()
        if m.start() in missing or token == '\u00ad' or token.startswith('-') and '\n' in token:
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
    normalization = 'typography'
    if start < 0 and len(quote.strip()) >= 48:
        haystack, offsets = _canonical(text, compound_hyphens=True)
        needle = _canonical(quote, compound_hyphens=True)[0].strip()
        start = haystack.find(needle) if needle else -1
        if start >= 0 and haystack.find(needle, start+1) >= 0:
            return None
        normalization = 'compound_hyphen'
    if start < 0:
        return None
    a,b = offsets[start][0], offsets[start+len(needle)-1][1]
    return {'start':a,'end':b,'quote':text[a:b], 'normalization':normalization}


def audit_map(answer, proposed, excerpts, *, anchor_trial=False, answer_bindings=None,
              answer_protocol='legacy_answer_v1'):
    """Missing/legacy maps are explicit unknowns, not semantic approval or refusal.

    Auxiliary structure errors warn; unverifiable proposed quotations hold the draft. A located
    quotation only proves its bytes occurred in a delivered source, never that
    the source discusses the claimed subject or supports the claim.
    """
    result = {'version': 2, 'state': 'missing', 'semantic_status': 'unreviewed',
              'items': [], 'issues': [], 'blocking_issues': [], 'warnings': []}
    result['protocol'] = 'anchors_trial_v2' if anchor_trial else 'quotes_v1'
    result['answer_protocol'] = answer_protocol
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
        from .qa_answer import bind
        binding = bind(answer, claim)
        if answer_bindings is not None and pos <= len(answer_bindings):
            candidate = answer_bindings[pos-1]
            s = candidate['span']
            if answer[s['start']:s['end']] != claim or s['text'] != claim:
                raise ValueError('程序回答位置与正文不一致。')
            binding = candidate
        item['answer_binding'] = binding
        extended = any(k in raw for k in ('subject', 'basis', 'quotes'))
        if extended and binding['state'] != 'bound':
            issues.append('陈述在草稿中重复出现，无法唯一绑定。' if binding['state'] == 'ambiguous'
                          else '陈述未对应草稿中的原句。')
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
        if basis not in ('direct', 'inference', 'metadata', 'not_located'):
            issues.append('依据类型必须为直接描述、推断、程序记录或尚未定位。')
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
                if isinstance(q, dict) and 'anchor_id' in q:
                    issues.append('证据锚点无效：未绑定本轮实际来源。')
                continue
            tag = q['source']
            if 'anchor_id' in q:
                try:
                    if not anchor_trial or basis == 'metadata':
                        raise ValueError('证据锚点无效：本轮未启用原文锚点试验，或将其误作程序记录。')
                    from .qa_anchors import resolve
                    found = resolve(pool[tag], q['anchor_id'])
                    # Additional copied text remains subject to the old strict
                    # locator. An anchor cannot make a known bad quote disappear.
                    if 'quote' in q and not locate(found['quote'], q['quote']):
                        raise ValueError('证据锚点无效：附带引句不在所选原文中，不能用锚点绕过原句检查。')
                    item['quotes'].append({'source': tag, **found})
                    quoted.add(tag)
                except ValueError as exc:
                    issues.append(str(exc))
                continue
            found = locate(pool[tag]['text'], q.get('quote')) if basis != 'metadata' else None
            if not found and isinstance(q.get('quote'), str):
                from .qa_evidence import source_metadata
                # A metadata quote must match a complete known field, not an
                # arbitrary substring of a URL/date or model-invented metadata.
                for field, value in source_metadata(pool[tag]).items():
                    if _canonical(q['quote'])[0].strip() == _canonical(value)[0].strip():
                        found = {'quote': value, 'kind': 'program_metadata', 'field': field}
                        break
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
        item['blocking_issues'] = [s for s in item['issues'] if '中无法定位所给原句' in s or '证据锚点无效' in s]
        item['warnings'] = [s for s in item['issues'] if s not in item['blocking_issues']]
        result['blocking_issues'].extend(f"{item['id']}：{s}" for s in item['blocking_issues'])
        if any(q.get('normalization') for q in item['quotes']):
            item['warnings'].append('原句按明确排版等价定位；展示保留原始字符。')
        if any(q.get('kind') == 'program_metadata' for q in item['quotes']):
            item['warnings'].append('匹配程序记录，仅用于来源、获取时间或版本说明，不是论文原句或语义支持证明。')
        if any(q.get('kind') == 'source_anchor' for q in item['quotes']):
            item['warnings'].append('试验锚点原文由程序回填；位置有效不代表对象归属或语义支持已通过。')
        result['warnings'].extend(f"{item['id']}：{s}" for s in item['warnings'])
    result['state'] = ('invalid' if result['issues'] else
                       'legacy' if any(i['state'] == 'legacy' for i in result['items']) else 'recorded')
    return result
