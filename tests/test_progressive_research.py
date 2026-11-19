"""One research journey across durable QA, selective review and fresh retrieval.

Controlled model responses verify orchestration, not real-model semantic quality.
"""
import json
import time

from test_workflow_guards import env  # noqa: F401

from litprism.context.dialogue import build_dialogue
from litprism.knowledge import get_source, search_knowledge


def ask(client, cid, question, key):
    req = {'question': question, 'request_id': key, 'web_search': False}
    response = client.post(f'/api/conversations/{cid}/chat-runs', json=req)
    assert response.status_code == 200, response.text
    rid = response.json()['run_id']
    for _ in range(300):
        state = client.get('/api/chat-runs/'+rid).json()
        if not state['active']:
            break
        time.sleep(.01)
    assert state['status'] == 'completed', (state, client.get('/api/tasks/'+state['task_id']).json())
    result = client.get('/api/chat-runs/'+rid+'/result')
    assert result.status_code == 200, result.text
    return rid, req, result.json()


def test_followup_select_confirm_retrieve_and_revoke_through_persistent_web(request):
    cfg, kb, model, client = request.getfixturevalue('env')
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1_000_000
    cfg.qa.evidence_anchor_trial = True
    pid = kb.add_paper(title='Progressive research fixture', verify_level='verified')
    kb.add_chunks(pid, [
        {'text': 'progressfixture uses averaged word representations.', 'page': 1},
        {'text': 'progressfixture uses hierarchical softmax for efficient prediction.', 'page': 2},
    ], level='fulltext')
    assert kb.get_analysis(pid) is None  # Initial reading is not a QA prerequisite.
    cid = kb.add_conversation('Follow-up to knowledge')

    def response(messages, task='', **kwargs):
        model.calls.append((task, messages))
        if task == 'expand':
            return {'terms': ['reviewedprediction' if 'reviewedprediction' in str(messages) else 'progressfixture']}
        assert task == 'qa'
        content = messages[-1]['content']
        question = content.split('【问题】', 1)[1].split('\n', 1)[0]
        anchors, _ = json.JSONDecoder().raw_decode(
            content.split('【试验定位锚点（程序生成，不是语义结论）】\n', 1)[1])

        def unit(phrase, text):
            choices = [a for a in anchors if phrase in a['preview']]
            assert choices, (phrase, [a['preview'] for a in anchors])
            a = choices[0]
            return {'text': text.format(tag=a['source']), 'subject': 'progressfixture',
                    'basis': 'direct', 'sources': [a['source']],
                    'quotes': [{'source': a['source'], 'anchor_id': a['anchor_id']}]}

        if 'reviewedprediction' in question:
            return {'answer_units': [unit('reviewedprediction', '重新检索所得知识说明预测使用层次 softmax。[{tag}]')]}
        if '继续' in question:
            return {'answer_units': [
                unit('hierarchical softmax', '预测使用层次 softmax。[{tag}]'),
                unit('averaged word', '另一个未选入库的描述：平均词表示。[{tag}]'),
            ]}
        return {'answer_units': [
            unit('averaged word', '模型使用平均词表示。[{tag}]'),
            {'text': '预测加速机制尚未展开，可以继续追问。', 'subject': '预测机制',
             'basis': 'not_located', 'sources': [], 'quotes': []},
        ], 'insufficient_evidence': True}

    model.chat_json = response
    _, _, first = ask(client, cid, 'progressfixture 采用什么表示，预测如何加速？', 'progressive_initial')
    assert first['status'] == 'answered'
    first_review = client.get('/api/chat-reviews/'+first['assistant']['qa_review_id']).json()
    assert first_review['review_mode'] == 'unreviewed'
    assert not kb._con.execute("SELECT 1 FROM evidence_sources WHERE kind='knowledge'").fetchone()

    run_id, req, followup = ask(client, cid, '继续解释它的预测加速，并给出原文。', 'progressive_followup')
    assert followup['status'] == 'answered'
    rid = followup['assistant']['qa_review_id']
    parent = client.get('/api/chat-reviews/'+rid).json()
    assert len(parent['units']) == 2
    raw = kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0]
    uid = parent['units'][0]['id']
    endpoint = '/api/chat-reviews/'+rid+'/units/'+uid
    detail = client.get(endpoint).json()
    tag = detail['sources'][0]
    context = client.get(endpoint+'/sources/'+tag).json()
    anchor = next(a for a in context['anchors'] if 'efficient prediction' in a['quote'])
    request = {'action': 'confirm', 'note': '离线控制：对照本条原文，仅选取预测机制。',
               'reviewed_text': 'reviewedprediction 预测使用层次 softmax。['+tag+']',
               'evidence': [{'source': tag, 'anchor_id': anchor['id']}],
               'snapshot_hash': detail['snapshot_hash'], 'review_token': detail['review_token']}
    accepted = client.post(endpoint+'/review', json=request)
    assert accepted.status_code == 200, accepted.text
    sid = accepted.json()['knowledge_id']
    assert get_source(kb, sid)['citable']
    assert client.post(endpoint+'/review', json=request).status_code == 409
    after = client.get('/api/chat-reviews/'+rid).json()
    assert [u['decision'] for u in after['units']] == ['confirm', 'unreviewed']
    assert after['review_summary']['confirmed'] == 1 and after['review_summary']['pending'] == 1
    assert kb._con.execute('SELECT payload FROM semantic_runs WHERE id=?', (rid,)).fetchone()[0] == raw
    assert all(m['ref'] != followup['assistant']['ref'] for m in build_dialogue(kb, cfg, cid, None).history)
    hits = search_knowledge(kb, 'reviewedprediction', 10)
    assert hits and all('另一个未选入库' not in h['text'] for h in hits)
    knowledge_chunks = {h['id'] for h in hits if h['source_id'] == sid}

    # Looking up a prior receipt after review must not resend any model request.
    calls = len(model.calls)
    assert client.post(f'/api/conversations/{cid}/chat-runs', json=req).json()['run_id'] == run_id
    assert client.get('/api/chat-runs/'+run_id+'/result').json()['status'] == 'human_partial'
    assert len(model.calls) == calls

    _, _, reused = ask(client, cid, '继续解释 reviewedprediction，并引用整理后的知识。', 'progressive_reuse')
    assert reused['status'] == 'answered'
    assert any(s.get('ref') in knowledge_chunks for s in reused['assistant']['source_links'])
    last_qa = next(messages for task, messages in reversed(model.calls) if task == 'qa')
    from litprism.qa_retrieval import prompt_material
    material = prompt_material(last_qa[-1]['content'])
    assert 'reviewedprediction' in material and '另一个未选入库的描述' not in material

    # A new conversation has no answer history to carry this knowledge across.
    new_cid = kb.add_conversation('Fresh conversation')
    _, _, fresh = ask(client, new_cid, 'reviewedprediction 的预测机制是什么？', 'progressive_fresh')
    assert any(s.get('ref') in knowledge_chunks for s in fresh['assistant']['source_links'])

    # Explicit paper-only scope deliberately excludes independent knowledge.
    strict = kb.add_conversation('Paper only', scope={'paper_ids': [pid], 'mode': 'strict'})
    _, _, paper_only = ask(client, strict, 'progressfixture 采用什么表示？', 'progressive_strict')
    assert not any(s.get('ref') in knowledge_chunks for s in paper_only['assistant']['source_links'])
    current = client.get(endpoint).json()
    revoked = client.post(endpoint+'/review', json={'action': 'revoke', 'note': '离线控制：撤销并检查派生答案。',
        'snapshot_hash': current['snapshot_hash'], 'review_token': current['review_token']})
    assert revoked.status_code == 200, revoked.text
    assert not get_source(kb, sid)['citable'] and not search_knowledge(kb, 'reviewedprediction', 10)
    refreshed = client.get('/api/conversations/'+cid+'/messages').json()['items']
    assert refreshed[-1]['status'] == 'invalid_citation'
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, new_cid, None).history)
