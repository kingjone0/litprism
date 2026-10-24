"""Opt-in anchor experiments: snapshot-bound location, never semantic certification."""
import json

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.qa_anchors import build, catalog, resolve
from litprism.qa_claims import audit_map


def source(**changes):
    return dict({'tag': 'P1', 'chunk_id': 'c_first', 'paper_id': 'paper', 'material_id': 'mat_first',
                 'source_kind': 'paper', 'page': 2, 'text': '  First original paragraph.\n\nSecond original paragraph.\n'}, **changes)


def proposed(anchor_id, **quote):
    return [{'claim': '可回溯的转述。[P1]', 'subject': '论文方法', 'basis': 'direct', 'sources': ['P1'],
             'quotes': [dict(source='P1', anchor_id=anchor_id, **quote)]}]


def test_exact_backfill_keeps_ranges_and_stable_identity_without_model_copy():
    s = source()
    anchors = build(s)
    assert len(anchors) == 2
    assert anchors[0]['quote'] == 'First original paragraph.'
    for anchor in anchors:
        assert s['text'][anchor['start']:anchor['end']] == anchor['quote']
        assert resolve(s, anchor['id'])['text_sha256'] == anchor['text_sha256']
    assert build(dict(s, tag='P97')) == anchors
    assert catalog([s])[0]['anchor_id'] == anchors[0]['id']
    assert len(build(source(text='First.\r\n\r\nSecond.'))) == 2
    with pytest.raises(ValueError):build(source(chunk_id=None))


def test_fine_spans_keep_pdf_wraps_abbreviations_decimals_and_exact_coverage():
    text = 'Dr. Smith et al. use 3.14 as in Eq. 2. First method uses atten-\r\ntion. Next sentence supports another point. 中文句子。下一句。'
    s = source(text=text)
    anchors = build(s)
    assert len(anchors) == 5
    assert anchors[0]['quote'] == 'Dr. Smith et al. use 3.14 as in Eq. 2.'
    assert anchors[1]['quote'] == 'First method uses atten-\r\ntion.'
    assert ''.join(a['quote'].replace(' ', '').replace('\r', '').replace('\n', '') for a in anchors) == text.replace(' ', '').replace('\r', '').replace('\n', '')
    assert all(s['text'][a['start']:a['end']] == a['quote'] and a['id'].startswith('qa2_') for a in anchors)


def test_long_sentence_splits_into_marked_adjacent_spans_without_losing_words():
    s = source(text=('long-context ' * 110) + 'ends here.')
    anchors = build(s)
    assert len(anchors) > 1
    assert all(len(a['quote']) <= 360 for a in anchors)
    assert anchors[0]['continues_after'] and not anchors[0]['continues_before']
    assert anchors[-1]['continues_before'] and not anchors[-1]['continues_after']
    assert ' '.join(a['quote'] for a in anchors) == s['text']
    assert all(a['granularity'] == 'fragment' for a in anchors)


def test_old_paragraph_id_resolves_only_original_bytes_but_is_not_in_new_catalog():
    from litprism.qa_anchors import _paragraphs
    s = source(text='First evidence. Second evidence.')
    old = _paragraphs(s)[0]['id']
    assert old not in {a['anchor_id'] for a in catalog([s])}
    assert resolve(s, old)['quote'] == s['text']
    assert resolve(s, old)['granularity'] == 'legacy_paragraph'
    with pytest.raises(ValueError):resolve(dict(s, material_id='changed'), old)


def test_multiple_fine_anchors_remain_separate_in_a_single_claim():
    s = source(text='First fact. Second fact.')
    p = proposed(build(s)[0]['id'])
    p[0]['quotes'].append({'source': 'P1', 'anchor_id': build(s)[1]['id']})
    a = audit_map(p[0]['claim'], p, [s], anchor_trial=True)
    assert not a['blocking_issues']
    assert [q['quote'] for q in a['items'][0]['quotes']] == ['First fact.', 'Second fact.']
    assert a['semantic_status'] == 'unreviewed'


@pytest.mark.parametrize('change', [
    {'chunk_id': 'c_second'}, {'material_id': 'mat_second'}, {'paper_id': 'another'},
    {'page': 3}, {'text': 'First altered paragraph.\n\nSecond original paragraph.\n'},
])
def test_other_source_material_or_content_cannot_reuse_anchor(change):
    original = source()
    old = build(original)[0]['id']
    with pytest.raises(ValueError):resolve(dict(original, **change), old)


def test_anchor_trial_off_rejects_anchor_but_keeps_old_quote_contract():
    s = source()
    p = proposed(build(s)[0]['id'])
    assert audit_map(p[0]['claim'], p, [s])['blocking_issues']
    result = audit_map(p[0]['claim'], p, [s], anchor_trial=True)
    assert not result['blocking_issues'] and result['semantic_status'] == 'unreviewed'
    q = result['items'][0]['quotes'][0]
    assert q['kind'] == 'source_anchor' and q['quote'] == 'First original paragraph.'
    assert result['protocol'] == 'anchors_trial_v2'


@pytest.mark.parametrize('change', [
    {'anchor_id': 'invented'}, {'anchor_id': None}, {'source': 'P99'},
    {'quote': 'A fabricated quote with 99% accuracy.'},
])
def test_forged_anchor_or_bad_quote_remains_blocking(change):
    s = source()
    p = proposed(build(s)[0]['id'])
    p[0]['quotes'][0].update(change)
    assert audit_map(p[0]['claim'], p, [s], anchor_trial=True)['blocking_issues']


def test_valid_position_does_not_certify_which_model_the_claim_describes():
    s = source(text='Earlier model A used fixed features.')
    p = proposed(build(s)[0]['id'])
    p[0]['claim'] = '本文模型B使用固定特征。[P1]'
    a = audit_map(p[0]['claim'], p, [s], anchor_trial=True)
    assert not a['blocking_issues'] and a['semantic_status'] == 'unreviewed'
    assert '不代表对象归属' in a['warnings'][0]


@pytest.mark.parametrize('durable', [False, True])
def test_api_trial_persists_backfilled_bytes_and_keeps_review_revoke_guard(env, durable):
    from litprism.evidence import stored_excerpts
    cfg, kb, model, client = env
    assert cfg.qa.evidence_anchor_trial is False
    cfg.qa.evidence_anchor_trial = True
    pid = paper(kb, text='alphatopic supports the result.\n\nIndependent context.', page=2)
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    def respond(messages, task='', **kwargs):
        model.calls.append((task, messages))
        data = json.loads(messages[-1]['content'].split('【试验定位锚点（程序生成，不是语义结论）】\n')[1])
        a = data[0]
        text = f'原文中的转述结果。[{a["source"]}]'
        return {'answer': text, 'evidence_map': [{'claim': text, 'subject': '结果', 'basis': 'direct',
            'sources': [a['source']], 'quotes': [{'source': a['source'], 'anchor_id': a['anchor_id']}]}]}
    model.chat_json = respond
    cid = kb.add_conversation('隔离锚点试验', scope={'paper_ids': [pid]})
    if durable:
        from test_chat_runs import wait
        rid = client.post('/api/conversations/'+cid+'/chat-runs', json={
            'question': 'alpha', 'request_id': 'anchor-backfill-trial'}).json()['run_id']
        assert wait(client, rid)['status'] == 'completed'
        response = client.get('/api/chat-runs/'+rid+'/result').json()
    else:
        response = client.post('/api/chat/'+cid, json={'question': 'alpha'}).json()
    assert response['status'] == 'answered'
    review = client.get('/api/chat-reviews/'+response['assistant']['qa_review_id']).json()
    q = review['evidence_audit']['items'][0]['quotes'][0]
    assert q['quote'] == 'alphatopic supports the result.' and q['material_id'] == kb.get_paper(pid)['current_material_id']
    assert len(model.calls) == 1
    assert '【片段】' in model.calls[0][1][-1]['content']
    assert response['assistant']['retrieval']['evidence_protocol'] == 'anchors_trial_v2'
    kb.update_paper(pid, verify_level='unchecked')
    assert not client.get('/api/chat-reviews/'+review['id']).json()['sources'][0]['citable']
    assert client.post('/api/chat-reviews/'+review['id']+'/review', json={
        'action': 'confirm', 'note': '不得放行失效来源', 'reviewed_text': '结果 [P1]', 'source_tags': ['P1'],
        'snapshot_hash': review['snapshot_hash'], 'review_token': review['review_token']}).status_code == 409


def test_durable_resume_rejects_changed_trial_protocol_without_new_model_call(env, monkeypatch):
    from litprism import qa_reviews
    from test_chat_runs import wait
    cfg, kb, model, client = env
    pid = paper(kb)
    cid = kb.add_conversation('协议变更', scope={'paper_ids': [pid]})
    saved = qa_reviews.save
    def fail(*args, **kwargs):raise RuntimeError('publication interrupted')
    monkeypatch.setattr(qa_reviews, 'save', fail)
    rid = client.post('/api/conversations/'+cid+'/chat-runs', json={
        'question': 'alpha', 'request_id': 'anchor-protocol-before'}).json()['run_id']
    assert wait(client, rid)['status'] == 'failed'
    count = len(model.calls)
    monkeypatch.setattr(qa_reviews, 'save', saved)
    cfg.qa.evidence_anchor_trial = True
    assert client.post('/api/chat-runs/'+rid+'/resume', json={}).status_code == 200
    assert wait(client, rid)['status'] == 'stale' and len(model.calls) == count


@pytest.mark.parametrize('bad', ['yes', 1, None])
def test_trial_flag_requires_explicit_boolean(bad):
    from litprism.config import Config, _validate
    cfg = Config();cfg.qa.evidence_anchor_trial = bad
    with pytest.raises(ValueError):_validate(cfg)


def test_shadow_comparison_does_not_replace_unlocated_quotes_with_nearby_anchors():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'tools/anchor_trial_acceptance.py'
    spec = importlib.util.spec_from_file_location('anchor_trial_acceptance', path)
    tool = importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    s = source()
    data = [
        {'claim': '可回溯的转述。[P1]', 'subject': '方法', 'basis': 'direct', 'sources': ['P1'],
         'quotes': [{'source': 'P1', 'quote': 'First original paragraph.'}]},
        {'claim': '可回溯的转述。[P1]', 'subject': '方法', 'basis': 'direct', 'sources': ['P1'],
         'quotes': [{'source': 'P1', 'quote': 'A fabricated value of 99%.'}]},
    ]
    report = tool.compare('可回溯的转述。[P1]', data, [s])
    assert report['converted_located_quotes'] == 1
    assert report['anchor_path']['blocking_issues'] == report['quote_path']['blocking_issues']
    assert report['semantic_status'] == 'unreviewed' and not report['new_generation']
