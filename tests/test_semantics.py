# ruff: noqa: F811
"""Semantic release, distant-batch review, and immutable human review boundaries."""
import copy
import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.config import Config
from litprism.extract import analyze_paper
from litprism.knowledge import get_source, search_knowledge
from litprism.semantics import make_units, reconcile, review_units, run_review, supported_fields

SOURCE = 'Alpha perplexity on PTB is 20.5; lower perplexity is better. SuperGLUE Average is 71.8; RTE Accuracy is 69.0.'
EXCERPT = {'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 19, 'paper_title': 'Study', 'text': SOURCE}


class Reviewer:
    supports_semantic_review = True
    name = 'controlled-reviewer'

    def __init__(self, verdict='supported', quote=SOURCE, differences=None):
        self.verdict, self.quote, self.differences = verdict, quote, differences or []
        self.calls = []

    def chat_json(self, messages, task='', **kwargs):
        payload = json.loads(messages[-1]['content'])
        self.calls.append((task, payload))
        if task == 'semantic_cross_review':
            return {'differences': self.differences}
        return {'reviews': [{'id': c['id'], 'verdict': self.verdict, 'reason': 'Controlled evidence check',
                            'quotes': [{'tag': 'P1', 'quote': self.quote}], 'facts': []}
                           for c in payload['claims']]}


def units(text='Alpha perplexity is 20.5 [P1 p.19]。'):
    return make_units([('key_conclusions', text, ['P1 p.19'])], [EXCERPT], 1)


@pytest.mark.parametrize('quote', ['fabricated evidence that does not occur', '', 'too short'])
def test_supported_without_literal_source_remains_pending(quote):
    result = review_units(units(), [EXCERPT], Config(), Reviewer(quote=quote))
    assert result[0]['status'] == 'needs_review'
    assert not supported_fields({'items': result})


def test_quote_matching_does_not_conflate_superscripts():
    e = {**EXCERPT, 'text':'Energy measure is 12 m² in this trial.'}
    model = Reviewer(quote='Energy measure is 12 m2 in this trial.')
    result = review_units(units(),[e],Config(),model)
    assert result[0]['status'] == 'needs_review'


def test_corrupt_json_keys_cannot_pass_on_valid_citation_syntax():
    from litprism.extract import _validate_fields
    result, issues = _validate_fields({'research_question':'Valid-looking text [P1 p.19]',
                                      '等 Neutral <div>草稿': 'corrupt'},[EXCERPT])
    assert issues and not result['research_question'] and not result['field_evidence']


def test_metrics_direction_table_columns_and_units_are_not_silently_released():
    cfg = Config()
    for text in ['PTB perplexity improves by 20.5 percentage points [P1]。',
                 'SuperGLUE Average is 69.0 [P1]。', 'Higher perplexity is better [P1]。']:
        model = Reviewer('supported' if 'percentage' in text else 'contradicted')
        report = run_review([([('key_conclusions', text, ['P1'])], [EXCERPT])], [EXCERPT], cfg, model)
        assert report['pending'] and not report['passed'] and not supported_fields(report)
        assert report['items'][0]['text'] == text
    report = run_review([([('key_conclusions', 'SuperGLUE Average is 71.8 [P1]。', ['P1'])], [EXCERPT])], [EXCERPT], cfg, Reviewer())
    assert report['passed'] and '71.8' in supported_fields(report)['key_conclusions']


def test_no_capability_and_oversize_review_never_claim_pass():
    assert run_review([], [], Config(), object())['status'] == 'not_run'
    cfg = Config()
    cfg.context.window_tokens = cfg.context.output_reserve_tokens+1
    model = Reviewer()
    result = review_units(units(), [EXCERPT], cfg, model)
    assert not model.calls and result[0]['status'] == 'needs_review'


def test_sentence_citations_remain_with_claim_and_missing_result_is_pending():
    result = units('第一条 [P1]。第二条。[P1]')
    assert all(i['candidate_tags'] == ['P1'] for i in result)
    class Missing(Reviewer):
        def chat_json(self, *args, **kwargs):
            return {'reviews': []}
    assert all(i['status'] == 'needs_review' for i in review_units(result, [EXCERPT], Config(), Missing()))
    assert len(units('第一条。 [P1]')) == 1
    assert not units('[P1]')


def test_semicolon_does_not_detach_local_sources_or_fan_out_field_refs():
    sources=[{**EXCERPT,'tag':f'P{i}'} for i in range(1,7)]
    result=make_units([('methodology','First clause；second clause [P1][P2]。Next sentence [P3]。',
                       [f'P{i}' for i in range(1,7)])],sources,1)
    assert len(result)==2
    assert result[0]['candidate_tags']==['P1','P2'] and 'First clause' in result[0]['text']
    assert result[1]['candidate_tags']==['P3']


def test_excessive_source_fanout_does_not_silently_truncate_or_call_api():
    sources=[{**EXCERPT,'tag':f'P{i}'} for i in range(1,5)]
    items=make_units([('methodology','Compound statement',[f'P{i}' for i in range(1,5)])],sources,1)
    model=Reviewer()
    review_units(items,sources,Config(),model)
    assert not model.calls and len(items[0]['candidate_tags'])==4 and items[0]['status']=='needs_review'


def test_review_groups_bound_output_not_just_input_size():
    items = units()*14
    for i,item in enumerate(items):
        item['id'] = f'cl{i}'
    model = Reviewer()
    review_units(items,[EXCERPT],Config(),model)
    assert [len(p['claims']) for _,p in model.calls] == [1]*14


@pytest.mark.parametrize('selected', [['P1'], ['UNKNOWN'], [], 'P1'])
def test_evidence_selection_uses_stored_text_and_rejects_unknown_ids(selected):
    class Selector(Reviewer):
        def chat_json(self, messages, **kwargs):
            cid = json.loads(messages[-1]['content'])['claims'][0]['id']
            return {'reviews':[{'id':cid,'verdict':'supported','reason':'checked','evidence':selected,
                                'quotes':[{'tag':'P1','quote':SOURCE}]}]}
    item = review_units(units(),[EXCERPT],Config(),Selector())[0]
    assert (item['status']=='machine_supported') == (selected==['P1'])
    if item['status']=='machine_supported':
        assert item['quotes'][0]['quote']==SOURCE and item['quotes'][0]['chunk_id']=='chunk1'


def test_shared_call_budget_across_batches_keeps_unreviewed_claims_pending(monkeypatch):
    monkeypatch.setattr('litprism.semantics.MAX_REVIEW_CALLS',2)
    model = Reviewer()
    report = run_review([([('key_conclusions','Claim [P1]。',['P1'])],[EXCERPT])]*5,[EXCERPT],Config(),model)
    assert report['review_calls']==2 and report['max_review_calls']==2
    assert len([t for t,_ in model.calls if t=='semantic_review'])==2
    assert report['pending']>=3 and not report['passed']


def test_output_limit_stops_paid_reviews_and_preserves_pending_candidates():
    from litprism.llm import OutputLimitError
    class Limited(Reviewer):
        def chat_json(self, messages, task='', **kwargs):
            self.calls.append(task)
            raise OutputLimitError('controlled output budget')
    model = Limited()
    fields = [('key_conclusions', 'Statement [P1]。', ['P1'])]*15
    report = run_review([(fields,[EXCERPT])]*3,[EXCERPT],Config(),model)
    assert model.calls == ['semantic_review']
    assert len(report['items']) == 45 and report['pending'] == 45
    assert not report['passed'] and not report['coverage']['complete'] and not supported_fields(report)


def test_cross_output_limit_stops_all_later_tile_pairs():
    from litprism.llm import OutputLimitError
    class Limited(Reviewer):
        def chat_json(self, *args, **kwargs):
            self.calls.append(kwargs['task'])
            raise OutputLimitError('controlled')
    cfg = Config()
    cfg.context.window_tokens = cfg.context.output_reserve_tokens+3000
    items = []
    for i in range(12):
        item = units()[0]
        item.update(id=f'cl{i}',text='x '*1000,status='machine_supported')
        items.append(item)
    model = Limited()
    _, coverage = reconcile(items,cfg,model)
    assert model.calls == ['semantic_cross_review'] and not coverage['complete']
    assert all(i['status'] == 'needs_review' for i in items)


def test_value_index_retains_both_sides_but_separates_conditions():
    items = units()+units()
    for i, unit in enumerate(items):
        unit.update(id=f'cl{i}', batch=i+1, status='machine_supported')
        unit['facts'] = [{'subject':'Alpha', 'metric':'perplexity', 'conditions':'PTB',
                         'value':str(20+i), 'unit':'', 'claim_id':unit['id'], 'source':{'paper_id':'paper1'}}]
    differences, coverage = reconcile(items, Config(), Reviewer())
    assert coverage['complete'] and differences[0]['claim_ids'] == ['cl0', 'cl1']
    assert all(u['status'] == 'needs_review' for u in items)
    items[1]['facts'][0]['conditions'] = 'another split'
    assert not reconcile(items, Config(), Reviewer())[0]


def test_all_pairs_include_distant_batches_and_report_incomplete_budget():
    cfg = Config()
    cfg.context.window_tokens = cfg.context.output_reserve_tokens+4200
    items = []
    for i in range(18):
        unit = units()[0]
        unit.update(id=f'cl{i}', batch=i+1, text=('claim '+str(i)+' ') * 100, status='machine_supported')
        items.append(unit)
    model = Reviewer()
    _, coverage = reconcile(items, cfg, model)
    assert any({'cl0', 'cl17'} <= {c['id'] for c in p['claims']} for _, p in model.calls)
    assert coverage['calls'] <= 40
    if not coverage['complete']:
        assert coverage['skipped_claim_ids']
        assert all(u['status'] == 'needs_review' for u in items if u['id'] in coverage['skipped_claim_ids'])


def test_unknown_global_claim_id_fails_closed():
    with pytest.raises(ValueError, match='未知陈述'):
        reconcile(units()+units('Other [P1]。'), Config(), Reviewer(differences=[{'claim_ids':['unknown','bogus'], 'reason':'test'}]))


def seed_report(kb):
    pid = paper(kb, text=SOURCE*4)
    cid = kb.get_chunks(pid)[0]['id']
    claim = units()[0]
    claim['quotes'] = [{'tag':'P1','chunk_id':cid,'page':19,'quote':SOURCE}]
    payload = {'_semantic_review': {'items':[claim], 'differences':[], 'status':'needs_review', 'pending':1},
               '_citations':[{'tag':'P1','chunk_id':cid}]}
    kb.save_analysis(pid, payload)
    return pid, claim, payload


@pytest.mark.parametrize('run', [False, True])
def test_human_review_is_version_bound_editable_and_revocable(env, run):
    _, kb, _, client = env
    pid, claim, payload = seed_report(kb)
    if run:
        kb._con.execute('INSERT INTO semantic_runs VALUES (?,?,?,?)', ('run1','Comparison',json.dumps(payload),'2026-10-18'))
        kb._con.commit()
        endpoint = '/api/semantic-runs/run1'
    else:
        endpoint = f'/api/paper/{pid}/semantic'
    data = client.get(endpoint).json()
    assert data['sources'][0]['chunk_id'] == kb.get_chunks(pid)[0]['id']
    req = {'action':'confirm', 'note':'已查对原文与条件', 'reviewed_text':'humanmarker PTB perplexity is 20.5.', 'analysis_hash':'stale'}
    url = endpoint+'/'+claim['id']+'/review'
    assert client.post(url,json=req).status_code == 409
    req['analysis_hash'] = data['analysis_hash']
    assert not search_knowledge(kb, 'humanmarker', 5)
    response = client.post(url,json=req)
    assert response.status_code == 200, response.text
    kid = response.json()['knowledge_id']
    assert get_source(kb,kid)['status'] == 'manual_confirmed' and search_knowledge(kb, 'humanmarker',5)
    assert kb.get_paper(pid)['verify_level'] == 'verified'
    assert kb.get_analysis(pid)['payload'] == payload
    assert client.post(url,json=req).status_code == 409
    req.update(action='revoke',note='适用条件需要重核')
    assert client.post(url,json=req).status_code == 200
    assert get_source(kb,kid)['status'] == 'revoked' and not search_knowledge(kb,'humanmarker',5)
    assert len(client.get(endpoint).json()['reviews']) == 2


def test_source_revoked_blocks_semantic_approval(env):
    _, kb, _, client = env
    pid, claim, _ = seed_report(kb)
    endpoint = f'/api/paper/{pid}/semantic'
    digest = client.get(endpoint).json()['analysis_hash']
    kb._con.execute("UPDATE papers SET verify_level='unchecked' WHERE id=?",(pid,))
    kb._con.commit()
    assert client.post(endpoint+'/'+claim['id']+'/review',json={'action':'confirm','note':'test','reviewed_text':'test','analysis_hash':digest}).status_code == 409


def test_semantic_transport_failure_preserves_previous_analysis_and_concepts(env):
    cfg,kb,model,_ = env
    pid = paper(kb)
    kb.save_analysis(pid, {'old':'frozen'})
    kb.add_concepts(pid,[{'term':'old','definition':'frozen'}])
    before = copy.deepcopy(kb.list_concepts(pid))
    model.supports_semantic_review = True
    original = model.chat_json
    def failing(messages, task='', **kwargs):
        if task == 'semantic_review':
            raise TimeoutError('controlled failure')
        return original(messages,task=task,**kwargs)
    model.chat_json = failing
    with pytest.raises(TimeoutError):
        analyze_paper(kb,cfg,model,pid,force=True,with_concepts=False)
    assert kb.get_analysis(pid)['payload'] == {'old':'frozen'} and kb.list_concepts(pid) == before
