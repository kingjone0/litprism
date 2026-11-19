# ruff: noqa: F811

import pytest
from test_workflow_guards import env  # noqa: F401

from litprism.concepts import normalize_concepts
from litprism.evidence import citations_for
from litprism.extract import analyze_paper


def add_paper(kb, title='Term study'):
    pid = kb.add_paper(title=title, verify_level='verified')
    kb.add_chunks(pid, [
        {'page': 3, 'text': 'BLEU is reported for the English–German task in this experiment. ' * 8},
        {'page': 4, 'text': 'The English–French data uses a different tokenizer. ' * 8},
    ], level='fulltext')
    return pid


def test_term_projection_preserves_version_and_does_not_cite_general_meaning(env):
    _, kb, _, api = env
    pid = add_paper(kb)
    original, other_page = kb.get_chunks(pid)
    cites = citations_for([dict(original, tag='P1'), dict(other_page, tag='P2')])
    payload = {'_citations': cites, 'concepts': [
        {'term': 'BLEU', 'definition': '（论文未给出定义）通用含义：翻译评估指标 [P2 p.4]；论文在英德实验中报告该指标 [P1 p.3]。'},
        {'term': 'ZQX77', 'definition': '论文给出了 ZQX77 的精确数学公式 [P1 p.3]。'},
        {'term': 'BLEU', 'definition': '错误的页码 [P1 p.7]。'},
    ]}
    kb.save_analysis(pid, payload, material_id=original['material_id'])
    kb.add_chunks(pid, [{'page': 3, 'text': 'A later material has a different BLEU line.'}], level='fulltext')
    current = kb.get_chunks(pid)[0]
    kb.save_analysis(pid, {'concepts': [{'term': 'BLEU', 'definition': '新版术语 [P1 p.3]。'}],
                           '_citations': citations_for([dict(current, tag='P1')])})

    initial = api.get(f'/api/paper/{pid}').json()
    old_id = initial['analysis_history'][0]['id']
    detail = api.get(f'/api/paper/{pid}', params={'version_id': old_id}).json()
    assert detail['analysis'] == payload
    first, unrelated, wrong_page = [c['sections'] for c in detail['concept_details']]
    generic, usage = first
    assert generic['kind'] == 'general' and generic['sources'] == []
    assert generic['text'] == '翻译评估指标'
    assert usage['kind'] == 'paper_usage' and '释义待核对' in usage['note']
    assert usage['sources'] == [{'tag': 'P1', 'ref': original['id'], 'page': 3}]
    assert [b[0]['kind'] for b in (unrelated, wrong_page)] == ['insufficient', 'insufficient']
    assert unrelated[0]['pending'] and '未定位到术语' in unrelated[0]['note']
    assert wrong_page[0]['sources'] == []
    source = api.get('/api/evidence/'+usage['sources'][0]['ref']).json()
    assert source['material']['status'] == 'superseded'
    assert source['text'] == original['text']
    latest = api.get(f'/api/paper/{pid}').json()['concept_details'][0]['sections'][0]
    assert latest['sources'][0]['ref'] == current['id']


def test_structured_concepts_require_literal_quote_inside_the_request_and_keep_background_separate(env):
    _, kb, _, api = env
    pid = add_paper(kb)
    excerpts = [dict(c, tag=f'P{i}') for i, c in enumerate(kb.get_chunks(pid), 1)]
    response = [{'term': 'BLEU', 'sections': [
        {'kind': 'paper_usage', 'text': '英德任务报告 BLEU [P1 p.3]。',
         'quote': 'BLEU is reported for the English–German task'},
        {'kind': 'paper_definition', 'text': '错误地说论文给出精确算式 [P1 p.3]。',
         'quote': 'This formula does not occur'},
        {'kind': 'general', 'text': '机器翻译指标 [P2 p.4]'},
        {'kind': 'general', 'text': '根据我的记忆，采用另外一种定义 [mem:term]。'},
    ]}]
    # In the narrow view P1 really refers to this same material, but its
    # source text was trimmed before sending. Do not accept a quote from the omitted suffix.
    restricted, = normalize_concepts(response, [dict(excerpts[0], text='BLEU is reported.')])
    assert restricted['sections'][0]['kind'] == 'insufficient'
    concepts = list(normalize_concepts(response, excerpts))
    assert '精确算式' not in concepts[0]['definition']
    kb.save_analysis(pid, {'concepts': concepts, '_citations': citations_for(excerpts)})
    sections = api.get(f'/api/paper/{pid}').json()['concept_details'][0]['sections']
    assert [s['kind'] for s in sections] == ['paper_usage', 'insufficient', 'general']
    assert sections[0]['note'] == '已定位引句 · 释义未审核'
    assert sections[1]['pending']
    assert sections[2]['sources'] == []


def _structured(term, kind, text, quote):
    return [{'term': term, 'sections': [{'kind': kind, 'text': text, 'quote': quote}]}]


@pytest.mark.parametrize(('quote', 'kind'), [
    ('Ink encoding has a source-target vocabulary of about 37000 tokens.', 'paper_usage'),
    ('Ink encoding has a source-target vocabulary of about 32000 tokens.', 'insufficient'),
    ('Ink encoding uses a temperature of -40 degrees.', 'insufficient'),
])
def test_hyphen_layout_does_not_accept_changed_value_or_sign(quote, kind):
    excerpts = [{'tag': 'P1', 'page': 1, 'text':
                 'Ink encoding has a sourcetarget vocabulary of about 37000 tokens. '
                 'Ink encoding uses a temperature of 40 degrees.'}]
    proposal = _structured('Ink encoding', 'paper_usage', '本文实验的设置 [P1 p.1]。', quote)
    item, = normalize_concepts(proposal, excerpts)
    section, = item['sections']
    assert section['kind'] == kind
    if kind == 'paper_usage':
        assert section['anchor'] == {'mode': 'layout', 'quote_tags': ['P1']}
        assert section['quote'] == quote


def test_use_can_find_full_term_in_explicit_neighbor_but_definition_cannot_borrow_it(env):
    _, kb, _, api = env
    pid = kb.add_paper(title='Adjacent term study', verify_level='verified')
    kb.add_chunks(pid, [
        {'page': 1, 'section': 'Methods', 'text': 'We introduce the Window Mixer module here.'},
        {'page': 2, 'section': 'Methods', 'text': 'The inner dimension equals 2048 in this experiment.'},
    ], level='fulltext')
    excerpts = [dict(c, tag=f'P{i}') for i, c in enumerate(kb.get_chunks(pid), 1)]
    proposal = _structured('Window Mixer', 'paper_usage', '实验的内层宽度为 2048 [P2 p.2]。',
                           excerpts[1]['text'])
    item, = normalize_concepts(proposal, excerpts)
    assert item['sections'][0]['anchor'] == {'mode': 'exact', 'quote_tags': ['P2'], 'context_tag': 'P1'}
    kb.save_analysis(pid, {'concepts': [item], '_citations': citations_for(excerpts)})
    section = api.get(f'/api/paper/{pid}').json()['concept_details'][0]['sections'][0]
    assert section['kind'] == 'paper_usage'
    assert section['sources'] == [
        {'tag': 'P2', 'ref': excerpts[1]['id'], 'page': 2},
        {'tag': 'P1', 'ref': excerpts[0]['id'], 'page': 1, 'supplemental': True},
    ]
    # The API cannot reconstruct a lost supplemental ref from the rest of the paper.
    kb.save_analysis(pid, {'concepts': [item], '_citations': citations_for(excerpts[1:])})
    broken = api.get(f'/api/paper/{pid}').json()['concept_details'][0]['sections'][0]
    assert broken['kind'] == 'insufficient' and broken['sources'] == section['sources'][:1]
    proposal[0]['sections'][0]['kind'] = 'paper_definition'
    assert list(normalize_concepts(proposal, excerpts))[0]['sections'][0]['reason'] == 'term_context'


@pytest.mark.parametrize(('other', 'outcome'), [
    ({}, 'overlap'),
    ({'paper_id': 'different-paper'}, 'insufficient'),
    ({'material_id': 'different-material'}, 'insufficient'),
    ({'tag': 'P3'}, 'insufficient'),
    ({'section': 'Results'}, 'insufficient'),
    ({'page': 4}, 'insufficient'),
    ({'text': 'Methods report a different setup and continue.'}, 'insufficient'),
])
def test_cross_block_quote_needs_real_overlap_and_same_scope(other, outcome):
    overlap = 'it assigns a distinct signal to each starting position'
    source = {'paper_id': 'paper', 'material_id': 'material', 'section': 'Method'}
    left = dict(source, id='left', page=1, tag='P1', text=f'Position Tagging is an approach in which {overlap}')
    right = dict(source, id='right', page=2, tag='P2', text=f'{overlap} before embedding the tokens.')
    right.update(other)
    quote = f'Position Tagging is an approach in which {overlap} before embedding the tokens.'
    proposal = _structured('Position Tagging', 'paper_definition', '位置处理方式 [P1 p.1]。', quote)
    section = list(normalize_concepts(proposal, [left, right]))[0]['sections'][0]
    if outcome == 'overlap':
        assert section['anchor'] == {'mode': 'overlap', 'quote_tags': ['P1', 'P2']}
    else:
        assert section['kind'] == outcome and 'anchor' not in section


def test_neighbor_quote_keeps_original_reference_and_never_uses_out_of_request_text():
    source = {'paper_id': 'paper', 'material_id': 'material', 'section': 'Results'}
    excerpt = dict(source, id='results', page=2, tag='P4', text='BLEU is the metric in the development task.')
    previous = dict(source, id='prior', page=1, tag='P3', text='We varied the base model across three experiments.')
    candidates = [previous, excerpt]
    proposal = _structured('BLEU', 'paper_usage', '用于对比三组实验 [P4 p.2]。', previous['text'])
    section = list(normalize_concepts(proposal, candidates))[0]['sections'][0]
    assert section['anchor'] == {'mode': 'exact', 'quote_tags': ['P3']}
    assert section['text'].endswith('[P4 p.2]。')
    assert list(normalize_concepts(proposal, [excerpt]))[0]['sections'][0]['reason'] == 'quote'
    proposal[0]['sections'][0]['text'] = '页码错误 [P4 p.9]。'
    assert list(normalize_concepts(proposal, candidates))[0]['sections'][0]['reason'] == 'references'


@pytest.mark.parametrize('failure', [False, True])
def test_whole_plan_attempts_terms_once_across_separate_directives(env, monkeypatch, failure):
    cfg, kb, _, _ = env
    pid = add_paper(kb)
    from litprism.plan import Directive, Plan
    monkeypatch.setattr('litprism.plan.build_plan', lambda *a, **kw:
                        Plan(paper_id=pid, mode='whole', context_tokens=10,
                             directives=[Directive(id='d1', runs=[], quota=1), Directive(id='d2', runs=[], quota=1)]))
    cfg.context.window_tokens = 100000
    seen = []

    class Model:
        name = 'offline'
        supports_semantic_review = False

        def chat_json(self, messages, *, task):
            seen.append(task)
            if task == 'concepts':
                if failure:
                    raise ValueError('offline failure')
                return {'concepts': []}
            return {}

    result = analyze_paper(kb, cfg, Model(), pid, force=True)
    assert result['_coverage']['reading_mode'] == 'whole'
    assert seen.count('concepts') == 1 and seen.count('extract') >= 2
    assert bool(result.get('_concepts_error')) == failure


def test_actual_distinct_batches_still_get_their_own_terms(env, monkeypatch):
    cfg, kb, _, _ = env
    pid = add_paper(kb)
    from litprism.plan import Plan
    monkeypatch.setattr('litprism.plan.build_plan', lambda *a, **kw:
                        Plan(paper_id=pid, mode='batched', context_tokens=10))
    excerpts = [dict(c, tag=f'P{i}', paper_title='Term study') for i, c in enumerate(kb.get_chunks(pid), 1)]
    monkeypatch.setattr('litprism.reading.reading_batches', lambda *a, **kw:
                        ([[e] for e in excerpts], excerpts, {'complete': True, 'batches': 2}))
    cfg.context.window_tokens = 100000
    seen = []

    class Model:
        name = 'offline'
        supports_semantic_review = False

        def chat_json(self, messages, *, task):
            if task == 'concepts':
                seen.append(messages[-1]['content'])
                return {'concepts': []}
            return {}

    result = analyze_paper(kb, cfg, Model(), pid, force=True)
    assert result['_coverage']['reading_mode'] == 'batched'
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(c['text'] in msg for c, msg in zip(excerpts, seen, strict=True))
