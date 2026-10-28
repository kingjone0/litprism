"""Keep the paired acceptance honest about unknowns, source parity and paid retries."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


def tool():
    p = Path(__file__).resolve().parents[1] / 'tools/anchor_live_acceptance.py'
    spec = importlib.util.spec_from_file_location('anchor_live_acceptance', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_failure_is_unknown_and_missing_map_does_not_become_semantic_pass():
    t = tool()
    assert t.measurements(None, {}) is None
    for data in ({}, {'evidence_map': 'bad'}, {'evidence_map': [{'quotes': None}]}):
        m = t.measurements(data, {})
        assert m['semantic_passed'] is None and m['human_review_time_seconds'] is None
        assert m['anchors_proposed'] == 0


def test_pair_source_parity_includes_uncited_sources_and_text_but_not_request_grant():
    t = tool()
    sources = [{'tag': 'P1', 'chunk_id': 'a', 'text': 'quoted', 'web_grant_id': 'run1'},
               {'tag': 'P2', 'chunk_id': 'b', 'text': 'uncited context'}]
    changed = copy.deepcopy(sources)
    changed[0]['web_grant_id'] = 'run2'
    assert t.delivery_signature(changed) == t.delivery_signature(sources)
    changed[1]['text'] = 'different uncited context'
    assert t.delivery_signature(changed) != t.delivery_signature(sources)
    assert t.delivery_signature(None) is None


def test_frozen_plan_counts_query_expansion_separately_from_twelve_qa_calls():
    t = tool()
    groups = json.loads((t.ROOT / 'tools/anchor_live_cases.json').read_text(encoding='utf-8'))['groups']
    assert len(groups) * 2 == 12
    assert sum(sum(t.task_limits(g).values()) for g in groups) * 2 == 14


def test_model_dispatch_blocks_retries_and_unplanned_tasks_before_sending():
    t = tool()
    counts = {}
    limits = {'expand': 1, 'qa': 1}
    t.claim_dispatch(counts, limits, 'expand')
    t.claim_dispatch(counts, limits, 'qa')
    for task in ('expand', 'qa', 'semantic_review'):
        with pytest.raises(RuntimeError, match='blocked before dispatch'):
            t.claim_dispatch(counts, limits, task)
    assert counts == limits


@pytest.mark.parametrize('fail_anchor', [False, True])
def test_durable_pair_uses_same_sources_and_first_attempts_without_semantic_claim(tmp_path, monkeypatch, fail_anchor):
    import yaml
    from litprism.config import Config, config_to_dict
    from litprism.db import KnowledgeBase
    from litprism.providers import ProviderStore
    from litprism import llm
    t = tool()
    home = tmp_path / '_selftest.pair'
    source = tmp_path / '_selftest.source'
    source.mkdir()
    (source / 'qa-results.json').write_text('{}', encoding='utf-8')
    cfg = Config()
    cfg.app.data_dir, cfg.export.notes_dir = str(home / 'data'), str(home / 'notes')
    cfg.llm.timeout = 10
    cfg.context.overflow_retries = 0
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Paired alphatopic source', verify_level='verified')
    kb.add_chunks(pid, [{'text': 'alphatopic original source.', 'page': 1}])
    store = ProviderStore(kb)
    provider = store.create(name='Offline test transport', base_url='http://127.0.0.1:1', api_key='acceptance-placeholder')
    store.set_models(provider.id, [{'model_id': 'test', 'window_tokens': 100000, 'max_output_tokens': 10000}])
    store.set_active_model(provider.id, 'test')
    kb.close()
    (home / 'config').mkdir()
    (home / 'config/config.yaml').write_text(yaml.safe_dump(config_to_dict(cfg)), encoding='utf-8')
    text = 'actual test fixture abstract alphatopic'
    snapshots = [{'id': 'web', 'url': 'https://example.org/paper', 'title': 'Fixture abstract', 'content': text,
                  'obtained_at': '2026-10-24', 'acquisition': 'unit fixture',
                  'content_sha256': __import__('hashlib').sha256(text.encode()).hexdigest()}]
    t.mixed.write(home / 'web-snapshots.json', snapshots)
    t.mixed.write(home / 'cases.json', {'groups': [{'id': 'pair', 'available': ['transformer'], 'focus': ['transformer'],
        'cases': [{'id': 'v1', 'question': 'alphatopic', 'web': ['web'], 'expect': 'not a semantic test'}]}]})
    t.mixed.write(home / 'manifest.json', {'papers': [{'alias': 'transformer', 'paper_id': pid}],
        'code_sha256': t.fingerprints(), 'planned_calls': 2, 'source_root': str(source),
        'model_task_limits': {'pair': {'qa': 1}},
        'source_results_sha256': t.mixed.sha(source / 'qa-results.json'),
        'cases_sha256': t.mixed.sha(home / 'cases.json'), 'snapshots_sha256': t.mixed.sha(home / 'web-snapshots.json')})
    sends = []
    def transport(url, *, headers, json, timeout, stream=False):
        messages = json['messages']
        anchor = '【试验定位锚点' in messages[-1]['content']
        sends.append(anchor)
        if fail_anchor and anchor:
            raise llm.requests.exceptions.ConnectTimeout('controlled first attempt failure')
        quote = {'source': 'P1', 'quote': 'alphatopic original source.'}
        if anchor:
            catalog = __import__('json').loads(messages[-1]['content'].split('【试验定位锚点（程序生成，不是语义结论）】\n')[1])
            a = next(x for x in catalog if x['source'] == 'P1')
            quote = {'source': 'P1', 'anchor_id': a['anchor_id']}
        answer = 'alphatopic original source [P1]'
        value = {'answer': answer, 'evidence_map': [{'claim': answer, 'subject': 'alphatopic', 'basis': 'direct',
                   'sources': ['P1'], 'quotes': [quote]}]}
        class Response:
            status_code = 200
            def iter_lines(self, **kwargs):
                for packet in [
                    {'choices':[{'index':0,'delta':{'content':__import__('json').dumps(value)},'finish_reason':'stop'}]},
                    {'choices':[],'usage':{'prompt_tokens':200,'completion_tokens':100}}]:
                    yield ('data: '+__import__('json').dumps(packet)).encode()
                    yield b''
                yield b'data: [DONE]'
                yield b''
            def close(self):
                pass
            def json(self):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': __import__('json').dumps(value)}}],
                        'usage': {'prompt_tokens': 200, 'completion_tokens': 100}}
        return Response()
    monkeypatch.setattr(llm.requests, 'post', transport)
    t.run(home)
    result = json.loads((home / 'results.json').read_text(encoding='utf-8'))
    assert sends == [False, True] and result['usage']['calls'] == 2
    assert all(r['first_attempt_only'] for r in result['rows'])
    assert result['semantic_passed'] is None and result['original_results_unchanged']
    a = next(r for r in result['rows'] if r['arm'] == 'anchors')
    if fail_anchor:
        assert a['measurements'] is None and result['usage']['unknown_usage_calls'] == 1
        assert not result['pairs'][0]['same_delivery']
    else:
        assert result['pairs'][0]['same_delivery']
        assert a['measurements']['anchors_resolved'] == 1
        assert a['measurements']['semantic_passed'] is None
    with pytest.raises(FileExistsError):
        t.run(home)  # Never send another attempt from the same frozen run.
    assert sends == [False, True]
