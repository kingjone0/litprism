"""N02 reporting must not turn model self-assessment or missing data into truth."""
import importlib
import json
from types import SimpleNamespace

import pytest

from litprism.config import LLMConfig
from litprism.semantics import payload_hash


@pytest.fixture
def audit(monkeypatch):
    from pathlib import Path
    # Imported legacy CLI helpers set a default HOME. Keep that default local
    # to this fixture so unrelated path tests still run without HOME overrides.
    monkeypatch.setenv('LITPRISM_HOME', '')
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools'))
    return importlib.import_module('n02_acceptance')


@pytest.fixture
def evidence(audit):
    payload = {'_coverage': {'complete': True, 'uncovered_units': []},
               '_semantic_review': {'passed': True, 'pending': 0, 'coverage': {'complete': True},
                                    'items': [{'id': 'c1', 'text': 'Actual claim', 'status': 'machine_supported'}]}}
    rows = [{'id': 'positive', 'expected': 'supported', 'released': True, 'raw_verdict': 'supported', 'complete': True},
            {'id': 'negative', 'expected': 'blocked', 'released': False, 'raw_verdict': 'contradicted', 'complete': True}]
    cross = [{'id': 'cross', 'expected_conflict': True, 'found': True, 'complete': True}]
    labels = {'analysis_hash': payload_hash(payload), 'reviewer': 'independent-test-reference',
              'items': [{'id': 'c1', 'text_sha256': audit.sha(b'Actual claim'), 'verdict': 'supported', 'basis': 'p.1 original quote'}]}
    return payload, rows, cross, labels


def assess(audit, evidence, **kwargs):
    payload, rows, cross, labels = evidence
    return audit.quality(payload, rows, cross, local_planned=2, cross_planned=1, **kwargs)


def test_self_supported_does_not_substitute_for_independent_adjudication(audit, evidence):
    result = assess(audit, evidence)
    assert not result['passed'] and result['paper']['unjudged'] == 1
    assert result['paper']['precision'] is None and result['paper']['recall'] is None
    assert assess(audit, evidence, adjudication=evidence[3])['passed']


@pytest.mark.parametrize('kind', ['false_accept', 'false_reject', 'abstain', 'incomplete', 'duplicate'])
def test_failed_controls_and_missing_or_duplicate_samples_cannot_pass(audit, evidence, kind):
    payload, rows, cross, labels = evidence
    if kind == 'false_accept':
        rows[1].update(raw_verdict='supported', released=True)
    elif kind == 'false_reject':
        rows[0].update(raw_verdict='contradicted', released=False)
    elif kind == 'abstain':
        rows[0].update(raw_verdict='needs_review', released=False)
    elif kind == 'incomplete':
        rows.pop()
    else:
        rows[1]['id'] = rows[0]['id']
    assert not assess(audit, evidence, adjudication=labels)['passed']


@pytest.mark.parametrize('kind', ['pending', 'uncovered', 'cross_incomplete', 'cross_error', 'false_release'])
def test_full_paper_gate_retains_incomplete_and_wrong_release(audit, evidence, kind):
    payload, rows, cross, labels = evidence
    if kind == 'pending':
        payload['_semantic_review'].update(passed=False, pending=1)
    elif kind == 'uncovered':
        payload['_coverage'].update(complete=False, uncovered_units=['d1'])
    elif kind == 'cross_incomplete':
        payload['_semantic_review']['coverage']['complete'] = False
    elif kind == 'cross_error':
        cross[0]['found'] = False
    else:
        labels['items'][0]['verdict'] = 'contradicted'
    labels['analysis_hash'] = payload_hash(payload)
    assert not assess(audit, evidence, adjudication=labels)['passed']


@pytest.mark.parametrize('kind', ['analysis', 'claim', 'duplicate', 'reviewer', 'basis'])
def test_stale_or_ambiguous_adjudication_is_rejected(audit, evidence, kind):
    labels = evidence[3]
    if kind == 'analysis':
        labels['analysis_hash'] = 'old'
    elif kind == 'claim':
        labels['items'][0]['text_sha256'] = 'old'
    elif kind == 'duplicate':
        labels['items'] *= 2
    elif kind == 'reviewer':
        labels['reviewer'] = ''
    else:
        labels['items'][0]['basis'] = ''
    with pytest.raises(ValueError):
        assess(audit, evidence, adjudication=labels)


@pytest.fixture
def prepared(audit, tmp_path, monkeypatch):
    pdf = tmp_path/'paper.pdf'
    pdf.write_bytes(b'frozen offline test input, not a real PDF')
    fixture = tmp_path/'fixture.json'
    fixture.write_text(json.dumps({'papers': {'fasttext': {'sha256': audit.sha(pdf.read_bytes()), 'url': 'fixture'}}, 'cases': []}), encoding='utf-8')
    cross = tmp_path/'cross.json'
    cross.write_text('{"cases": []}', encoding='utf-8')
    monkeypatch.setattr(audit, 'FIXTURE', fixture)
    monkeypatch.setattr(audit, 'CROSS', cross)
    monkeypatch.setattr(audit, 'load_document', lambda _: SimpleNamespace(
        page_count=5, title='Offline acceptance', warnings=[], pages=[SimpleNamespace(number=1, text='text')]))
    monkeypatch.setattr(audit, 'chunk_document', lambda *a: [{'text': 'A sufficiently long source fact. '*35, 'page': 1, 'section': '结果'}])
    folder = tmp_path/'_selftest.n02'
    audit.prepare(pdf, folder, 'baseline')
    return folder, pdf


@pytest.mark.parametrize('changed', ['source.pdf', 'document.json', 'manifest.json'])
def test_changed_frozen_inputs_stop_before_client_is_used(audit, prepared, changed):
    folder, pdf = prepared
    if changed == 'manifest.json':
        data = json.loads((folder/changed).read_text(encoding='utf-8'))
        data['paper_stage_limits']['extract'] += 1
        audit.save(folder/changed, data)
    else:
        (folder/changed).write_text('modified', encoding='utf-8')
    with pytest.raises(ValueError):
        audit.verify(folder)
    assert not (folder/'started.json').exists()


def test_prepare_rejects_unrelated_pdf_and_existing_folder(audit, prepared):
    folder, pdf = prepared
    with pytest.raises(ValueError):
        audit.prepare(pdf, folder, 'baseline')
    pdf.write_bytes(b'another paper')
    with pytest.raises(ValueError):
        audit.prepare(pdf, folder.parent/'_selftest.other')


def test_transport_failure_is_persisted_unknown_and_never_retried(audit, prepared):
    folder, _ = prepared
    class FailedClient:
        name = audit.MODEL
        supports_semantic_review = True
        cfg = LLMConfig(provider='openai', model='deepseek/deepseek-v4.1-flash', base_url=audit.ENDPOINT,
                        temperature=0, max_tokens=8192, timeout=180)
        calls = 0
        def chat_json(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError('private-key-text')
    client = FailedClient()
    summary = audit.run(folder, client)
    assert client.calls == 1 and not summary['quality']['passed']
    assert summary['run']['status'] == 'failed'
    assert summary['usage']['calls'] == summary['usage']['unknown_usage_calls'] == 1
    assert summary['timing']['elapsed_seconds'] >= 0
    assert 'private-key-text' not in ''.join(p.read_text(encoding='utf-8') for p in folder.glob('*.json'))
    with pytest.raises(FileExistsError):
        audit.run(folder, client)
    assert client.calls == 1
    assert audit.report(folder)['usage'] == summary['usage']


def test_thinking_profile_is_frozen_and_not_a_global_default(audit, prepared):
    folder, pdf = prepared
    thinking = folder.parent/'_selftest.thinking'
    manifest = audit.prepare(pdf, thinking, 'thinking')
    assert manifest['max_output_tokens'] == 16384 and manifest['timeout_seconds'] == 300
    assert manifest['review_connect_timeout'] == 30 and manifest['review_read_timeout'] == 180
    assert audit.verify(thinking) == manifest
    assert audit.config(thinking).context.output_reserve_tokens == 16384
    assert audit.config(folder).context.output_reserve_tokens == 8192


def test_default_profile_uses_confirmed_capacity_and_automatic_allocation(audit, prepared):
    folder, pdf = prepared
    automatic = folder.parent/'_selftest.automatic'
    manifest = audit.prepare(pdf, automatic)
    assert manifest['profile'] == 'model-capacity'
    assert manifest['window_tokens'] == 1_000_000 and manifest['max_output_tokens'] == 384_000
    cfg = audit.config(automatic)
    from litprism.db import KnowledgeBase
    from litprism.providers import apply_active_window
    kb = KnowledgeBase(cfg)
    try:
        apply_active_window(cfg, kb)  # An isolated acceptance DB has no provider rows.
        assert cfg.llm.automatic_output and cfg.llm.model_max_output_tokens == 384_000
    finally:
        kb.close()
    assert audit.verify(automatic) == manifest


@pytest.mark.parametrize('provider', ['openai', 'anthropic'])
@pytest.mark.parametrize('limit,connect,read,expected', [
    (300, '30', '180', (30, 180)), (7, '30', '180', (7, 7)), (300, 'bad', 'bad', (10, 60)),
])
def test_connect_and_read_timeout_are_distinct_and_respect_user_limit(monkeypatch, provider, limit, connect, read, expected):
    import requests

    from litprism.llm import AnthropicClient, OpenAICompatClient
    monkeypatch.setenv('LITPRISM_REVIEW_CONNECT_TIMEOUT', connect)
    monkeypatch.setenv('LITPRISM_REVIEW_READ_TIMEOUT', read)
    settings = LLMConfig(provider=provider, timeout=limit)
    client = (AnthropicClient if provider == 'anthropic' else OpenAICompatClient)(settings)
    seen = []
    def post(*a, **kw):
        seen.append(kw['timeout'])
        return SimpleNamespace(status_code=200, json=lambda: {
            'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}],
            'content': [{'type': 'text', 'text': '{}'}], 'stop_reason': 'end_turn', 'usage': {}})
    monkeypatch.setattr(requests, 'post', post)
    client.chat_json([{'role': 'user', 'content': 'offline fixture'}], task='semantic_review')
    assert seen == [expected]


def test_observed_tang_continuation_cases_keep_pdf_and_source_scope():
    from pathlib import Path

    from litprism.semantics import make_units
    base = Path(__file__).resolve().parents[1]/'tools/fixtures'
    data = json.loads((base/'n02-fasttext-provenance-cases.json').read_text(encoding='utf-8'))
    existing = json.loads((base/'semantic-real-cases.json').read_text(encoding='utf-8'))
    assert data['paper_sha256'] == existing['papers']['fasttext']['sha256']
    assert {c['expected'] for c in data['cases']} == {'supported', 'contradicted', 'needs_review'}
    assert 'Tang' not in data['sources']['p3_intervening_table']['text']
    assert 'Tang' in data['sources']['p3_tang_continuation']['text']
    assert 'LSTM-GRNN' in data['sources']['p3_recurrent_baselines']['text']
    for case in data['cases']:
        sources = [{'id': sid, 'tag': f'P{i}', **data['sources'][sid]} for i, sid in enumerate(case['source_ids'], 1)]
        assert len(sources) <= 3 and all(s['page'] in (2, 3) for s in sources)
        assert len(make_units([('experiment_design', case['text'], [s['tag'] for s in sources])], sources, 1)) == 1


def test_failed_control_retains_sample_id_and_cannot_count_as_completed(audit):
    class Client:
        _n02_last_response = {'reviews': []}
        def chat_json(self, *a, **kw):
            raise TimeoutError('private error')
    case = {'id': 'failed-sample', 'expected': 'supported', 'source_ids': ['s'], 'text': 'A true fact.'}
    sources = {'s': {'paper': 'paper', 'page': 1, 'text': 'A true fact. '*30}}
    result = audit.local_control(case, sources, audit.config(audit.ROOT/'_selftest.unused'), Client())
    assert result['id'] == 'failed-sample' and not result['complete']
    assert result['error_type'] == 'TimeoutError' and not result['released']
    assert 'private error' not in json.dumps(result)


def test_cli_quality_failure_has_nonzero_exit_without_loading_provider(audit, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['n02_acceptance', 'report', '--output', str(tmp_path)])
    monkeypatch.setattr(audit, 'live_client', lambda *a: pytest.fail('Offline report must not access credentials'))
    assert audit.main() == 1
    assert json.loads(capsys.readouterr().out)['scope_passed'] is False


def test_controls_only_attempts_all_samples_and_preserves_transport_failure(audit, prepared):
    folder, pdf = prepared
    local = json.loads(audit.FIXTURE.read_text(encoding='utf-8'))
    local['sources'] = {'s': {'paper': 'p', 'page': 1, 'text': 'Actual source. '*30}}
    local['cases'] = [{'id': 'a', 'expected': 'supported', 'source_ids': ['s'], 'text': 'Actual source.'},
                      {'id': 'b', 'expected': 'blocked', 'source_ids': ['s'], 'text': 'Wrong claim.'}]
    audit.FIXTURE.write_text(json.dumps(local), encoding='utf-8')
    controls = folder.parent/'_selftest.controls'
    audit.prepare(pdf, controls, 'thinking-review', controls_only=True)
    class Client:
        name = audit.MODEL
        cfg = LLMConfig(provider='openai', model='deepseek/deepseek-v4.1-flash', base_url=audit.ENDPOINT,
                        temperature=0, max_tokens=16384, timeout=300)
        calls = 0
        def chat_json(self, messages, *, task=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError('private')
            request = json.loads(messages[-1]['content'])
            self.last_usage = {'prompt_tokens': 10, 'completion_tokens': 5}
            return {'reviews': [{'id': c['id'], 'verdict': 'contradicted', 'reason': 'offline'} for c in request['claims']]}
    client = Client()
    summary = audit.run(controls, client)
    assert client.calls == 2 and summary['scope'] == 'controls_only'
    metrics = summary['quality']['local_controls']
    assert metrics['attempted'] == 2 and metrics['completed'] == 1
    assert summary['usage']['unknown_usage_calls'] == summary['usage']['failed_calls'] == 1
    assert not (controls/'data/litprism.db').exists()
    assert not summary['scope_passed']


@pytest.mark.parametrize('count', [None, 1])
def test_recovery_report_never_invents_zero_replay_count(audit, prepared, count):
    folder, _ = prepared
    audit.save(folder/'recovery-policy.json', {} if count is None else {'confirmed_replayed_requests': count})
    summary = audit.report(folder)
    assert summary['replayed_requests'] == count
    assert summary['reporter_implementation_sha256'] == audit.implementation()


@pytest.mark.parametrize('malformed', ['missing_reason', 'duplicate_id'])
def test_incomplete_review_schema_cannot_count_as_complete_control(audit, malformed):
    class Client:
        def chat_json(self, messages, **kwargs):
            request = json.loads(messages[-1]['content'])
            row = {'id': request['claims'][0]['id'], 'verdict': 'supported', 'reason': 'valid', 'evidence': ['P1']}
            if malformed == 'missing_reason':
                row['reason'] = ''
            self._n02_last_response = {'reviews': [row, row] if malformed == 'duplicate_id' else [row]}
            return self._n02_last_response
    case = {'id': 'sample', 'expected': 'supported', 'source_ids': ['s'], 'text': 'Actual source.'}
    row = audit.local_control(case, {'s': {'paper': 'p', 'page': 1, 'text': 'Actual source. '*30}}, audit.config(audit.ROOT/'_selftest.unused'), Client())
    assert not row['complete'] and not row['released']
