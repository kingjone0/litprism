"""First-attempt probes must block input drift and auxiliary calls before paying."""
import hashlib
import importlib.util
from pathlib import Path

import pytest
from test_workflow_guards import env, paper  # noqa: F401


def tool():
    p = Path(__file__).resolve().parents[1] / 'tools/answer_units_live_acceptance.py'
    spec = importlib.util.spec_from_file_location('units_live', p)
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    return t


@pytest.mark.parametrize('task,count', [('expand', 0), ('qa', 1), ('semantic_review', 0)])
def test_unplanned_task_or_resend_blocked_before_dispatch(task, count):
    t = tool()
    messages = [{'content': '【片段】\noriginal\n\n【程序送达记录】\n{}'}]
    frozen = {'material_sha256': hashlib.sha256(b'original').hexdigest()}
    with pytest.raises(RuntimeError, match='before dispatch'):
        t.validate_dispatch(task, messages, frozen, count)


def test_source_drift_blocks_and_exact_frozen_source_allows_first_dispatch():
    t = tool()
    messages = [{'content': '【片段】\noriginal\n\n【程序送达记录】\n{}'}]
    frozen = {'material_sha256': hashlib.sha256(b'original').hexdigest()}
    t.validate_dispatch('qa', messages, frozen, 0)
    messages[0]['content'] = messages[0]['content'].replace('original', 'changed')
    with pytest.raises(RuntimeError, match='source input changed'):
        t.validate_dispatch('qa', messages, frozen, 0)


def test_input_freeze_runs_real_retrieval_without_sending_provider_calls(env):
    cfg, kb, real, _ = env
    cfg.qa.evidence_anchor_trial = True
    cfg.llm.model_window_tokens = cfg.context.window_tokens = 1000000
    pid = paper(kb)
    t = tool()
    frozen = t.freeze_question(kb, cfg, real, 'alphatopic', [pid])
    assert not real.calls and frozen['provider_calls'] == 0
    assert frozen['sources'][0]['chunk_id'] == kb.get_chunks(pid)[0]['id']
    assert frozen['retrieval']['mode'] == 'full_material'
    assert '【试验定位锚点' in frozen['messages'][-1]['content']


def test_acceptance_counts_new_answer_units_instead_of_reporting_zero_claims():
    t = tool()
    metrics = t.paired.measurements({'answer_units': [{'text': 'fact', 'quotes': [
        {'source': 'P1', 'anchor_id': 'qa2_original'}]}]}, {'items': [], 'warnings': []})
    assert metrics['claims_proposed'] == metrics['quotes_proposed'] == metrics['anchors_proposed'] == 1
    assert metrics['semantic_passed'] is None
