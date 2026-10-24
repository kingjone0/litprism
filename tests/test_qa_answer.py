"""The displayed answer and claim record share text; old paraphrases remain unknown."""
import copy

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.qa_answer import compile_answer, bind
from litprism.qa_claims import audit_map


def unit(text='论文方法使用注意力。[P1]'):
    return {'text': text, 'subject': '本文方法', 'basis': 'direct', 'sources': ['P1'],
            'quotes': [{'source': 'P1', 'quote': 'Our method uses attention.'}]}


def test_units_keep_literal_body_offsets_even_with_repeated_unicode_and_ignore_model_ids():
    u = unit()
    u.update(claim='模型另写的错误概括', id='invented', start=999, answer_binding={'state': 'supported'})
    raw = {'answer_units': [u, copy.deepcopy(u)]}
    before = copy.deepcopy(raw)
    answer, mapping, bindings, protocol = compile_answer(raw)
    assert raw == before and protocol == 'answer_units_v1'
    assert mapping[0]['claim'] == mapping[1]['claim'] == u['text']
    assert bindings[0]['span']['id'] != bindings[1]['span']['id']
    audit = audit_map(answer, mapping, [{'tag': 'P1', 'text': 'Our method uses attention.'}],
                      answer_bindings=bindings, answer_protocol=protocol)
    for item in audit['items']:
        b = item['answer_binding']
        assert b['method'] == 'answer_units' and b['state'] == 'bound'
        assert answer[b['span']['start']:b['span']['end']] == item['claim'] == b['span']['text']
    assert audit['semantic_status'] == 'unreviewed' and not audit['issues']


@pytest.mark.parametrize('bad', [None, 'text', [], [None], [{'text': ' '}], [{'text': 7}]])
def test_invalid_units_do_not_fall_back_to_an_independent_answer(bad):
    with pytest.raises(ValueError):
        compile_answer({'answer_units': bad, 'answer': 'an independently valid answer'})


@pytest.mark.parametrize('other', [{'answer': 'a conflicting answer'}, {'evidence_map': []}])
def test_conflicting_parallel_text_or_map_is_not_silently_selected(other):
    with pytest.raises(ValueError):
        compile_answer({'answer_units': [unit()], **other})


def test_old_literal_record_binds_but_paraphrase_and_ambiguous_occurrence_do_not():
    answer = '本文方法使用注意力。[P1]'
    old = {'answer': answer, 'evidence_map': [{'claim': answer}]}
    text, mapping, bindings, protocol = compile_answer(old)
    assert bindings is None and mapping == old['evidence_map'] and protocol == 'legacy_answer_v1'
    assert bind(text, answer)['state'] == 'bound'
    assert bind(text, '作者提出了一种注意力方法。[P1]')['state'] == 'unmatched'
    assert bind(text+'\n'+text, text)['state'] == 'ambiguous'
    assert bind('A  B [P1]', 'A B [P1]')['state'] == 'unmatched'
    assert bind('aaaaa', 'aaa')['state'] == 'ambiguous'


@pytest.mark.parametrize('durable', [False, True])
def test_api_units_persist_exact_answer_and_fine_anchor_with_source_revoke_guard(env, durable):
    import json
    from test_chat_runs import wait
    cfg, kb, model, client = env
    pid = paper(kb, text='alphatopic original source. Other unrelated context.', page=2)
    cfg.qa.evidence_anchor_trial = True
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    def respond(messages, task='', **kwargs):
        model.calls.append((task, messages))
        catalog = json.loads(messages[-1]['content'].split('【试验定位锚点（程序生成，不是语义结论）】\n')[1])
        selected = next(a for a in catalog if 'original source.' in a['preview'])
        u = unit('原文中的实际方法。[P1]')
        u['quotes'] = [{'source': selected['source'], 'anchor_id': selected['anchor_id']}]
        return {'answer_units': [u]}
    model.chat_json = respond
    cid = kb.add_conversation('回答单元', scope={'paper_ids': [pid]})
    if durable:
        rid = client.post('/api/conversations/'+cid+'/chat-runs', json={
            'question': 'alphatopic', 'request_id': 'units-fine-anchor'}).json()['run_id']
        assert wait(client, rid)['status'] == 'completed'
        response = client.get('/api/chat-runs/'+rid+'/result').json()
    else:
        response = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert response['status'] == 'answered'
    assert response['assistant']['content'].startswith('原文中的实际方法。[P1]')
    review = client.get('/api/chat-reviews/'+response['assistant']['qa_review_id']).json()
    item = review['evidence_audit']['items'][0]
    assert review['evidence_audit']['answer_protocol'] == 'answer_units_v1'
    assert item['answer_binding']['span']['text'] == review['draft']
    assert item['quotes'][0]['quote'] == 'alphatopic original source.'
    assert item['quotes'][0]['anchor_version'] == 2
    assert len(model.calls) == 1
    old_audit = copy.deepcopy(review['evidence_audit'])
    kb.update_paper(pid, verify_level='unchecked')
    invalid = client.get('/api/chat-reviews/'+review['id']).json()
    assert invalid['evidence_audit'] == old_audit and not invalid['sources'][0]['citable']
    assert client.post('/api/chat-reviews/'+review['id']+'/review', json={
        'action': 'confirm', 'note': '不能绕过来源撤销', 'reviewed_text': review['draft'], 'source_tags': ['P1'],
        'snapshot_hash': review['snapshot_hash'], 'review_token': review['review_token']}).status_code == 409


def test_new_units_do_not_hide_invented_source_quotes():
    u = unit()
    u['quotes'][0]['quote'] = 'An invented 99% result.'
    answer, mapping, bindings, protocol = compile_answer({'answer_units': [u]})
    a = audit_map(answer, mapping, [{'tag': 'P1', 'text': 'Our method uses attention.'}],
                  answer_bindings=bindings, answer_protocol=protocol)
    assert a['items'][0]['answer_binding']['state'] == 'bound'
    assert a['blocking_issues'] and a['semantic_status'] == 'unreviewed'
