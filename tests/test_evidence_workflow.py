# ruff: noqa: F811
"""来源链接、可信状态和人工审核的端到端边界。"""
import threading
import time

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.deliverables import DeliverableStore
from litprism.knowledge import search_knowledge
from litprism.schema import current_version
from litprism.tasks import TaskManager, checkpoint


def test_structured_table_rows_survive_storage_and_sidebar_api(env):
    from test_pdf_reading import _table_page

    from litprism.ingest import Document, Page, chunk_document, detect_sections
    from litprism.pdf_layout import reading_layout
    _, kb, _, client = env
    pdf, page = _table_page()
    try:
        reading, tables = reading_layout(page)
        pages = [Page(1, page.get_text(), reading, tables)]
        chunks = chunk_document(Document(sections=detect_sections(pages)))
    finally:
        pdf.close()
    pid = kb.add_paper(title='Table evidence', verify_level='manual_confirmed')
    kb.add_chunks(pid, chunks)
    row = next(c for c in kb.get_chunks(pid) if 'GPT-3 Few-Shot' in c['text'])
    response = client.get('/api/evidence/'+row['id'])
    assert response.status_code == 200
    detail = response.json()
    assert detail['page'] == 1 and detail['source_kind'] == 'paper'
    assert detail['chunk_id'] == row['id'] and detail['citable']
    assert '"SuperGLUE Average": "71.8"' in detail['text']
    assert '"RTE Accuracy": "69.0"' in detail['text']
    preview = client.get('/api/paper/'+pid+'/text').json()
    assert any(row['id'] == c['chunk_id'] for pg in preview['pages'] for c in pg['chunks'])


def test_upload_keeps_original_table_but_warns_and_withholds_ambiguous_values(env, monkeypatch):
    from test_pdf_reading import _table_page

    from litprism.verify import PaperVerifier, VerificationResult
    _, kb, _, client = env
    pdf, _ = _table_page(duplicate_header=True)
    try:
        contents = pdf.tobytes()
    finally:
        pdf.close()
    monkeypatch.setattr('litprism.enrich.enrich_document', lambda *a: None)
    monkeypatch.setattr(PaperVerifier, 'verify', lambda *a, **kw: VerificationResult(level='verified'))
    response = client.post('/api/upload?analyze=false', files={'files': ('table.pdf', contents, 'application/pdf')})
    assert response.status_code == 200
    data = response.json()['results'][0]
    assert data['ok'] and any('表格' in w and '第 1 页' in w for w in data['warnings'])
    stored = kb.get_paper(data['paper_id'])
    assert '71.8' in stored['full_text']
    chunks = kb.get_chunks(stored['id'])
    assert any('表格待核对' in c['text'] for c in chunks)
    assert not any('71.8' in c['text'] for c in chunks)


def candidate(client, kind='knowledge'):
    return client.post('/api/evidence', json={'kind': kind, 'title': '人工知识来源',
        'url': 'https://example.org/original' if kind == 'web' else '',
        'content': 'reviewmarker 这是用于测试的知识快照。' * 30}).json()


@pytest.mark.parametrize('kind', ['web', 'knowledge'])
def test_reviewed_source_lifecycle_and_persistent_reply_links(env, kind):
    cfg, kb, model, client = env
    model.text = '该知识快照记载测试内容 [P1]。'
    source = candidate(client, kind)
    assert source['status'] == 'pending'
    assert not search_knowledge(kb, 'reviewmarker', 8)
    endpoint = '/api/evidence/'+source['id']+'/review'
    assert client.post(endpoint, json={'action': 'confirm', 'note': '已核对', 'content_hash': 'stale'}).status_code == 409
    assert client.post(endpoint, json={'action': 'confirm', 'note': '', 'content_hash': source['content_hash']}).status_code == 409
    req = {'action': 'confirm', 'note': '已核对原始出处与此快照', 'content_hash': source['content_hash']}
    assert client.post(endpoint, json=req).status_code == 200
    assert client.post(endpoint, json=req).status_code == 409
    assert search_knowledge(kb, 'reviewmarker', 8)
    conv = client.post('/api/conversations', json={}).json()['id']
    r = client.post('/api/chat/'+conv, json={'question': 'reviewmarker'}).json()
    assert r['assistant']['citation_check']['passed']
    link = r['assistant']['source_links'][0]
    assert link['kind'] == kind
    detail = client.get('/api/evidence/'+link['ref']).json()
    assert detail['content_hash'] == source['content_hash']
    assert detail['page'] is None and detail['citable']
    assert detail['review_note'] == req['note']
    citations = [{'tag': 'P1', 'chunk_id': link['ref']}]
    store = DeliverableStore(kb)
    did = store.add(type='notes', title='Notes', content='结论 [P1]', citations=citations)
    assert client.get('/api/deliverables/'+did+'/download').status_code == 200
    req['action'] = 'revoke'
    req['note'] = '发现版本不适用，撤销'
    assert client.post(endpoint, json=req).status_code == 200
    assert not search_knowledge(kb, 'reviewmarker', 8)
    assert client.get('/api/deliverables/'+did+'/download').status_code == 409
    history = client.get('/api/conversations/'+conv+'/messages').json()
    assert history['items'][-1]['source_links'][0]['trust_level'] == 'revoked'
    detail = client.get('/api/evidence/'+source['id']).json()
    assert len(detail['reviews']) == 2


def test_paper_links_are_round_specific_and_show_current_availability(env):
    _, kb, _, client = env
    pid = paper(kb)
    conv = client.post('/api/conversations', json={}).json()['id']
    reply = client.post('/api/chat/'+conv, json={'question': 'alphatopic'}).json()
    ref = reply['assistant']['source_links'][0]['ref']
    detail = client.get('/api/evidence/'+ref).json()
    assert detail['paper_id'] == pid and detail['page'] == 1
    client.delete('/api/paper/'+pid)
    detail = client.get('/api/evidence/'+ref).json()
    assert not detail['citable'] and detail['trust_level'] == 'deleted'
    assert client.get('/api/evidence/missing').status_code == 404


def test_uncitable_paper_can_be_manually_confirmed_from_source_detail(env):
    _, kb, _, client = env
    pid = paper(kb, level='unchecked')
    cid = kb.get_chunks(pid)[0]['id']
    assert not client.get('/api/evidence/'+cid).json()['citable']
    assert client.post('/api/evidence/'+cid+'/review', json={'action': 'confirm', 'note': ''}).status_code == 400
    confirmed = client.post('/api/evidence/'+cid+'/review', json={'action': 'confirm', 'note': '已人工查阅原始文献'}).json()
    assert confirmed['citable'] and confirmed['trust_level'] == 'manual_confirmed'
    assert kb.get_paper(pid)['verify_level'] == 'manual_confirmed'


def test_explicit_paper_scope_excludes_global_knowledge(env):
    cfg, kb, model, client = env
    p = paper(kb, text='otherterm ' * 40)
    source = candidate(client)
    client.post('/api/evidence/'+source['id']+'/review', json={'action': 'confirm', 'note': '核对', 'content_hash': source['content_hash']})
    conv = client.post('/api/conversations', json={'scope': {'paper_ids': [p], 'mode':'strict'}}).json()['id']
    result = client.post('/api/chat/'+conv, json={'question': 'reviewmarker'}).json()
    assert result['insufficient_evidence']


def test_bad_url_and_unconfigured_search_do_not_create_fake_sources(env):
    cfg, kb, _, client = env
    assert client.post('/api/evidence', json={'kind': 'web', 'title': 'Bad', 'content': 'text', 'url': 'javascript:alert(1)'}).status_code == 400
    cfg.websearch.api_key_ref = ''
    assert client.post('/api/evidence/search', json={'query': 'test'}).status_code == 503
    assert client.get('/api/evidence').json()['items'] == []


def test_web_search_pending_snapshots_are_bound_to_request_not_global_knowledge(env, monkeypatch):
    cfg, kb, _, client = env
    monkeypatch.setattr('litprism.secrets.SecretStore.get', lambda *_: 'offline-test-only')
    monkeypatch.setattr('requests.post', lambda *a, **kw: type('Response', (), {
        'raise_for_status': lambda self: None,
        'json': lambda self: {'results': [{'title': '网页快照', 'url': 'https://example.org/page', 'content': 'reviewmarker 原文摘要'}]}})())
    result = client.post('/api/evidence/search', json={'query': 'reviewmarker'}).json()
    assert result['items'][0]['status'] == 'pending'
    assert not search_knowledge(kb, 'reviewmarker', 8)
    model = env[2]
    model.text = '据本轮搜索摘要（未审核）得到的说明。[P1]'
    conv = client.post('/api/conversations', json={}).json()['id']
    reply = client.post('/api/chat/'+conv, json={'question': 'reviewmarker', 'web_search': True}).json()
    assert not reply['insufficient_evidence'] and reply['status']=='answered'
    assert reply['assistant']['source_links'][0]['session_only']
    assert reply['assistant']['candidate_links'][0]['title'] == '网页快照'
    assert model.calls, '实际取得的搜索摘要可作本轮有标识的外部证据'
    assert not search_knowledge(kb, 'reviewmarker', 8), '本轮引用不会自动确认全局知识'
    history = client.get('/api/conversations/'+conv+'/messages').json()
    assert history['items'][-1]['candidate_links']


def test_source_revoked_while_model_is_running_blocks_answer(env):
    cfg, kb, model, client = env
    source = candidate(client)
    review = {'action': 'confirm', 'note': '已核对', 'content_hash': source['content_hash']}
    client.post('/api/evidence/'+source['id']+'/review', json=review)
    original = model.chat_json
    def revoke_during_call(messages, task='', **kw):
        if task == 'qa':
            from litprism.knowledge import review_source
            review_source(kb, source['id'], action='revoke', note='生成期间撤销', content_hash=source['content_hash'])
            return {'answer': '不应继续展示的结论 [P1]'}
        return original(messages, task=task, **kw)
    model.chat_json = revoke_during_call
    conv = client.post('/api/conversations', json={}).json()['id']
    reply = client.post('/api/chat/'+conv, json={'question': 'reviewmarker'}).json()
    assert not reply['assistant']['citation_check']['passed']
    assert '不应继续展示的结论' not in reply['assistant']['content']


def test_schema_upgrade_adds_evidence_without_destroying_papers(env):
    _, kb, _, _ = env
    pid = paper(kb)
    from litprism.schema import MIGRATIONS, apply_migrations

    latest = max(m.version for m in MIGRATIONS)
    assert current_version(kb._con) == latest
    kb._con.executescript('DROP TABLE evidence_reviews; DROP TABLE evidence_chunks; DROP TABLE evidence_sources; PRAGMA user_version=3;')
    expected = len([m for m in MIGRATIONS if m.version > 3])
    assert len(apply_migrations(kb._con)) == expected
    assert kb.get_paper(pid)
    assert not apply_migrations(kb._con)


def test_cancelled_task_cannot_publish_result():
    manager = TaskManager()
    entered, release = threading.Event(), threading.Event()
    def action():
        entered.set()
        release.wait(2)
        checkpoint()
        return {'unsafe_result': True}
    try:
        tid = manager.submit(action)['id']
        assert entered.wait(2)
        manager.get(tid)['cancel'].set()
        release.set()
        deadline = time.monotonic()+2
        while manager.get(tid)['status'] not in ('cancelled', 'failed', 'succeeded') and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager.get(tid)['status'] == 'cancelled'
        assert 'result' not in manager.get(tid)
    finally:
        release.set()
        manager.pool.shutdown(wait=True)


def test_knowledge_text_and_snapshot_are_not_executable_or_editable(env):
    _, kb, _, client = env
    source = client.post('/api/evidence', json={'kind': 'knowledge', 'title': '<img src=x onerror=alert(1)>',
        'content': '<script>model_instruction()</script> reviewmarker'}).json()
    assert source['status'] == 'pending'
    assert client.patch('/api/evidence/'+source['id'], json={'content': 'changed'}).status_code == 405
    assert not search_knowledge(kb, 'reviewmarker', 8)
