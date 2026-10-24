"""Server-owned source records are distinct from academic raw quotations."""
import re

import pytest
from test_workflow_guards import env  # noqa: F401

from litprism.qa_claims import audit_map
from litprism.qa_evidence import source_metadata
from litprism.qa_retrieval import material_text


TIME = '2026-10-24T03:04:05+00:00'


def web():
    return {'tag': 'P1', 'source_kind': 'web', 'text': 'BERT uses bidirectional representations.',
            'title': 'BERT', 'url': 'https://arxiv.org/abs/1810.04805', 'acquired_at': TIME,
            'web_grant_id': 'grant', 'trust_level': 'pending'}


def audit(source, quote, basis='direct'):
    answer = '本轮来源的获取时间与论文年份不同。[P1]'
    return audit_map(answer, [{'claim': answer, 'subject': '来源记录', 'basis': basis,
        'sources': ['P1'], 'quotes': [{'source': 'P1', 'quote': quote,
            'kind': 'program_metadata', 'field': 'invented_field'}]}], [source])


@pytest.mark.parametrize('basis', ['direct', 'metadata'])
def test_recorded_time_is_located_as_program_data_not_paper_prose(basis):
    s = web()
    result = audit(s, '获取时间：'+TIME, basis)
    assert not result['blocking_issues'] and result['warnings']
    quote = result['items'][0]['quotes'][0]
    assert quote['kind'] == 'program_metadata' and quote['field'] == 'acquired_at'
    assert quote['quote'] not in s['text'] and 'start' not in quote and 'end' not in quote
    assert result['semantic_status'] == 'unreviewed'
    assert quote['quote'] in material_text([s])


def test_historical_label_uses_actual_dependency_state_and_preserves_knowledge_text():
    s = dict(web(), source_kind='knowledge', url='', dependency_status={'state': 'historical'},
             text='人工核对后的架构知识。')
    quote = source_metadata(s)['source_context']
    result = audit(s, quote)
    assert not result['blocking_issues']
    assert result['items'][0]['quotes'][0]['field'] == 'source_context'
    assert '历史版本依据' in quote and quote in material_text([s])
    assert s['text'] == '人工核对后的架构知识。'
    current = dict(s, dependency_status={'state': 'current'})
    assert audit(current, quote)['blocking_issues']


@pytest.mark.parametrize('quote', [
    '获取时间：2027-10-24T03:04:05+00:00', TIME, '历史版本依据，不代表当前材料结论',
    'BERT reports 99% accuracy.',
])
def test_forged_metadata_fields_and_partial_values_do_not_bypass_location(quote):
    assert audit(web(), quote)['blocking_issues']


def test_paper_raw_quote_cannot_be_reclassified_by_model_supplied_metadata():
    source = dict(web(), source_kind='paper')
    assert not source_metadata(source)
    assert audit(source, '获取时间：'+TIME)['blocking_issues']
    assert audit(web(), 'BERT uses bidirectional representations.', basis='metadata')['blocking_issues']


def test_public_web_reply_keeps_metadata_record_and_review_path(env, monkeypatch):
    from litprism.web import evidence_routes
    from litprism.knowledge import create_source
    _, kb, model, client = env
    monkeypatch.setattr(evidence_routes, 'discover_web', lambda *a: {'items': [create_source(
        kb, kind='web', title='BERT', content='alphatopic BERT evidence.', url='https://arxiv.org/abs/1810.04805')]})
    def response(messages, task='', **kwargs):
        model.calls.append((task, messages))
        if task != 'qa':
            return {'terms': ['alphatopic']}
        tag, stamp = re.search(r'\[(P\d+)\] 本轮搜索摘要[^\n]*获取时间：([^\s]+)', messages[-1]['content']).groups()
        text = f'本轮网页摘要的获取时间为 {stamp}。[{tag}]'
        return {'answer': text, 'evidence_map': [{'claim': text, 'subject': '来源获取记录',
            'basis': 'direct', 'sources': [tag], 'quotes': [{'source': tag, 'quote': '获取时间：'+stamp}]}]}
    model.chat_json = response
    cid = kb.add_conversation('程序记录', scope={'paper_ids': []})
    reply = client.post('/api/chat/'+cid, json={'question': 'alphatopic 获取时间', 'web_search': True}).json()
    assert reply['status'] == 'answered' and not reply['assistant']['audit_warnings']
    assert reply['assistant']['audit_notices']
    review = client.get('/api/chat-reviews/'+reply['assistant']['qa_review_id']).json()
    assert review['evidence_audit']['items'][0]['quotes'][0]['kind'] == 'program_metadata'
    reopened = client.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert reopened['status'] == 'answered' and reopened['qa_review_id']
