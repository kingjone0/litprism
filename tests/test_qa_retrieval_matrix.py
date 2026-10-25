"""A retrieval-only report must expose leaks and missing evidence, not pass QA."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_workflow_guards import env as workflow_env, paper

env = workflow_env
spec = importlib.util.spec_from_file_location('retrieval_matrix', Path(__file__).parents[1] / 'tools/qa_retrieval_matrix.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_capture_cannot_dispatch_expansion_or_generate_extra_answers():
    capture = matrix.Capture()
    with pytest.raises(AssertionError):
        capture.chat_json([], task='expand')
    messages = [{'role': 'user', 'content': 'original material'}]
    assert capture.chat_json(messages, task='qa')['insufficient_evidence']
    messages[0]['content'] = 'mutated'
    assert capture.messages[0]['content'] == 'original material'
    with pytest.raises(AssertionError):
        capture.chat_json([], task='qa')


def test_comparison_distinguishes_new_loss_from_existing_gap():
    before = {'witnesses_missing': ['old-gap', 'recovered'], 'expected_papers_missing': [], 'estimated_input_tokens': 100}
    after = {'witnesses_missing': ['old-gap', 'new-gap'], 'expected_papers_missing': ['missing-paper'], 'estimated_input_tokens': 200}
    assert matrix.compare(before, after) == {
        'new_missing_witnesses': ['new-gap'], 'recovered_witnesses': ['recovered'],
        'new_missing_papers': ['missing-paper'], 'estimated_input_delta': 100}


def test_refuses_existing_or_overlapping_output_before_opening_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, 'ROOT', tmp_path)
    source = tmp_path / '_selftest.source'
    baseline = tmp_path / '_selftest.baseline'
    output = tmp_path / '_selftest.output'
    output.mkdir()
    with pytest.raises(ValueError, match='new independent'):
        matrix.run(source, output, baseline, tmp_path / 'missing.json')
    with pytest.raises(ValueError):
        matrix.run(source, source, baseline, tmp_path / 'missing.json')


def test_isolation_rejects_daily_and_outside_workspace_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, 'ROOT', tmp_path)
    with pytest.raises(ValueError):
        matrix.isolated(tmp_path / 'daily-data')
    with pytest.raises(ValueError):
        matrix.isolated(tmp_path.parent / '_selftest.outside')


def delivery(kb, pid, *, chunk_ids=None, full=()):
    chunks = kb.get_chunks(pid)
    excerpts = [dict(c, chunk_id=c['id']) for c in chunks if chunk_ids is None or c['id'] in chunk_ids]
    result = SimpleNamespace(excerpts=excerpts, retrieval={'mode': 'passages', 'full_material_paper_ids': full})
    model = matrix.Capture()
    model.chat_json([{'role': 'user', 'content': 'bounded material'}], task='qa')
    return result, model


def test_scope_leak_is_not_confused_with_current_trust(env):
    cfg, kb, _, _ = env
    a = paper(kb, title='Allowed')
    b = paper(kb, title='Outside')
    result, model = delivery(kb, b)
    report = matrix.inspect_delivery(kb, cfg, result, model, scope=[a], expected_papers=[a], witnesses=[])
    assert not report['strict_scope_ok'] and report['current_trusted_materials_only']
    assert report['expected_papers_missing'] == [a]
    assert report['answer_quality'] == report['refusal_quality'] == 'not_evaluated'


def test_incomplete_material_and_missing_external_are_visible(env):
    cfg, kb, _, _ = env
    pid = paper(kb)
    kb.add_chunks(pid, [{'text': 'first', 'page': 1}, {'text': 'second', 'page': 2}])
    chunks = kb.get_chunks(pid)
    result, model = delivery(kb, pid, chunk_ids=[chunks[0]['id']], full=[pid])
    report = matrix.inspect_delivery(kb, cfg, result, model, scope=[pid], expected_papers=[pid],
                                     witnesses=[chunks[1]['id']], external=[{'id': 'missing-web'}])
    assert not report['full_material_record_correct']
    assert not report['external_preserved'] and report['witnesses_missing'] == [chunks[1]['id']]


def test_old_version_is_not_counted_as_current_evidence(env):
    cfg, kb, _, _ = env
    pid = paper(kb)
    result, model = delivery(kb, pid)
    kb.add_chunks(pid, [{'text': 'replacement', 'page': 2}])
    report = matrix.inspect_delivery(kb, cfg, result, model, scope=[pid], expected_papers=[pid], witnesses=[])
    assert not report['current_trusted_materials_only']
