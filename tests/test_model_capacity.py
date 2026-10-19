"""Capacity allocation and reasoning isolation across the real request boundary."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from litprism.config import Config, LLMConfig
from litprism.db import KnowledgeBase
from litprism.llm import AnthropicClient, ContextWindowError, OpenAICompatClient, output_allocation
from litprism.providers import ProviderError, ProviderStore, apply_active_window
from litprism.web import server


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    store = ProviderStore(kb)
    p = store.create(name='Confirmed gateway', base_url='https://api.commandcode.ai/provider/v1',
                     api_key='offline-fixture-only', chat_model='deepseek/deepseek-v4.1-flash')
    store.set_models(p.id, [{'model_id': p.chat_model}])
    store.set_active_model(p.id, p.chat_model)
    yield cfg, kb, store, p
    kb.close()


def test_confirmed_capacity_applies_without_migrating_or_rewriting_user_settings(env):
    cfg, kb, store, provider = env
    probe = copy.deepcopy(cfg)
    info = apply_active_window(probe, kb)
    assert info['window'] == 1_000_000 and info['max_output_tokens'] == 384_000
    assert info['output_mode'] == 'automatic'
    assert probe.llm.max_tokens == 384_000 and probe.llm.timeout == 300
    assert cfg.llm.max_tokens == 4096 and cfg.context.window_tokens == 32000
    assert store.get(provider.id).to_dict()['models'][0]['defaults']['source'] == 'user_confirmed'
    other = store.create(name='Other gateway', base_url='https://example.org/v1', chat_model=provider.chat_model)
    store.set_active_model(other.id, provider.chat_model)
    assert apply_active_window(copy.deepcopy(cfg), kb)['max_output_tokens'] is None


@pytest.mark.parametrize('api_output', [None, 128000])
def test_api_metadata_is_cached_and_preferred_without_becoming_model_configuration(env, monkeypatch, api_output):
    cfg, kb, store, provider = env
    item = {'id': provider.chat_model, 'context_length': 1_000_000}
    if api_output:
        item['max_output_tokens'] = api_output
    monkeypatch.setattr('requests.get', lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {'data': [item]}))
    store.fetch_models(provider.id)
    # Replacing/editing the directory must not discard fetched metadata.
    store.set_models(provider.id, [{'model_id': provider.chat_model}])
    info = apply_active_window(copy.deepcopy(cfg), kb)
    assert info['window'] == 1_000_000
    assert info['capability_sources']['window_tokens'] == 'api'
    assert info['max_output_tokens'] == (api_output or 384000)
    assert info['capability_sources']['max_output_tokens'] == ('api' if api_output else 'user_confirmed')
    assert store.get(provider.id).models[0].api_limits['checked_at']
    other = store.create(name='Another service', base_url='https://example.org/v1', chat_model=provider.chat_model)
    store.set_active_model(other.id, provider.chat_model)
    assert apply_active_window(copy.deepcopy(cfg), kb)['max_output_tokens'] is None


@pytest.mark.parametrize('item', [
    {'context_length': True, 'max_output_tokens': -1},
    {'context_length': '1000000', 'max_tokens': 384000},
    {'context_length': 1.5, 'max_output_tokens': '384K'},
])
def test_ambiguous_or_invalid_api_fields_do_not_become_capabilities(item):
    from litprism.providers import api_capabilities
    assert api_capabilities(item) == {'window_tokens': None, 'max_output_tokens': None}


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('task', ['extract', 'qa', 'semantic_review', 'semantic_cross_review'])
def test_all_tasks_use_capacity_not_legacy_or_environment_caps(env, monkeypatch, protocol, task):
    cfg, kb, _, _ = env
    apply_active_window(cfg, kb)
    # Old experimental process settings cannot silently truncate the app.
    monkeypatch.setenv('LITPRISM_REVIEW_MAX_TOKENS', '4096')
    client = (OpenAICompatClient if protocol == 'openai' else AnthropicClient)(cfg.llm)
    captured = []
    def post(*a, **kw):
        captured.append(kw['json'])
        return SimpleNamespace(status_code=200, json=lambda: {
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'reasoning_content': 'service thought'}}],
            'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'service thought'},
                                                {'type': 'text', 'text': '{}'}]})
    monkeypatch.setattr('requests.post', post)
    assert client.chat_json([{'role': 'user', 'content': 'source'}], task=task) == {}
    assert captured[-1]['max_tokens'] == 384_000
    assert client.last_reasoning == 'service thought'
    assert client.last_request_settings['output_mode'] == 'automatic'
    # New responses must not inherit a previous thought.
    monkeypatch.setattr('requests.post', lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: {
        'choices': [{'message': {'content': '{}'}}], 'content': [{'type': 'text', 'text': '{}'}]}))
    client.chat_json([{'role': 'user', 'content': 'next'}], task=task)
    assert client.last_reasoning == ''


def test_large_input_receives_remaining_window_and_oversize_is_not_sent(monkeypatch):
    settings = LLMConfig(automatic_output=True, model_window_tokens=1_000_000, model_max_output_tokens=384_000)
    messages = [{'role': 'user', 'content': 'x' * 2_800_000}]
    result = output_allocation(settings, messages)
    assert 260_000 < result['max_tokens'] < 270_000
    assert result['max_tokens'] + result['estimated_input_tokens'] + result['input_safety_tokens'] == 1_000_000
    monkeypatch.setattr('requests.post', lambda *a, **kw: pytest.fail('Oversized input reached network'))
    with pytest.raises(ContextWindowError):
        OpenAICompatClient(settings).chat([{'role': 'user', 'content': 'x' * 4_000_000}])


def test_capacity_does_not_turn_into_a_claim_generation_target():
    from litprism.context.meter import Meter
    from litprism.plan import build_plan
    source = [{'id': 'c1', 'page': 1, 'section': 'Body', 'text': 'source observation. ' * 200}]
    small = build_plan({'id': 'p1'}, source, Meter(), available_tokens=1_000_000, max_tokens=32768)
    large = build_plan({'id': 'p1'}, source, Meter(), available_tokens=1_000_000, max_tokens=384_000)
    assert small.directives[0].expected == large.directives[0].expected


@pytest.mark.parametrize('value', [0, -1, 2.5, True, 'bad'])
def test_invalid_capability_does_not_replace_existing_models(env, value):
    _, _, store, p = env
    with pytest.raises(ProviderError):
        store.set_models(p.id, [{'model_id': p.chat_model, 'max_output_tokens': value}])
    assert store.get(p.id).models[0].max_output_tokens is None


def test_chat_roundtrip_keeps_thinking_out_of_answer_evidence_and_next_prompt(env, monkeypatch):
    cfg, kb, _, _ = env
    pid = kb.add_paper(title='Source', verify_level='verified')
    kb.add_chunks(pid, [{'page': 1, 'text': 'alphatopic source observation. ' * 40}])
    monkeypatch.setattr(server, 'KnowledgeBase', lambda _: kb)
    captured = []
    thought = '<script>private_thought_marker</script> [P999]'
    def post(*a, **kw):
        captured.append(kw['json'])
        return SimpleNamespace(status_code=200, json=lambda: {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'answer': 'Source observation [P1 p.1]', 'terms': ['alphatopic']}),
            'reasoning_content': thought}}]})
    monkeypatch.setattr('requests.post', post)
    api = TestClient(server.create_app(cfg))
    conv = api.post('/api/conversations', json={'title': 'Offline'}).json()
    cid = conv['id']
    response = api.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert 'assistant' in response, response
    assert response['assistant']['reasoning'] == thought
    assert thought not in response['assistant']['content']
    assert response['assistant']['request_settings']['max_tokens'] == 384_000
    assert response['assistant']['citation_check']['passed']
    saved = api.get('/api/conversations/'+cid+'/messages').json()['items'][-1]
    assert saved['reasoning'] == thought
    assert saved['request_settings']['output_mode'] == 'automatic'
    before = len(captured)
    assert api.post('/api/chat/'+cid, json={'question': 'alphatopic again'}).status_code == 200
    assert 'private_thought_marker' not in json.dumps(captured[before:])
    assert api.get('/api/context/'+cid).json()['context_pressure']['window'] == 1_000_000
