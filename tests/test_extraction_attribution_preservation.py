"""Local extraction must preserve authorship/modality, not silently repair prose.

These are synthetic transport/storage tests, not an automated semantic judge.
Real-model adherence is measured separately on the frozen reading acceptance.
"""
import copy

import pytest
from test_workflow_guards import env, paper  # noqa: F401

from litprism.extract import analyze_paper


SOURCE = (
    'Background baselines use independent count features or a margin classifier. '
    'Our method instead averages shared representations and predicts labels with a normalized classifier. '
    'The observed gap could be explained by omitting external representations; causality was not established. '
    'It is unclear whether this small benchmark is appropriate. We plan to release code.'
)


@pytest.mark.parametrize('field,text', [
    ('methodology', '背景常见基线使用独立计数特征，而本文方案平均共享表示并预测标签 [P1 p.1]。'),
    ('limitations', '作者认为差异可能与未使用外部表示有关，尚未证实因果 [P1 p.1]。'),
    ('limitations', '作者尚不清楚小型基准是否适合评估，并计划公开代码 [P1 p.1]。'),
    # A mechanical citation pass cannot silently fix or approve these errors.
    ('methodology', '本文采用背景中列举的间隔分类器 [P1 p.1]。'),
    ('limitations', '作者证实差异由外部表示导致，代码已发布 [P1 p.1]。'),
])
def test_candidate_storage_retains_exact_prose_without_semantic_approval(env, field, text):
    cfg, kb, _, _ = env
    pid = paper(kb, text=SOURCE)
    returned = {field: text, 'field_evidence': {field: ['P1 p.1']}}
    before = copy.deepcopy(returned)

    class Extractor:
        name = 'synthetic-attribution-transport'

        def chat_json(self, messages, *, task, **kwargs):
            assert task == 'extract'  # No new judge, concepts or automatic correction request.
            return copy.deepcopy(returned)

    result = analyze_paper(kb, cfg, Extractor(), pid, with_concepts=False, semantic_review=False)
    assert returned == before
    assert result[field].split('\n依据：')[0] == text
    assert result['_semantic_review']['status'] == 'skipped_by_user'
    assert not result['_semantic_review']['passed']
    assert result['_citation_check']['passed']
    assert [u['text'] for u in result['_candidates']] == [text]
    snapshot = kb.get_analysis(pid, version_id=result['_candidate_snapshot_id'])['payload']
    assert snapshot['_candidates'][0]['text'] == text
    assert not snapshot['_semantic_review']['passed']


def test_no_limitation_is_synthesized_from_background_terms(env):
    cfg, kb, _, _ = env
    pid = paper(kb, text=SOURCE)

    class Extractor:
        name = 'synthetic-empty-limitations'

        def chat_json(self, messages, *, task, **kwargs):
            assert task == 'extract'
            return {'methodology': '本文采用共享表示并预测标签 [P1 p.1]。', 'limitations': '',
                    'field_evidence': {'methodology': ['P1 p.1']}}

    result = analyze_paper(kb, cfg, Extractor(), pid, with_concepts=False, semantic_review=False)
    assert result['limitations'] == ''
    assert not any(u['field'] == 'limitations' for u in result['_candidates'])
