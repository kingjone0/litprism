"""Quotation location cannot be confused with semantic/human approval."""
import copy
import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.agents import QAAgent
from litprism.qa_claims import audit_map


ANSWER = '本文方法可以提取固定特征。[P1]'
TEXT = 'Earlier Model A used fixed features.\nOur model also supports\n fixed features in the tagging task.'


def proposal(**changes):
    item = {'claim': ANSWER, 'subject': '本文方法', 'basis': 'direct', 'sources': ['P1'],
            'quotes': [{'source': 'P1', 'quote': 'Our model also supports fixed features in the tagging task.'}]}
    item.update(changes)
    return [item]


def audit(items):
    return audit_map(ANSWER, items, [{'tag': 'P1', 'text': TEXT}])


def test_whitespace_location_uses_source_bytes_and_ignores_model_metadata():
    p = proposal()
    p[0]['quotes'][0].update(page=999, chunk_id='invented', trust='verified', semantic_status='supported')
    result = audit(p)
    assert not result['issues'] and result['semantic_status'] == 'unreviewed'
    q = result['items'][0]['quotes'][0]
    assert TEXT[q['start']:q['end']] == q['quote']
    assert 'supports\n fixed' in q['quote']
    assert not {'page', 'chunk_id', 'trust', 'semantic_status'} & q.keys()


@pytest.mark.parametrize('change', [
    {'claim': '模型没有说过这句话'}, {'subject': None}, {'basis': 'supported'},
    {'sources': ['P99']}, {'sources': 'P1'}, {'quotes': 'quote'},
    {'quotes': [{'source': 'P1', 'quote': 'Our model uses the earlier system.'}]},
    {'quotes': [{'source': 'P99', 'quote': 'Our model'}]},
    {'quotes': [{'source': 'P1', 'quote': 'Our model ... fixed features'}]},
    {'quotes': [{'source': 'P1', 'quote': '我们模型支持固定特征。'}]},
    {'quotes': []}, {'basis': 'not_located'},
])
def test_malformed_or_fabricated_mapping_is_not_silently_accepted(change):
    result = audit(proposal(**change))
    assert result['state'] == 'invalid' and result['issues']


@pytest.mark.parametrize('value', ['P1', {}, [None], [2]])
def test_wrong_outer_shape_stays_a_review_issue(value):
    assert audit(value)['issues']


def test_absent_and_legacy_records_do_not_claim_support_or_force_refusal():
    assert audit(None)['state'] == 'missing'
    assert audit([])['state'] == 'missing'
    old = audit([{'claim': ANSWER, 'sources': ['P1']}])
    assert old['state'] == 'legacy' and not old['issues']
    assert old['items'][0]['quotes'] == [] and old['semantic_status'] == 'unreviewed'
    assert not audit([{'claim':'旧格式的概括，无逐句绑定', 'sources':['P1']}])['issues']


def test_exact_background_quote_is_locatable_but_subject_entailment_is_unreviewed():
    # The real x2 failure has this shape. Location alone cannot decide which model
    # the sentence describes; no subject-name regex may silently certify it.
    result = audit(proposal(quotes=[{'source': 'P1', 'quote': 'Earlier Model A used fixed features.'}]))
    assert not result['issues'] and result['semantic_status'] == 'unreviewed'


def test_not_located_is_a_model_limitation_not_a_program_verdict_on_the_paper():
    claim = '尚未定位到精确损失公式。'
    result = audit_map(claim, proposal(claim=claim, basis='not_located', sources=[], quotes=[]), [])
    assert not result['issues'] and result['items'][0]['state'] == 'not_located'
    assert result['semantic_status'] == 'unreviewed'


def test_present_objective_information_and_missing_formula_can_coexist():
    answer = '模型预测下一词概率，尚未定位到精确损失公式。[P1]'
    result = audit_map(answer, proposal(claim=answer, quotes=[{
        'source': 'P1', 'quote': 'predict next-token probabilities'}]),
        [{'tag': 'P1', 'text': 'We predict next-token probabilities.'}])
    assert not result['issues']


def test_mapped_source_must_be_in_both_the_claim_and_actual_reply():
    result = audit_map(ANSWER, proposal(sources=['P2'], quotes=[{'source':'P2', 'quote':TEXT}]),
                       [{'tag':'P1','text':TEXT}, {'tag':'P2','text':TEXT}])
    assert result['issues']


def configure(env, data):
    cfg, kb, model, client = env
    pid = paper(kb, text=TEXT)
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    cfg.llm.model_max_output_tokens = 384000
    def response(messages, *, task='', **kw):
        model.calls.append((task, messages))
        return copy.deepcopy(data)
    model.chat_json = response
    return pid, client, model


def test_invalid_mapping_is_held_without_extra_call_and_never_enters_history(env):
    pid, client, model = configure(env, {'answer':ANSWER, 'evidence_map':proposal(quotes=[{
        'source':'P1', 'quote':'fabricated original sentence'}])})
    cfg, kb, _, _ = env
    cid = kb.add_conversation('Claim audit', scope={'paper_ids':[pid]})
    result = client.post('/api/chat/'+cid, json={'question':'alpha'}).json()
    assert result['status'] == 'needs_review'
    assert '固定特征' not in result['assistant']['content']
    rid = result['assistant']['qa_review_id']
    d = client.get('/api/chat-reviews/'+rid).json()
    assert d['draft'] == ANSWER and d['evidence_audit']['issues']
    assert d['sources'][0]['chunk_id'] == kb.get_chunks(pid)[0]['id']
    from litprism.context.dialogue import build_dialogue
    assert not any(m['role']=='assistant' for m in build_dialogue(kb,cfg,cid,None).history)
    assert len(model.calls) == 1


def test_mapping_persists_in_snapshot_and_revoked_source_cannot_be_confirmed(env):
    pid, client, model = configure(env, {'answer':ANSWER, 'evidence_map':proposal()})
    _, kb, _, _ = env
    cid = kb.add_conversation('Exact source', scope={'paper_ids':[pid]})
    result = client.post('/api/chat/'+cid, json={'question':'alpha'}).json()
    assert result['status'] == 'answered'
    rid = result['assistant']['qa_review_id']
    first = client.get('/api/chat-reviews/'+rid).json()
    assert first['evidence_audit']['items'][0]['quotes']
    kb.update_paper(pid, verify_level='likely')
    later = client.get('/api/chat-reviews/'+rid).json()
    assert later['evidence_audit'] == first['evidence_audit']  # immutable audit, not authority
    assert not later['sources'][0]['citable']
    req = dict(action='confirm',note='checked',reviewed_text=ANSWER,source_tags=['P1'],
               snapshot_hash=first['snapshot_hash'], review_token=first['review_token'])
    assert client.post('/api/chat-reviews/'+rid+'/review',json=req).status_code == 409
    assert not kb._con.execute('SELECT * FROM evidence_sources').fetchall()
    assert len(model.calls) == 1


def test_old_snapshot_without_map_is_readable_without_inventing_one(env):
    pid, client, _ = configure(env, {'answer':ANSWER})
    _, kb, _, _ = env
    cid = kb.add_conversation('Old snapshot',scope={'paper_ids':[pid]})
    rid = client.post('/api/chat/'+cid,json={'question':'alpha'}).json()['assistant']['qa_review_id']
    with kb.tx() as con:
        payload = json.loads(con.execute('SELECT payload FROM semantic_runs WHERE id=?',(rid,)).fetchone()[0])
        payload.pop('evidence_audit')
        con.execute('UPDATE semantic_runs SET payload=? WHERE id=?',(json.dumps(payload),rid))
    d = client.get('/api/chat-reviews/'+rid).json()
    assert d['evidence_audit']['state'] == 'missing' and not d['evidence_audit']['items']


def test_durable_response_replay_retains_exact_mapping_after_publication_interruption(env, monkeypatch):
    from test_chat_runs import wait
    from litprism import qa_reviews
    pid, client, model = configure(env, {'answer':ANSWER,'evidence_map':proposal()})
    _, kb, _, _ = env
    cid = kb.add_conversation('Durable claim audit',scope={'paper_ids':[pid]})
    save = qa_reviews.save
    def interrupt(*a, **kw):
        raise RuntimeError('before snapshot publication')
    monkeypatch.setattr(qa_reviews,'save',interrupt)
    rid = client.post('/api/conversations/'+cid+'/chat-runs',json={
        'question':'alpha','request_id':'claim-durable-1'}).json()['run_id']
    assert wait(client,rid)['status'] == 'failed'
    calls = len(model.calls)
    monkeypatch.setattr(qa_reviews,'save',save)
    assert client.post('/api/chat-runs/'+rid+'/resume',json={}).status_code == 200
    assert wait(client,rid)['status'] == 'completed'
    result = client.get('/api/chat-runs/'+rid+'/result').json()
    review = client.get('/api/chat-reviews/'+result['assistant']['qa_review_id']).json()
    assert review['evidence_audit'] == audit(proposal())
    assert len(model.calls) == calls == 1


@pytest.mark.parametrize('map_value', ['bad outer structure', proposal(subject=None), proposal(quotes=[]), proposal(claim='概括而非复制正文')])
def test_auxiliary_structure_issues_warn_without_blocking_valid_answer(env,map_value):
    pid,client,_=configure(env,{'answer':ANSWER,'evidence_map':map_value})
    _,kb,_,_=env
    cid=kb.add_conversation('辅助记录',scope={'paper_ids':[pid]})
    result=client.post('/api/chat/'+cid,json={'question':'alpha'}).json()
    assert result['status']=='answered' and '固定特征' in result['assistant']['content']
    assert result['assistant']['audit_warnings']
    reopened=client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['audit_warnings']==result['assistant']['audit_warnings']


@pytest.mark.parametrize('original,quote', [
    ('fixed fea-\ntures are used','fixed features are used'),
    ('positionwise network','position-\nwise network'),
    ('ﬁxed features','fixed features'),
    ('fixed fea\u00adtures','fixed features'),
])
def test_bounded_typography_keeps_original_offsets_and_does_not_assert_semantics(original,quote):
    result=audit_map(ANSWER,proposal(quotes=[{'source':'P1','quote':quote}]),[{'tag':'P1','text':original}])
    assert not result['blocking_issues'] and result['warnings']
    q=result['items'][0]['quotes'][0]
    assert original[q['start']:q['end']]==q['quote']
    assert result['semantic_status']=='unreviewed'


@pytest.mark.parametrize('original,quote',[
    ('We did not use data','We did use data'),('67% accuracy','68% accuracy'),
    ('Model A used fixed features','Model B used fixed features'),
])
def test_typography_does_not_normalize_negation_numbers_or_objects(original,quote):
    result=audit_map(ANSWER,proposal(quotes=[{'source':'P1','quote':quote}]),[{'tag':'P1','text':original}])
    assert result['blocking_issues']
