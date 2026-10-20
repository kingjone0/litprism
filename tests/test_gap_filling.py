# ruff: noqa: F811
"""优先缺口的反例回归及全原文分批读取。"""
from types import SimpleNamespace

import pytest
from test_workflow_guards import Model, env, paper  # noqa: F401

from litprism.agents import CompareAgent
from litprism.extract import analyze_paper
from litprism.reading import reading_batches
from litprism.scope import paper_scope
from litprism.skills.runner import SkillRunner, _render_value


def test_delivery_rejects_uncited_body_and_rechecks_edits(env):
    _, kb, _, client = env
    pid = paper(kb)
    cite = {'tag': 'P1', 'paper_id': pid, 'chunk_id': kb.get_chunks(pid)[0]['id']}
    body = {'type': 'notes', 'title': 'Notes', 'content': '无引用结论', 'citations': [cite]}
    assert client.post('/api/deliverables', json=body).status_code == 409
    body['content'] = '结论 [P1 p.1]'
    did = client.post('/api/deliverables', json=body).json()['id']
    assert client.put('/api/deliverables/'+did, json={'content': '新无依据结论'}).status_code == 409
    assert client.get('/api/deliverables/'+did).json()['content'] == body['content']


@pytest.mark.parametrize('value', ['乙方法 [P1 p.1]', {'nested': 'error'}, ['invalid']])
def test_compare_cell_is_bound_to_paper_and_schema(env, value):
    cfg, kb, _, _ = env
    ids = [paper(kb), paper(kb, title='Second')]
    class Comparison(Model):
        def chat_json(self, *a, **kw):
            return {'summary': '比较 [P1 p.1]', 'dimensions': [{'dimension': '方法', 'values': {'D2': value}}]}
    result = CompareAgent(kb, cfg, Comparison()).compare(ids)
    assert not result['_citation_check']['passed']
    assert 'dimensions' not in result


def test_compare_reads_tail_and_reports_coverage(env):
    cfg, kb, model, _ = env
    first = paper(kb)
    original = [{k: c[k] for k in ('text', 'page', 'section')} for c in kb.get_chunks(first)]
    kb.add_chunks(first, original + [{'text': f'TAIL_{i}', 'page': i} for i in range(2, 10)])
    second = paper(kb, title='Second')
    result = CompareAgent(kb, cfg, model).compare([first, second])
    submitted = str([messages for task, messages in model.calls if task == 'compare'])
    assert 'TAIL_9' in submitted
    assert result['_coverage']['total_chunks'] == 10
    assert result['_coverage']['complete']


def test_analysis_reads_tail_and_keeps_validated_batch_results(env):
    cfg, kb, _, _ = env
    pid = paper(kb, text='填充文本。' * 6000)
    original = [{k: c[k] for k in ('text', 'page', 'section')} for c in kb.get_chunks(pid)]
    kb.add_chunks(pid, original + [{'text': 'TAIL_UNIQUE 局限是样本很小。', 'page': 9}])
    class Extraction(Model):
        def chat_json(self, messages, task='', **kw):
            self.calls.append((task, messages))
            if task == 'extract' and 'TAIL_UNIQUE' in str(messages):
                return {'limitations': '样本很小 [P2 p.9]'}
            return {}
    model = Extraction()
    result = analyze_paper(kb, cfg, model, pid, with_concepts=False)
    assert '样本很小' in result['limitations']
    assert result['_citation_check']['passed']
    assert result['_coverage']['read_chunks'] == 2


def test_reading_batches_preserve_every_character_and_fit_budget(env):
    from litprism.context.meter import Meter
    from litprism.reading import excerpt_text
    cfg, kb, _, _ = env
    cfg.context.window_tokens = 800
    cfg.context.output_reserve_tokens = 200
    text = '独特原文abcdefghij。' * 300
    pid = paper(kb, text=text)
    batches, _, coverage = reading_batches(kb, cfg, [kb.get_paper(pid)], '约束', '任务')
    assert len(batches) > 1
    assert ''.join(e['text'] for b in batches for e in b) == text
    for batch in batches:
        assert sum(Meter(cfg).estimate_message(m) for m in [
            {'role': 'system', 'content': '约束'}, {'role': 'user', 'content': '任务\n\n'+excerpt_text(batch)}]) <= 600
    assert coverage['complete']


def test_skill_scope_intersection_and_unknown_scope(env):
    cfg, kb, model, client = env
    a, b = paper(kb), paper(kb, title='Second')
    runner = SkillRunner(kb, cfg, llm=model)
    hits = runner._dispatch('search_passages', {'query': 'alphatopic', 'paper_ids': [b]}, {})
    assert hits and {h['paper_id'] for h in hits} == {b}
    assert runner._dispatch('search_passages', {'query': 'alphatopic', 'paper_ids': [b]}, {'paper_ids': [a]}) == []
    with pytest.raises(ValueError):
        paper_scope(kb, {'paper_ids': ['missing']})
    cid = client.post('/api/collections', json={'name': 'Empty'}).json()['id']
    assert paper_scope(kb, {'collection_id': cid}) == []
    assert _render_value('{{papers}}', {'papers': [a, b]}) == [a, b]


def test_chat_configuration_and_trash_derived_data(env):
    cfg, kb, _, client = env
    cfg.retrieval.top_k = 1
    a, b = paper(kb), paper(kb, title='Second')
    kb.add_concepts(a, [{'term': 'Term', 'definition': 'Definition'}])
    kb.upsert_relation(a, b, 'similar', .8)
    conv = client.post('/api/conversations', json={}).json()['id']
    answer = client.post('/api/chat/'+conv, json={'question': 'alphatopic'}).json()
    assert len(answer['sources'][0]['component']['items']) == 1
    assert client.post('/api/chat/'+conv, json={'question': 'test', 'top_k': 0}).status_code == 400
    client.delete('/api/paper/'+a)
    assert client.get('/api/concepts').json()['items'] == []
    assert client.get('/api/concepts?paper=missing').status_code == 404
    assert kb.list_relations() == [] and kb.stats()['relations'] == 0
    client.post('/api/trash/'+a+'/restore')
    assert len(kb.list_concepts()) == 1 and len(kb.list_relations()) == 1


def test_anthropic_output_limit_has_no_repeated_request(monkeypatch):
    from litprism.config import LLMConfig
    from litprism.llm import AnthropicClient, OutputLimitError
    calls = []
    def post(*a, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=200, json=lambda: {'stop_reason': 'max_tokens', 'content': []})
    monkeypatch.setattr('litprism.llm.requests.post', post)
    with pytest.raises(OutputLimitError):
        AnthropicClient(LLMConfig()).chat([])
    assert len(calls) == 1


def test_nested_db_transactions_do_not_commit_partial_analysis(env):
    _, kb, _, _ = env
    pid = paper(kb)
    with pytest.raises(RuntimeError):
        with kb.tx():
            kb.save_analysis(pid, {'research_question': 'intermediate'})
            raise RuntimeError('rollback')
    assert kb.get_analysis(pid) is None


def test_failed_later_reading_batch_preserves_previous_analysis_and_terms(env):
    cfg, kb, _, _ = env
    cfg.context.window_tokens = 3000
    cfg.context.output_reserve_tokens = 500
    pid = paper(kb, text='abcdefghijklmno 原文。' * 3000)
    kb.save_analysis(pid, {'research_question': '上一版解读'})
    kb.add_concepts(pid, [{'term': '上一版术语', 'definition': '旧定义'}])
    previous = kb.get_analysis(pid)['payload']
    class Failing(Model):
        def chat_json(self, *args, **kwargs):
            self.calls.append(('extract', []))
            if len(self.calls) > 1:
                raise RuntimeError('第二批失败')
            return {'research_question': '当前原文 [P1]'}
    with pytest.raises(RuntimeError, match='第二批失败'):
        analyze_paper(kb, cfg, Failing(), pid, force=True, with_concepts=False)
    assert kb.get_analysis(pid)['payload'] == previous
    assert kb.list_concepts(pid)[0]['term'] == '上一版术语'


def test_manual_output_budget_is_rejected_and_model_capability_drives_requests(env, tmp_path, monkeypatch):
    import yaml

    from litprism.config import config_to_dict
    from litprism.providers import ProviderStore
    cfg, kb, model, client = env
    path = tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(config_to_dict(cfg)), encoding='utf-8')
    cfg.config_path = str(path)
    before = path.read_text(encoding='utf-8')
    assert client.patch('/api/config', json={'llm': {'max_tokens': 1.5}}).status_code == 400
    assert client.patch('/api/config', json={'context': {'output_reserve_tokens': 999999}}).status_code == 400
    assert path.read_text(encoding='utf-8') == before
    assert client.patch('/api/config', json={'llm': {'max_tokens': 8192}, 'context': {'output_reserve_tokens': 8192}}).status_code == 400
    store = ProviderStore(kb)
    p = store.create(name='Offline', base_url='https://example.org/v1', api_key='offline-test-only', chat_model='fixture')
    store.set_models(p.id, [{'model_id': 'fixture', 'window_tokens': 128000, 'max_output_tokens': 100000}])
    store.set_active_model(p.id, 'fixture')
    seen = []
    monkeypatch.setattr('litprism.llm.build_llm_for', lambda *a, **kw: (seen.append(kw) or model))
    paper(kb)
    assert client.post('/api/ask', json={'question': 'alphatopic'}).status_code == 200
    assert seen[-1]['max_tokens'] == 100000


def test_nested_collections_enforce_descendant_scope_and_prevent_cycles(env):
    cfg, kb, _, client = env
    root = client.post('/api/collections', json={'name': 'Root'}).json()['id']
    child = client.post('/api/collections', json={'name': 'Child', 'parent_id': root}).json()['id']
    pid = paper(kb)
    client.patch('/api/paper/'+pid, json={'collection_id': child})
    assert paper_scope(kb, {'collection_id': root}) == [pid]
    invalid = client.patch('/api/collections/'+root, json={'name': 'Should roll back', 'parent_id': child})
    assert invalid.status_code == 400
    groups = client.get('/api/collections').json()['items']
    assert next(g for g in groups if g['id'] == root)['name'] == 'Root'
    client.delete('/api/collections/'+root)
    assert kb.get_paper(pid)['collection_id'] == child
    groups = client.get('/api/collections').json()['items']
    assert next(g for g in groups if g['id'] == child)['parent_id'] is None


def test_conversation_search_pin_and_bulk_reverification(env):
    _, kb, _, client = env
    a = client.post('/api/conversations', json={'title': 'Old'}).json()['id']
    client.post('/api/conversations', json={'title': 'New'})
    kb.append_message(a, 'user', 'uniquemessagemarker')
    assert client.patch('/api/conversations/'+a, json={'title': 'Renamed', 'pinned': True}).status_code == 200
    assert client.get('/api/conversations').json()['items'][0]['id'] == a
    assert client.get('/api/conversations?q=uniquemessagemarker').json()['items'][0]['id'] == a
    pid = paper(kb)
    result = client.post('/api/library/batch', json={'operation': 'reverify', 'paper_ids': [pid]})
    assert result.status_code == 200
    assert kb.get_paper(pid)['verify_level'] == 'unchecked'
    assert client.post('/api/library/batch', json={'operation': 'delete', 'paper_ids': [pid, 'missing']}).status_code == 400
    assert kb.get_paper(pid)
    assert client.post('/api/library/batch', json={'operation': 'delete', 'paper_ids': [pid]}).status_code == 200
    assert kb.get_paper(pid) is None


@pytest.mark.parametrize('payload', [{'summary': {'bad': 'type'}}, {'dimensions': {'bad': 'type'}},
                                     {'summary': '结论 [P1 p.1]', 'dimensions': [{'values': {}}]}])
def test_malformed_comparison_is_a_safe_failure_not_a_type_error(env, payload):
    cfg, kb, _, _ = env
    ids = [paper(kb), paper(kb, title='Second')]
    class Comparison(Model):
        def chat_json(self, *a, **kw):
            return payload
    result = CompareAgent(kb, cfg, Comparison()).compare(ids)
    assert not result['_citation_check']['passed']
    assert 'dimensions' not in result
