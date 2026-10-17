"""跨入口验收：防止核验、引用、范围与模型设置在模块边界失效。"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from litprism.agents import CompareAgent, QAAgent
from litprism.config import Config
from litprism.context.dialogue import DialogueContext
from litprism.db import KnowledgeBase
from litprism.deliverables import DeliverableStore
from litprism.mcp.tools import LitPrismTools
from litprism.providers import ProviderStore
from litprism.skills.manifest import SkillManifest
from litprism.skills.registry import SkillEntry
from litprism.skills.runner import SkillRunner
from litprism.skills.validate import validate
from litprism.verify import PaperVerifier, VerificationResult
from litprism.web import server


class Model:
    name = 'recording'

    def __init__(self):
        self.calls = []
        self.text = '结论 [P1 p.1]'

    def chat_json(self, messages, task='', **kwargs):
        self.calls.append((task, messages))
        if task == 'qa':
            return {'answer': self.text, 'confidence': 'high'}
        if task == 'extract':
            return {'research_question': '方法 [P1 p.1]', 'field_evidence': {'research_question': ['P1 p.1']}}
        if task == 'compare':
            return {'summary': self.text, 'dimensions': []}
        return {}

    def chat(self, *args, **kwargs):
        self.calls.append(('skill', args))
        return self.text


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
        getattr(cfg.sources, name).enabled = False
    kb = KnowledgeBase(cfg)
    model = Model()
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    monkeypatch.setattr(server, 'build_llm', lambda _: model)
    client = TestClient(server.create_app(cfg))
    yield cfg, kb, model, client
    kb.close()


def paper(kb, title='Study', level='verified', page=1, text='alphatopic evidence. ' * 20):
    pid = kb.add_paper(title=title, verify_level=level)
    kb.add_chunks(pid, [{'text': text, 'page': page}])
    return pid


@pytest.mark.parametrize('level', ['not_found', 'likely', 'unchecked'])
def test_local_upload_blocks_unverified_even_when_verify_flag_false(env, monkeypatch, level):
    _, kb, _, client = env
    monkeypatch.setattr(PaperVerifier, 'verify', lambda *a, **kw: VerificationResult(level=level))
    monkeypatch.setattr('litprism.enrich.enrich_document', lambda *a: None)
    r = client.post('/api/upload?verify=false&analyze=false', files={'files': ('a.md', b'# Audit\n\n' + b'content '*90)})
    assert r.status_code == 200 and not r.json()['results'][0]['ok']
    assert not kb.list_papers() and kb.count_chunks() == 0


def test_unverified_old_records_cannot_enter_qa_or_mcp_evidence(env):
    cfg, kb, model, _ = env
    paper(kb, level='not_found')
    ans = QAAgent(kb, cfg, model).ask('alphatopic')
    assert ans.insufficient_evidence and not any(t == 'qa' for t, _ in model.calls)
    assert LitPrismTools(kb, cfg).search_passages('alphatopic')['count'] == 0


def test_web_whitelist_is_used_and_revocation_removes_citability(env):
    cfg, kb, model, client = env
    pid = paper(kb, level='unchecked')
    r = client.post('/api/whitelist', json={'title': 'Study', 'reason': '用户查阅原文确认'})
    assert r.status_code == 200
    assert client.post('/api/verify', json={'title': 'Study'}).json()['level'] == 'manual_confirmed'
    assert not QAAgent(kb, cfg, model).ask('alphatopic').insufficient_evidence
    client.delete('/api/whitelist/' + r.json()['id'])
    assert kb.get_paper(pid)['verify_level'] == 'unchecked'
    assert QAAgent(kb, cfg, model).ask('alphatopic').insufficient_evidence


def test_chat_scope_and_like_fallback_exclude_other_papers(env):
    _, kb, _, client = env
    a = paper(kb, title='A', text='aaa evidence')
    b = paper(kb, title='B', text='bbb evidence')
    conv = client.post('/api/conversations', json={'scope': {'paper_ids': [a]}}).json()['id']
    result = client.post('/api/chat/' + conv, json={'question': 'bbb'}).json()
    assert result['insufficient_evidence'] and result['sources'] == []
    hits = kb.search_match('(', paper_ids=[a], fallback_terms=['bbb', 'aaa'])
    assert hits and all(h['paper_id'] == a for h in hits)
    assert all(h['paper_id'] != b for h in hits)


def test_unknown_page_cannot_validate_invented_page(env):
    cfg, kb, model, _ = env
    paper(kb, page=None)
    assert not QAAgent(kb, cfg, model).ask('alphatopic').citation_check_passed


def test_mcp_ask_uses_current_interface(env):
    cfg, kb, model, _ = env
    paper(kb)
    result = LitPrismTools(kb, cfg, llm=model).ask('alphatopic')
    assert result['ok'] and result['citation_check']['passed']
    assert result['citations'][0]['chunk_id']


def test_skill_without_evidence_neither_calls_model_nor_saves(env):
    cfg, kb, model, _ = env
    m = SkillManifest(id='test', name='Test', prompt='引用 [P1 p.1]', output={'type': 'deliverable', 'citation_required': True})
    entry = SkillEntry(manifest=m, validation=validate(m))
    result = SkillRunner(kb, cfg, llm=model, registry=SimpleNamespace(get=lambda _: entry)).run('test')
    assert not result.ok and not result.deliverable_id and not model.calls
    assert not DeliverableStore(kb).list()


def test_compare_blocks_fabricated_reference(env):
    cfg, kb, model, _ = env
    a, b = paper(kb, title='A'), paper(kb, title='B')
    model.text = '捏造结论 [P999 p.999]'
    result = CompareAgent(kb, cfg, model).compare([a, b])
    assert not result['_citation_check']['passed']
    assert '捏造结论' not in result['summary']


def test_every_web_task_uses_selected_model_and_force_reanalysis(env, monkeypatch):
    cfg, kb, startup, client = env
    pid = paper(kb)
    selected = Model()
    selected.name = 'selected'
    store = ProviderStore(kb, cfg.data_path)
    p = store.create(name='Test', base_url='https://offline.invalid', api_key='test-only')
    store.set_active_model(p.id, 'test')
    monkeypatch.setattr('litprism.llm.build_llm_for', lambda *a, **kw: selected)
    r = client.post('/api/analyze/' + pid + '?force=true')
    assert r.status_code == 200 and r.json()['analysis']['_model'] == 'selected'
    client.post('/api/ask', json={'question': 'alphatopic'})
    client.post('/api/analyze/' + pid + '?force=true')
    assert len([t for t, _ in selected.calls if t == 'extract']) == 2
    assert any(t == 'qa' for t, _ in selected.calls) and not startup.calls


def test_relatedness_threshold_is_applied(env):
    cfg, kb, model, _ = env
    paper(kb)
    cfg.retrieval.min_score = 1.0
    assert QAAgent(kb, cfg, model).ask('alphatopic').insufficient_evidence


def test_third_followup_keeps_original_topic():
    d = DialogueContext(history=[
        {'role': 'user', 'kind': 'normal', 'content': 'quantumfold 的方法'},
        {'role': 'user', 'kind': 'normal', 'content': '它有哪些局限？'},
    ])
    assert 'quantumfold' in d.query('它用了什么数据集？')


def test_request_over_budget_never_reaches_model(env):
    cfg, kb, model, _ = env
    paper(kb)
    cfg.context.window_tokens = 200
    cfg.context.output_reserve_tokens = 50
    result = QAAgent(kb, cfg, model).ask('alphatopic')
    assert result.insufficient_evidence and not any(t == 'qa' for t, _ in model.calls)


def test_export_revalidates_database_sources(env, tmp_path):
    _, kb, _, _ = env
    pid = paper(kb)
    chunk = kb.get_chunks(pid)[0]
    store = DeliverableStore(kb)
    did = store.add(type='notes', title='Test', content='结论 [P1 p.999]', citations=[{'tag': 'P1', 'paper_id': pid, 'chunk_id': chunk['id'], 'page': 999}])
    with pytest.raises(ValueError, match='引用校验'):
        store.export(did, tmp_path/'invalid.md')
    assert not (tmp_path/'invalid.md').exists()


def test_collections_and_recycle_bin_preserve_original_content(env):
    cfg, kb, model, client = env
    pid = paper(kb)
    chunk = kb.get_chunks(pid)[0]
    cid = client.post('/api/collections', json={'name': 'Research'}).json()['id']
    assert client.patch('/api/paper/'+pid, json={'collection_id': cid}).status_code == 200
    assert kb.get_paper(pid)['collection_id'] == cid
    assert client.delete('/api/paper/'+pid).status_code == 200
    assert kb.get_paper(pid) is None
    assert kb._con.execute('SELECT id FROM chunks WHERE id=?', (chunk['id'],)).fetchone()
    assert QAAgent(kb, cfg, model).ask('alphatopic').insufficient_evidence
    assert client.get('/api/trash').json()['items'][0]['id'] == pid
    assert client.post('/api/trash/'+pid+'/restore').json()['ok']
    assert not QAAgent(kb, cfg, model).ask('alphatopic').insufficient_evidence
    client.delete('/api/collections/'+cid)
    assert kb.get_paper(pid)['collection_id'] is None


def test_bibliography_uses_original_title_and_blocks_unverified(env):
    _, kb, _, client = env
    pid = paper(kb)
    kb.rename_paper(pid, 'Display name')
    for fmt in ('bibtex', 'ris', 'json'):
        r = client.post('/api/library/export', json={'paper_ids': [pid], 'format': fmt})
        assert r.status_code == 200 and 'Study' in r.text
    invalid = paper(kb, title='Unchecked', level='unchecked')
    assert client.post('/api/library/export', json={'paper_ids': [invalid]}).status_code == 409


def test_create_download_and_archive_delivery_validate_sources(env):
    _, kb, _, client = env
    pid = paper(kb)
    cite = {'tag': 'P1', 'chunk_id': kb.get_chunks(pid)[0]['id'], 'paper_id': pid}
    r = client.post('/api/deliverables', json={'title': 'Notes', 'type': 'notes', 'content': '结论 [P1 p.1]', 'citations': [cite]})
    assert r.status_code == 200
    downloaded = client.get('/api/deliverables/'+r.json()['id']+'/download')
    assert downloaded.status_code == 200 and '## 引用来源' in downloaded.text
    store = DeliverableStore(kb)
    assert store.get(r.json()['id']).status == 'exported'
    store.update(r.json()['id'], status='final')
    assert client.get('/api/deliverables-archive').content.startswith(b'PK')
    assert store.get(r.json()['id']).status == 'final'
    kb.update_paper(pid, verify_level='not_found')
    assert client.get('/api/deliverables/'+r.json()['id']+'/download').status_code == 409
    assert client.get('/api/deliverables-archive').status_code == 409


def test_skill_api_runs_declarative_template(env):
    cfg, kb, model, client = env
    paper(kb)
    created = client.post('/api/skills/scaffold', json={'id': 'audit-skill', 'name': 'Audit'})
    assert created.status_code == 200
    r = client.post('/api/skills/audit-skill/run', json={'inputs': {'topic': 'alphatopic'}})
    assert r.status_code == 200 and r.json()['ok'], r.text
    assert r.json()['citation_check']['passed']


def test_preference_edit_delete_restore_and_snapshot(env):
    cfg, kb, model, client = env
    paper(kb)
    mid = client.post('/api/memories', json={'value': '回答简短', 'kind': 'style_preference'}).json()['id']
    conv = client.post('/api/conversations', json={}).json()['id']
    client.post('/api/chat/'+conv, json={'question': 'alphatopic'})
    assert client.patch('/api/memories/'+mid, json={'value': '回答详细'}).status_code == 200
    snapshot = client.get('/api/turns/'+conv).json()['items'][0]['read_refs']
    assert '回答简短' in str(snapshot['request']) and '回答详细' not in str(snapshot['request'])
    client.delete('/api/memories/'+mid)
    assert client.post('/api/memories/'+mid+'/restore').json()['ok']


def test_context_overflow_retries_only_after_reducing_request(env):
    cfg, kb, _, _ = env
    paper(kb)
    conv = kb.add_conversation('History')
    kb.append_message(conv, 'user', 'Old history')
    class OverflowModel(Model):
        def chat_json(self, messages, task='', **kw):
            if task == 'qa' and not any(t == 'qa' for t, _ in self.calls):
                self.calls.append(('qa', list(messages)))
                raise RuntimeError('context_length_exceeded')
            return super().chat_json(messages, task=task, **kw)
    model = OverflowModel()
    result = QAAgent(kb, cfg, model).ask('alphatopic', conversation_id=conv)
    qa = [messages for task, messages in model.calls if task == 'qa']
    assert result.citation_check_passed and len(qa) == 2
    assert len(qa[1]) < len(qa[0])


def test_anthropic_json_contract_and_usage(env, monkeypatch):
    from litprism.llm import build_llm_for
    cfg, _, _, _ = env
    response = SimpleNamespace(status_code=200, json=lambda: {'content': [{'type': 'text', 'text': '{"answer":"ok"}'}],
                                                            'usage': {'input_tokens': 10, 'output_tokens': 3}})
    monkeypatch.setattr('litprism.llm.requests.post', lambda *a, **kw: response)
    model = build_llm_for(cfg, base_url='https://offline.invalid', api_key='test', model='test', protocol='anthropic')
    assert model.chat_json([{'role': 'user', 'content': 'test'}])['answer'] == 'ok'
    assert model.last_usage['input_tokens'] == 10


def test_extract_clears_fields_with_fabricated_evidence(env):
    from litprism.extract import analyze_paper
    cfg, kb, _, _ = env
    pid = paper(kb)
    class BadExtraction(Model):
        def chat_json(self, messages, **kwargs):
            return {'research_question': '无原文支持的结论', 'field_evidence': {'research_question': ['P999 p.3']}}
    result = analyze_paper(kb, cfg, BadExtraction(), pid, with_concepts=False)
    assert result['research_question'] == ''
    assert not result['_citation_check']['passed']
    assert '无原文支持的结论' not in str(kb.get_analysis(pid)['payload'])


def test_legacy_setting_cannot_disable_required_citation_check(env):
    cfg, kb, model, _ = env
    paper(kb)
    cfg.qa.strict_citation_check = False
    model.text = '错误来源 [P999 p.999]'
    assert not QAAgent(kb, cfg, model).ask('alphatopic').citation_check_passed


@pytest.mark.parametrize('concept_result', [[], [{'term': 'Bad', 'definition': '错误 [P999 p.9]'}], 'error'])
def test_reanalysis_clears_old_concepts_and_persists_latest_result(env, concept_result):
    from litprism.extract import analyze_paper
    cfg, kb, _, client = env
    pid = paper(kb)
    kb.add_concepts(pid, [{'term': 'Old', 'definition': '旧模型结果'}])
    class Extraction(Model):
        def chat_json(self, messages, task='', **kw):
            if task == 'concepts':
                if concept_result == 'error':
                    raise RuntimeError('offline failure')
                return {'concepts': concept_result}
            return super().chat_json(messages, task=task, **kw)
    result = analyze_paper(kb, cfg, Extraction(), pid, force=True)
    assert result['concepts'] == []
    assert kb.list_concepts(pid) == []
    assert kb.get_analysis(pid)['payload'] == result
    detail = client.get('/api/paper/'+pid).json()
    assert detail['analysis']['concepts'] == []
    assert 'Old' not in detail['markdown']


def test_malformed_extraction_fields_cannot_break_detail_or_export(env):
    from litprism.extract import analyze_paper, format_analysis
    cfg, kb, _, client = env
    pid = paper(kb)
    class Extraction(Model):
        def chat_json(self, messages, **kw):
            return {'research_question': ['无效字段 [P1 p.1]'], 'keywords': 42,
                    'concepts': [{'term': 'Injected', 'definition': '未检查'}]}
    result = analyze_paper(kb, cfg, Extraction(), pid, with_concepts=False)
    assert result['research_question'] == '' and result['keywords'] == []
    assert not result['_citation_check']['passed']
    assert 'Injected' not in format_analysis(kb.get_paper(pid), result)
    assert client.get('/api/paper/'+pid).status_code == 200


def test_output_token_limit_is_reported_without_identical_retries(monkeypatch):
    from litprism.config import LLMConfig
    from litprism.llm import OpenAICompatClient, OutputLimitError
    calls = []
    def respond(*a, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=200, json=lambda: {
            'choices': [{'finish_reason': 'length', 'message': {'content': '{"partial":'}}],
            'usage': {'completion_tokens': 100}})
    monkeypatch.setattr('litprism.llm.requests.post', respond)
    model = OpenAICompatClient(LLMConfig())
    with pytest.raises(OutputLimitError, match='输出 token 上限'):
        model.chat_json([{'role': 'user', 'content': 'test'}])
    assert len(calls) == 1
    assert model.last_usage == {'completion_tokens': 100}


def test_compare_accepts_single_cited_statement_without_character_iteration(env):
    cfg, kb, _, _ = env
    ids = [paper(kb), paper(kb, title='Second')]
    class Comparison(Model):
        def chat_json(self, messages, **kw):
            return {'summary': '结论 [P1 p.1]', 'dimensions': [],
                    'convergence': '一致点 [P1 p.1][P2 p.1]', 'divergence': '',
                    'evidence_map': [{'claim': '结论', 'sources': 'P1 p.1'}]}
    result = CompareAgent(kb, cfg, Comparison()).compare(ids)
    assert result['_citation_check']['passed']
    assert result['convergence'] == ['一致点 [P1 p.1][P2 p.1]']
    assert result['divergence'] == []
    assert result['evidence_map'][0]['sources'] == ['P1 p.1']
