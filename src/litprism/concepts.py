"""Keep term explanations separate from the source locations they cite."""
from __future__ import annotations

import re
import unicodedata

from .evidence import citation_issues
from .textutil import CITATION_RE, parse_citations
from .whitelist import is_citable

PAPER_KINDS = {'paper_definition', 'paper_usage'}
LABELS = {'paper_definition': '论文定义', 'paper_usage': '文中用法',
          'general': '通用含义', 'insufficient': '证据不足'}
GENERAL_RE = re.compile(r'通用含义\s*[：:]')
BACK_TO_PAPER_RE = re.compile(r'[；;。]\s*(?=(?:在本论文语境中|论文|文中|本文|本研究))')
NO_DEFINITION_RE = re.compile(r'[（(]\s*论文未给出定义\s*[）)]')
LAYOUT_HYPHEN_RE = re.compile(r'(?<=[A-Za-z]{3})-(?=[A-Za-z]{3})')


def _literal_term(term, chunks):
    """A mention is a useful check for misattribution, not a semantic verdict."""
    def words(value):
        return re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold())

    # Terms may give a long form and an abbreviation in parentheses.
    variants = {tuple(words(v)) for v in re.split(r'[()（）]', term) if words(v)}
    # Do not let separated words or incidental partial names count as a mention.
    for chunk in chunks:
        source = ' '.join(words(chunk['text']))
        for variant in variants:
            phrase = ' '.join(variant)
            if (re.search(r'[\u3400-\u9fff]', phrase) and phrase in source
                    or re.search(r'(?<!\w)' + re.escape(phrase) + r'(?:s|es)?(?!\w)', source)):
                return True
    return False


def _quote_found(quote, chunks):
    quote = ' '.join(quote.split())
    return len(quote) >= 8 and any(quote in ' '.join(c['text'].split()) for c in chunks)


def _single_hyphen_fold(text):
    for mark in LAYOUT_HYPHEN_RE.finditer(text):
        i = mark.start()
        yield text[:i] + text[i+1:]


def _layout_match(quote, text):
    """Allow one letter-to-letter hyphen discrepancy, not general fuzzy matching."""
    if len(quote.split()) < 5:
        return False
    return (any(candidate in text for candidate in _single_hyphen_fold(quote))
            or any(quote in candidate for candidate in _single_hyphen_fold(text)))


def _adjacent(left, right):
    return (left.get('paper_id') and left['paper_id'] == right.get('paper_id')
            and left.get('material_id') and left['material_id'] == right.get('material_id')
            and left.get('id') and right.get('id') and left['id'] != right['id']
            and int(right['tag'][1:]) == int(left['tag'][1:]) + 1
            and left.get('section') == right.get('section')
            and (left.get('page') == right.get('page')
                 or (isinstance(left.get('page'), int) and right.get('page') == left['page'] + 1)))


def _overlap_contains(quote, left, right):
    # PDF blocks may repeat their boundary. Only join a substantial exact
    # overlap; joining arbitrary consecutive paragraphs can invent a sentence.
    a, b = left.split(), right.split()
    for count in range(min(len(a)-1, len(b)-1, 160), 4, -1):
        if a[-count:] == b[:count]:
            return len(' '.join(b[:count])) >= 25 and quote in ' '.join(a + b[count:])
    return False


def _neighbors(cited, by_tag):
    edges = {}
    for c in cited:
        n = int(c['tag'][1:])
        for index in (n-1, n+1):
            other = by_tag.get(f'P{index}')
            if not other:
                continue
            left, right = (other, c) if index < n else (c, other)
            if _adjacent(left, right):
                edges[(left['tag'], right['tag'])] = (left, right)
    return list(edges.values())


def _locate(quote, cited, by_tag):
    quote = ' '.join(quote.split())
    if len(quote) < 8:
        return None
    for c in cited:
        if quote in ' '.join(c['text'].split()):
            return {'mode': 'exact', 'quote_tags': [c['tag']]}
    for c in cited:
        if _layout_match(quote, ' '.join(c['text'].split())):
            return {'mode': 'layout', 'quote_tags': [c['tag']]}
    tags = {c['tag'] for c in cited}
    edges = _neighbors(cited, by_tag)
    for pair in edges:
        neighbor = next((c for c in pair if c['tag'] not in tags), None)
        if neighbor and _quote_found(quote, [neighbor]):
            return {'mode': 'exact', 'quote_tags': [neighbor['tag']]}
    for a, b in edges:
        if _overlap_contains(quote, a['text'], b['text']):
            return {'mode': 'overlap', 'quote_tags': [a['tag'], b['tag']]}
    return None


def _align(term, kind, quote, cited, by_tag):
    anchor = _locate(quote, cited, by_tag)
    if not anchor:
        return None, 'quote'
    if _literal_term(term, cited):
        return anchor, ''
    # A definition still needs the name within the original or joined evidence.
    ids = {c['tag'] for c in cited}
    candidates = [by_tag[t] for t in anchor['quote_tags'] if t not in ids]
    if kind == 'paper_usage':
        candidates += [c for pair in _neighbors(cited, by_tag) for c in pair if c['tag'] not in ids]
    for other in candidates:
        if _literal_term(term, [other]):
            return dict(anchor, context_tag=other['tag']), ''
    return None, 'term_context'


def normalize_concepts(raw, excerpts):
    """Validate each proposed section against the text supplied in its own request."""
    if not isinstance(raw, list):
        raise ValueError('concepts 必须是数组。')
    by_tag = {e['tag']: e for e in excerpts}
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get('term'), str) or not row['term'].strip():
            continue
        term = row['term'].strip()
        category = row.get('category') if isinstance(row.get('category'), str) else None
        # Legacy services and frozen responses still use the original single-string contract.
        if 'sections' not in row:
            definition = row.get('definition')
            if isinstance(definition, str) and not citation_issues(definition, excerpts):
                yield {'term': term, 'category': category, 'definition': definition}
            continue
        if not isinstance(row['sections'], list):
            continue
        sections = []
        summary = []
        for section in row['sections']:
            if not isinstance(section, dict) or not isinstance(section.get('kind'), str):
                continue
            kind, value = section['kind'], section.get('text')
            if kind not in LABELS or not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            uncited = CITATION_RE.sub('', value).strip()
            if citation_issues(uncited, [], required=False):
                continue
            if kind not in PAPER_KINDS:
                # A generic meaning or admission of uncertainty cannot borrow a paper citation.
                if uncited:
                    sections.append({'kind': kind, 'text': uncited})
                    summary.append(f'{LABELS[kind]}：' + (uncited if kind == 'general' else '本轮未定位依据。'))
                continue
            quote = section.get('quote')
            quote = quote.strip() if isinstance(quote, str) else ''
            valid = not citation_issues(value, excerpts)
            cited = [by_tag[tag] for tag, _ in parse_citations(value)] if valid else []
            anchor, reason = _align(term, kind, quote, cited, by_tag) if valid else (None, 'references')
            if not anchor:
                sections.append({'kind': 'insufficient', 'text': value, 'pending': True, 'reason': reason})
                summary.append('证据不足：本轮未定位释义依据。')
                continue
            sections.append({'kind': kind, 'text': value, 'quote': quote, 'anchor': anchor})
            summary.append(f'{LABELS[kind]}：{value}')
        if sections:
            yield {'term': term, 'category': category, 'definition': '\n'.join(summary), 'sections': sections}


def _legacy_sections(definition):
    """Split explicit generic labels; never infer an older entry is a paper definition."""
    result = []
    definition = NO_DEFINITION_RE.sub('', definition).strip()
    label = GENERAL_RE.search(definition)
    if not label:
        return [{'kind': 'legacy', 'text': definition}] if definition else []
    before, after = definition[:label.start()].strip(), definition[label.end():].strip()
    tail = BACK_TO_PAPER_RE.search(after)
    if before:
        result.append({'kind': 'legacy', 'text': before})
    general = after[:tail.start()] if tail else after
    if general.strip():
        result.append({'kind': 'general', 'text': general.strip()})
    if tail and after[tail.end():].strip():
        result.append({'kind': 'paper_usage', 'text': after[tail.end():].strip(), 'legacy': True})
    return result


def concept_details(kb, paper, payload, material_id):
    """Return navigation links only when a saved citation matches its exact version and page."""
    concepts = payload.get('concepts')
    if not isinstance(concepts, list) or not concepts:
        return []
    pid = paper['id']
    chunks = ({c['id']: c for c in kb.get_chunks(pid, material_id=material_id)}
              if material_id and is_citable(paper.get('verify_level')) else {})
    citations, seen = {}, set()
    for cite in payload.get('_citations') or []:
        if not isinstance(cite, dict) or not isinstance(cite.get('tag'), str):
            continue
        tag = cite['tag'].upper()
        if tag in seen:
            citations.pop(tag, None)
            continue
        seen.add(tag)
        ref = cite.get('chunk_id')
        chunk = chunks.get(ref) if isinstance(ref, str) else None
        if (chunk and cite.get('paper_id') == pid and cite.get('material_id', material_id) == material_id
                and chunk.get('material_id') == material_id and cite.get('page') == chunk.get('page')):
            citations[tag] = dict(chunk, tag=tag)

    details = []
    for row in concepts:
        if not isinstance(row, dict) or not isinstance(row.get('term'), str):
            continue
        definition = row.get('definition')
        sections = row.get('sections')
        if not isinstance(sections, list):
            sections = _legacy_sections(definition) if isinstance(definition, str) else []
        blocks = []
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get('text'), str):
                continue
            kind, value = section.get('kind'), section['text']
            if not isinstance(kind, str) or (kind != 'legacy' and kind not in LABELS):
                continue
            matches = list(dict.fromkeys(parse_citations(value)))
            sources, cited = [], []
            for tag, page in matches:
                c = citations.get(tag)
                if c and (page is None or page == str(c.get('page'))):
                    sources.append({'tag': tag, 'ref': c['id'], 'page': c['page']})
                    cited.append(c)
            quote = section.get('quote')
            note = ''
            if kind in PAPER_KINDS or kind == 'legacy':
                has_all = bool(sources) and len(sources) == len(matches)
                stored = section.get('anchor')
                if not has_all:
                    note = '未能定位到对应版本的全部原文'
                    kind = 'insufficient'
                elif isinstance(stored, dict) and isinstance(quote, str) and kind in PAPER_KINDS:
                    # Revalidate only the explicitly saved anchor and original refs.
                    # Never fill a missing saved source from the analysis's other material.
                    saved_tags = stored.get('quote_tags')
                    allowed = {c['tag']: c for c in cited}
                    wanted = saved_tags + [stored.get('context_tag')] if isinstance(saved_tags, list) else []
                    for tag in wanted:
                        if isinstance(tag, str) and tag in citations:
                            allowed[tag] = citations[tag]
                    again, _ = _align(row['term'], kind, quote, cited, allowed)
                    if again != stored:
                        kind = 'insufficient'
                        note = '未能重新定位保存的引句'
                    else:
                        for tag in dict.fromkeys(t for t in wanted if isinstance(t, str)):
                            if tag in allowed and tag not in {s['tag'] for s in sources}:
                                c = allowed[tag]
                                sources.append({'tag': tag, 'ref': c['id'], 'page': c['page'], 'supplemental': True})
                        if stored['mode'] == 'layout':
                            note = '已定位引句（排版不同） · 释义未审核'
                        else:
                            note = '已定位引句 · 释义未审核'
                elif not _literal_term(row['term'], cited):
                    note = '所引片段未定位到术语写法'
                    kind = 'insufficient'
                elif isinstance(quote, str) and _quote_found(quote, cited):
                    note = '已定位引句 · 释义未审核'
                else:
                    note = '已定位来源 · 释义待核对'
                    if kind != 'legacy' and not section.get('legacy'):
                        kind = 'insufficient'
            if kind == 'insufficient' and not note:
                note = ('引句可定位，术语归属待核对' if section.get('reason') == 'term_context' and sources
                        else '未找到可核对的释义依据')
            text = re.sub(r'[ \t]+([。；，、！？])', r'\1', CITATION_RE.sub('', value)).strip()
            blocks.append({'kind': kind, 'text': text, 'note': note,
                           'sources': sources if kind != 'general' else [],
                           'pending': kind == 'insufficient' and bool(matches or section.get('pending'))})
        if blocks:
            details.append({'term': row['term'], 'category': row.get('category'), 'sections': blocks})
    return details
