"""Opt-in source spans. Location is not evidence entailment.

The original source text is never rewritten. Paragraphs keep exact character
ranges; punctuation and bounded fragments make review manageable without rewriting
PDF line wraps. Legacy paragraph IDs only resolve historical selections. IDs bind
immutable source identity, full text hash and character range, not P tags.
"""
from __future__ import annotations

import hashlib
import json
import re


def _paragraphs(excerpt):
    text = excerpt['text']
    if not isinstance(excerpt.get('chunk_id'), str) or not excerpt['chunk_id']:
        raise ValueError('证据锚点无效：来源缺少稳定块标识。')
    identity = {k: excerpt.get(k) for k in ('chunk_id', 'source_id', 'paper_id', 'material_id', 'page')}
    identity['text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    identity['source_kind'] = excerpt.get('source_kind', 'paper')
    anchors = []
    start = 0
    for match in [*re.finditer(r'\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*', text), None]:
        end = match.start() if match else len(text)
        raw = text[start:end]
        a = start + len(raw) - len(raw.lstrip())
        b = end - (len(raw) - len(raw.rstrip()))
        if a < b:
            bound = dict(identity, start=a, end=b)
            digest = hashlib.sha256(json.dumps(bound, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            anchors.append(dict(bound, id='qa_'+digest[:24], quote=text[a:b]))
        start = match.end() if match else len(text)
    return anchors


MAX_SPAN_CHARACTERS = 360
_ABBREVIATIONS = {'al', 'fig', 'figs', 'eq', 'eqs', 'dr', 'mr', 'mrs', 'prof', 'vs', 'etc', 'sec', 'secs', 'no', 'vol', 'pp', 'p'}


def _sentences(text, start, end):
    current = start
    for m in re.finditer(r'[.!?。！？][”’"\')）]*', text[start:end]):
        a, b = start+m.start(), start+m.end()
        if text[a] in '.!?':
            if b < end and not text[b].isspace():
                continue
            word = re.search(r'([A-Za-z]+)$', text[current:a])
            if text[a] == '.' and (word and (word[1].lower() in _ABBREVIATIONS or len(word[1]) == 1)
                                   or re.search(r'(?:e\.g|i\.e)$', text[max(current, a-6):a], re.I)):
                continue
        # Include an immediately trailing numeric reference, without combining
        # source blocks or moving content across their immutable boundaries.
        refs = re.match(r'(?:[ \t]*\[[\d,; \-–]+\])+', text[b:end])
        if refs:
            b += refs.end()
        yield current, b
        current = b
    if current < end:
        yield current, end


def build(excerpt):
    text = excerpt['text']
    anchors = []
    for paragraph in _paragraphs(excerpt):
        identity = {k: paragraph[k] for k in ('chunk_id', 'source_id', 'paper_id', 'material_id', 'page', 'text_sha256', 'source_kind')}
        for start, end in _sentences(text, paragraph['start'], paragraph['end']):
            first = True
            while start < end:
                while start < end and text[start].isspace():
                    start += 1
                if start >= end:
                    break
                stop = min(end, start+MAX_SPAN_CHARACTERS)
                if stop < end:
                    spaces = list(re.finditer(r'\s+', text[start:stop]))
                    if spaces:
                        stop = start+spaces[-1].start()
                trimmed = stop
                while trimmed > start and text[trimmed-1].isspace():
                    trimmed -= 1
                if trimmed == start:
                    stop = min(end, start+MAX_SPAN_CHARACTERS)
                    trimmed = stop
                bound = dict(identity, start=start, end=trimmed, protocol='source_spans_v2')
                digest = hashlib.sha256(json.dumps(bound, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                terminal = re.search(r'[.!?。！？][”’"\')）]*(?:[ \t]*\[[\d,; \-–]+\])*$', text[start:trimmed])
                anchors.append(dict(bound, id='qa2_'+digest[:24], quote=text[start:trimmed],
                                    granularity='sentence' if first and stop == end and terminal else 'fragment',
                                    continues_before=not first, continues_after=stop < end, anchor_version=2))
                first = False
                start = stop
    return anchors


def catalog(excerpts):
    return [{'source': e['tag'], 'anchor_id': a['id'], 'granularity': a['granularity'],
             'start': a['start'], 'end': a['end'], 'preview': a['quote'][:64]}
            for e in excerpts for a in build(e)]


def resolve(excerpt, anchor_id):
    if not isinstance(anchor_id, str):
        raise ValueError('证据锚点无效：必须选择本轮来源的锚点。')
    inventory = _paragraphs(excerpt) if anchor_id.startswith('qa_') else build(excerpt)
    found = next((a for a in inventory if a['id'] == anchor_id), None)
    if found is None:
        raise ValueError('证据锚点无效：编号不属于该来源的当前冻结内容或材料版本。')
    return {k: v for k, v in found.items() if k != 'id'} | {
        'anchor_id': found['id'], 'kind': 'source_anchor', 'anchor_version': found.get('anchor_version', 1),
        'granularity': found.get('granularity', 'legacy_paragraph')}
