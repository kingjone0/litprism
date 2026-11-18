"""Independent Compare must retain selected objects and fail closed on bad output."""
import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.agents import CompareAgent
from litprism.config import Config
from litprism.semantics import make_units, review_units


@pytest.mark.parametrize('verdict,tags,expected', [
    ('contradicted', ['P5'], 'needs_review'),
    ('contradicted', ['P1', 'P5'], 'needs_review'),
    ('contradicted', ['P1'], 'contradicted'),
    ('supported', ['P5'], 'needs_review'),
])
def test_compare_cell_accepts_only_its_own_papers_evidence(verdict, tags, expected):
    import json

    excerpts = [dict(tag=f'P{index}', id=f'c{index}', paper_id=document, document_tag=document,
                     page=index, text=f'Paper {document}, source {index} states its own results.')
                for index, document in enumerate(['D1'] * 4 + ['D2'], 1)]
    text = 'This batch reports 42 results [P1][P2][P3][P4].'
    items = make_units([('dimension|Results|D1', text, [])], excerpts, 1, comparison=True)

    class Reviewer:
        def chat_json(self, messages, **kwargs):
            payload = json.loads(messages[-1]['content'])
            claim, = payload['claims']
            assert claim['document_scope'] == 'D1'
            assert claim['candidate_sources'] == ['P1', 'P2', 'P3', 'P4']
            assert {source['document'] for source in payload['sources']} == {'D1'}
            return {'reviews': [{'id': claim['id'], 'verdict': verdict,
                                 'evidence': tags, 'reason': 'Controlled verdict.'}]}

    item, = review_units(items, excerpts, Config(), Reviewer(), comparison=True)
    assert item['status'] == expected
    assert item['text'] == text
    assert {quote['tag'] for quote in item['quotes']} == ({'P1'} if expected == 'contradicted' else set())


def test_compare_does_not_truncate_evidence_to_fit_window():
    excerpts = [dict(tag=f'P{index}', id=f'c{index}', paper_id='D1', document_tag='D1',
                     page=index, text='Long original source text. ' * 100)
                for index in range(1, 5)]
    items = make_units([('summary', 'Result [P1][P2][P3][P4].', [])], excerpts, 1, comparison=True)
    cfg = Config()
    cfg.context.window_tokens = cfg.context.output_reserve_tokens + 200

    class Reviewer:
        def chat_json(self, *args, **kwargs):
            pytest.fail('A claim exceeding the window must not be sent with trimmed evidence')

    item, = review_units(items, excerpts, cfg, Reviewer(), comparison=True)
    assert item['status'] == 'needs_review'
    assert item['candidate_tags'] == ['P1', 'P2', 'P3', 'P4']
    assert '预算不足' in item['reason']


def test_unknown_selected_object_is_not_silently_dropped(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    ids = [paper(kb, title='A'), paper(kb, title='B')]
    response = client.post('/api/compare', json={'refs': [*ids, 'p_not_a_real_selection']})
    assert response.status_code == 400
    assert not model.calls


def test_two_aliases_of_one_paper_are_not_two_comparison_objects(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    pid = paper(kb, title='Unique selected paper')
    response = client.post('/api/compare', json={'refs': [pid, 'Unique selected paper']})
    assert response.status_code == 400
    assert not model.calls


def test_repeated_selection_keeps_two_distinct_papers_and_original_order(request):
    cfg, kb, model, _ = request.getfixturevalue('env')
    ids = [paper(kb, title='A'), paper(kb, title='B')]
    result = CompareAgent(kb, cfg, model).compare([ids[1], ids[0], ids[1]])
    assert [p['id'] for p in result['_papers']] == [ids[1], ids[0]]
    assert result['_coverage']['total_chunks'] == 2


@pytest.mark.parametrize('raw', [
    [],
    {'summary': '结论 [P1 p.1]', 'dimensions': ['invalid cell']},
    {'summary': '结论 [P1 p.1]', 'dimensions': [{'dimension': '方法', 'values': []}]},
])
def test_malformed_response_is_reported_as_blocked_comparison_not_server_error(request, raw):
    cfg, kb, model, client = request.getfixturevalue('env')
    ids = [paper(kb, title='A'), paper(kb, title='B')]
    model.chat_json = lambda *a, **k: raw
    response = client.post('/api/compare', json={'refs': ids})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert not data['_citation_check']['passed']
    assert not data.get('dimensions') and '结论 [P1' not in data['summary']


def test_scope_aspects_and_document_cells_use_only_selected_current_materials(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1_000_000
    a = paper(kb, title='Selected A', text='CURRENT_A uses attention.')
    b = paper(kb, title='Selected B', text='CURRENT_B uses masked language modelling.')
    paper(kb, title='Unselected', text='UNSELECTED_RAW_SOURCE must not be sent.')
    kb.save_analysis(a, {'research_question': 'OLD_ANALYSIS_MUST_NOT_BECOME_SOURCE'})
    observed = []

    def reply(messages, task='', **kwargs):
        observed.append((task, messages))
        return {'summary': '两篇采用不同的目标。[P1 p.1][P2 p.1]',
                'dimensions': [{'dimension': '训练目标', 'values': {
                    'D1': 'attention [P1 p.1]', 'D2': 'masked language modelling [P2 p.1]'}}]}

    model.chat_json = reply
    result = client.post('/api/compare', json={'refs': [a, b], 'aspects': ['训练目标']}).json()['data']
    assert result['_citation_check']['passed']
    assert len(observed) == 1 and observed[0][0] == 'compare'
    content = str(observed[0][1])
    assert all(t in content for t in ('CURRENT_A', 'CURRENT_B', '关注维度：训练目标'))
    assert all(t not in content for t in ('UNSELECTED_RAW_SOURCE', 'OLD_ANALYSIS_MUST_NOT_BECOME_SOURCE'))
    assert {e['paper_id'] for e in result['_citations']} == {a, b}


def test_other_paper_citation_cannot_be_published_in_document_cell(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    ids = [paper(kb, title='A'), paper(kb, title='B')]
    model.chat_json = lambda *a, **k: {'summary': '比较。[P1 p.1][P2 p.1]',
        'dimensions': [{'dimension': '方法', 'values': {'D1': 'WRONG_DOCUMENT [P2 p.1]'}}]}
    result = client.post('/api/compare', json={'refs': ids}).json()['data']
    assert not result['_citation_check']['passed']
    assert 'WRONG_DOCUMENT' not in str(result.get('dimensions'))


def test_material_replacement_during_generation_stops_current_comparison_publication(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    ids = [paper(kb, title='A'), paper(kb, title='B')]
    model.supports_semantic_review = True
    tasks = []

    def replace(*args, **kwargs):
        tasks.append(kwargs.get('task'))
        kb.add_chunks(ids[0], [{'text': 'NEW_CURRENT_MATERIAL replaces the original.', 'page': 2}], level='fulltext')
        return {'summary': 'STALE_COMPARISON [P1 p.1][P2 p.1]'}

    model.chat_json = replace
    result = client.post('/api/compare', json={'refs': ids}).json()['data']
    assert not result['_citation_check']['passed']
    assert 'STALE_COMPARISON' not in result['summary']
    assert tasks == ['compare']  # Do not spend semantic calls on obsolete sources.
