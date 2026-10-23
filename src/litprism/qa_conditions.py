"""Conservative checks for explicit year-conditioned percentage comparisons.

Recognizes labelled index/checkpoint years and evaluation-population years. This is
not an arbitrary numeric entailment checker; unrecognised prose is untouched.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from .textutil import parse_citations

_YEAR = r'(?:18|19|20|21)\d{2}'
_PERCENT = re.compile(r'(?<![\d.])(\d+(?:\.\d+)?)\s*%')
_ROLES = {
    'index': re.compile(r'('+_YEAR+r')\s*(?:年\s*)?(?:的\s*)?(?:index\b|checkpoint\b|索引|检查点)', re.I),
    'target': re.compile(r'('+_YEAR+r')\s*(?:年\s*)?(?:的|在任的?|的在任)?\s*(?:world\s+leaders\b|leaders\b|evaluation\s+(?:set|population)\b|test\s+set\b|世界领导人|领导人|领袖|在任者|评测集|测试集)', re.I),
}


def _roles(text):
    values = {}
    for name, pattern in _ROLES.items():
        found = {m.group(1) for m in pattern.finditer(text)}
        if len(found) == 1:
            values[name] = next(iter(found))
    return values


def _sentences(text):
    # Decimal points and citation page dots are not sentence boundaries.
    return re.split(r'[。！？\n]+|(?<!\d)\.(?=\s+[A-Z]|$)', text)


def _records(sentence):
    # Explicit parenthetical target is local to its percentage. Canonicalize this
    # labelled form before shared-condition parsing; never drop the target year.
    sentence = re.sub(
        r'('+_YEAR+r')\s*(?:年\s*)?索引\s*(?:答对|得|准确率为|正确率为)\s*'
        r'(\d+(?:\.\d+)?\s*%)\s*\(对应\s*('+_YEAR+r')\s*(?:年\s*)?'
        r'(?:在任者|世界领导人|领导人|领袖)\s*\)',
        lambda m: f'{m[1]} index for {m[3]} leaders {m[2]}', sentence)
    matches = list(_PERCENT.finditer(sentence))
    if not matches:
        return []
    first = matches[0]
    prefix = sentence[:first.start()]
    # Determine position separately for each role: "2024 index scored 67%
    # for 2024 leaders" has one condition on either side of its value.
    # The lead-in may name both indices before stating the first result. Bind
    # to the nearest labelled condition, rather than treating that introduction
    # as the first result's conditions.
    shared = {}
    for role, pattern in _ROLES.items():
        role_matches = list(pattern.finditer(prefix))
        if role_matches:
            shared[role] = role_matches[-1].group(1)
    records = []
    for i, match in enumerate(matches):
        before = sentence[matches[i-1].end() if i else 0:match.start()]
        after = sentence[match.end():matches[i+1].start() if i+1 < len(matches) else len(sentence)]
        roles = {}
        for role in _ROLES:
            region = before if role in shared else after
            value = _roles(region).get(role, shared.get(role))
            if value is not None:
                roles[role] = value
        if len(matches) == 1:
            # "2024 index scored 67% for 2024 leaders" straddles the value.
            roles = _roles(sentence)
        records.append((format(Decimal(match.group(1)).normalize(), 'f'), roles))
    return records


def _table_claims(answer):
    """Recognise labelled three-column year/percentage rows; no guessed schema."""
    headers = None
    for line in answer.splitlines():
        if '|' not in line:
            headers = None
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if headers is None:
            index = next((i for i, c in enumerate(cells) if re.search(r'索引.*年|index.*year|year.*index', c, re.I)), None)
            target = next((i for i, c in enumerate(cells) if re.search(r'(?:评测|测试|领导人).*年|(?:target|leader|evaluation).*year', c, re.I)), None)
            metric = next((i for i, c in enumerate(cells) if re.search(r'准确率|正确率|accuracy', c, re.I)), None)
            if None not in (index, target, metric) and len({index, target, metric}) == 3:
                headers = (index, target, metric)
            continue
        if max(headers) >= len(cells):
            continue
        values = [re.findall(_YEAR, cells[i]) for i in headers[:2]]
        pct = list(_PERCENT.finditer(cells[headers[2]]))
        if all(len(v) == 1 for v in values) and len(pct) == 1:
            refs = ' '.join('['+tag+(f' p.{page}' if page is not None else '')+']'
                            for tag, page in parse_citations(line))
            yield f'{values[0][0]} index for {values[1][0]} leaders: {pct[0].group(0)} {refs}'


def conditional_risks(answer, excerpts):
    by_tag = {e['tag']: e for e in excerpts}
    issues = []
    clauses = re.findall(r'[^。！？\n]+[。！？]?(?:\s*\[P\d+[^\]]*\])*', answer)
    for clause in [*clauses, *_table_claims(answer)]:
        plain = re.sub(r'\[P\d+[^\]]*\]', '', clause)
        plain = unicodedata.normalize('NFKC', plain)
        if not _PERCENT.search(plain) or not any(p.search(plain) for p in _ROLES.values()):
            continue
        # Explicitly labelled rejected/counterfactual examples are not asserted
        # results; do not turn a correct correction into a false alarm.
        if re.search(r'不能说|不应说|错误(?:地|说法)|并非|\b(?:not|incorrect|wrong)\b', plain, re.I):
            continue
        tags = {tag for tag, _ in parse_citations(clause)}
        allowed = {}
        for tag in tags:
            source = by_tag.get(tag)
            if not source:
                continue
            text = ' '.join(unicodedata.normalize('NFKC', source['text']).split())
            for sentence in _sentences(text):
                for value, roles in _records(sentence):
                    if len(roles) == 2:
                        allowed.setdefault(value, set()).add((roles['index'], roles['target']))
        for value, roles in _records(plain):
            if value not in allowed:
                continue
            actual = (roles.get('index'), roles.get('target'))
            if None in actual or actual not in allowed[value]:
                issues.append(f"百分比 {value}% 的索引年份与评测对象年份未对应同一原文条件，请人工核对。")
    return list(dict.fromkeys(issues))
