"""The offline harness must not introduce paid tasks or overwrite old evidence."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('retrieval_offline', Path(__file__).parents[1] / 'tools/qa_retrieval_offline_acceptance.py')
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


def test_replay_rejects_daily_data_and_existing_output(tmp_path):
    with pytest.raises(ValueError, match='_selftest'):
        offline.run(tmp_path / 'daily', tmp_path / '_selftest.output')
    source = tmp_path / '_selftest.source'
    output = tmp_path / '_selftest.output'
    output.mkdir()
    with pytest.raises(ValueError, match='new, independent'):
        offline.run(source, output)
    with pytest.raises(ValueError, match='new, independent'):
        offline.run(source, source / 'child')


def test_stub_only_replays_expansion_and_captures_input():
    expansion = {'keywords': ['attention']}
    model = offline.FrozenInput(expansion)
    received = model.chat_json([], task='expand')
    received['keywords'].append('changed')
    assert expansion == {'keywords': ['attention']}
    assert model.chat_json([{'content': 'evidence'}], task='qa')['insufficient_evidence']
    assert model.messages == [{'content': 'evidence'}]
    with pytest.raises(AssertionError, match='duplicate'):
        model.chat_json([], task='qa')
    with pytest.raises(AssertionError, match='Unplanned'):
        model.chat_json([], task='semantic_review')


def test_frozen_legacy_merge_still_exposes_term_starvation():
    question = '注意力如何计算并将各头输出拼接投影为最终结果，请综合可用论文与网页来源逐步给出直接依据，不扩展无关参数。' * 2
    terms = offline.legacy_terms(question, {'keywords': ['unique expansion phrase']})
    assert len(terms) == 26
    assert 'unique expansion phrase' not in terms
