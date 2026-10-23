"""Invalid citations are audit drafts, never normal answers or follow-up facts."""
import json

import pytest
from test_workflow_guards import env as workflow_env  # noqa: F401
from test_workflow_guards import paper

from litprism.context.dialogue import build_dialogue

env = workflow_env

@pytest.mark.parametrize('text', ['BAD_DRAFT [P99 p.1]', 'BAD_DRAFT [P1 p.99]', 'BAD_DRAFT without sources'])
def test_chat_blocks_invalid_answer_and_reopen_preserves_status(env, text):
    _, kb, model, client = env
    pid = paper(kb)
    model.text = text
    cid = kb.add_conversation('Guard', scope={'paper_ids': [pid]})
    data = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert data['status'] == 'invalid_citation'
    assert not data['insufficient_evidence']  # not an honest refusal
    assert not data['sources'] and not data['assistant']['source_links']
    assert 'BAD_DRAFT' not in data['assistant']['content']
    stored = kb.get_messages(cid)[-1]
    assert json.loads(stored['meta'])['blocked_answer'] == text
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['status'] == 'invalid_citation' and 'BAD_DRAFT' not in reopened['content']


def test_invalid_legacy_assistant_is_hidden_and_not_history(env):
    cfg, kb, _, client = env
    cid = kb.add_conversation('Legacy')
    kb.append_message(cid, 'assistant', 'LEGACY_BAD [P99 p.7]', citation_check={'passed': False})
    assert not build_dialogue(kb, cfg, cid, None).history
    loaded = client.get(f'/api/conversations/{cid}/messages').json()['items'][0]
    assert 'LEGACY_BAD' not in loaded['content'] and loaded['status'] == 'invalid_citation'


def test_scope_snapshot_survives_scope_change(env):
    _, kb, _, client = env
    a = paper(kb, title='A')
    b = paper(kb, title='B')
    cid = kb.add_conversation('Scopes', scope={'paper_ids': [a]})
    client.post('/api/chat/'+cid, json={'question': 'alphatopic'})
    client.patch('/api/conversations/'+cid, json={'scope': {'paper_ids': [b]}})
    loaded = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert loaded['scope_snapshot']['paper_ids'] == [a]


def test_error_is_not_evidence_refusal_and_counts_expansion_plus_failure(env):
    _, kb, model, client = env
    paper(kb)
    def fail(messages, task='', **kwargs):
        model.last_usage = {'prompt_tokens': 7, 'completion_tokens': 2}
        if task == 'qa':
            raise RuntimeError('service unavailable')
        return {'keywords': ['alphatopic']}
    model.chat_json = fail
    cid = kb.add_conversation('Failure')
    reply = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert reply['status'] == 'error' and not reply['insufficient_evidence']
    assert reply['usage']['calls'] == 2 and reply['usage']['failed_calls'] == 1
    assert reply['usage']['input'] == 14 and reply['usage']['output'] == 4
    assert not reply['sources']


def test_reopened_answer_rechecks_current_source_trust(env):
    _, kb, _, client = env
    pid = paper(kb)
    cid = kb.add_conversation('Revoked', scope={'paper_ids': [pid]})
    assert client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()['status'] == 'answered'
    kb.delete_paper(pid)
    msg = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert msg['status'] == 'invalid_citation'
    assert '结论 [P1 p.1]' not in msg['content']


@pytest.mark.parametrize('reference,expected', [('P1 p.1', 'refused'), ('P99 p.1', 'invalid_citation')])
def test_refusal_keeps_only_checked_explanation_sources_and_rechecks_on_reopen(env, reference, expected):
    cfg, kb, model, client = env
    pid = paper(kb)
    def respond(messages, task='', **kwargs):
        return {'answer': f'片段不足，已有背景 [{reference}]', 'insufficient_evidence': True} if task == 'qa' else {}
    model.chat_json = respond
    cid = kb.add_conversation('Refusal evidence', scope={'paper_ids': [pid]})
    reply = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert reply['status'] == expected
    if expected == 'invalid_citation':
        assert not reply['assistant']['source_links'] and not reply['sources']
        return
    links = reply['assistant']['source_links']
    assert links and links[0]['tag'] == 'P1'
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['status'] == 'refused' and reopened['source_links'] == links
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
    kb.delete_paper(pid)
    invalid = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert invalid['status'] == 'invalid_citation' and '已有背景' not in invalid['content']
