"""Repair a source-valid citation layout defect without assigning evidence locally."""
from copy import deepcopy

import pytest

from litprism.extract import _FIELDS, _run_with_repair
from litprism.gates import MAX_REPAIR_ROUNDS, evaluate, repair_text
from litprism.plan import Directive, SectionRun


SCOPE = [dict(tag='P1', id='c1', paper_id='p1', page=1, text='A documented method.'),
         dict(tag='P2', id='c2', paper_id='p1', page=2, text='We plan to release code.')]
DIRECTIVE = Directive('d1', [SectionRun('Methods', ['c1', 'c2'], 600)])
BAD = {'methodology': '本文采用有记录的方法 [P1 p.1]。',
       'innovations': '组合已有方法；公开代码供后续研究。',
       'field_evidence': {'methodology': ['P1 p.1'], 'innovations': ['P1 p.1', 'P2 p.2']}}
GOOD = {'methodology': BAD['methodology'],
        'innovations': '作者报告方法组合 [P1 p.1]。作者计划公开代码 [P2 p.2]。',
        'field_evidence': deepcopy(BAD['field_evidence'])}


class Model:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def chat_json(self, messages, *, task):
        assert task == 'extract'
        self.requests.append(deepcopy(messages))
        value = self.responses[min(len(self.requests)-1, len(self.responses)-1)]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def run(model, **kwargs):
    return _run_with_repair(model, 'fixed system', 'fixed paper', DIRECTIVE,
                            'fallback', SCOPE, 'd1', **kwargs)


def test_valid_field_references_allow_citation_format_repair_without_local_assignment():
    model = Model([BAD, GOOD])
    original = deepcopy(BAD)
    data, issues, report = run(model)
    assert len(model.requests) == 2 and not report.failed and not issues
    assert data['innovations'] == GOOD['innovations']
    assert BAD == original  # No in-place citation attachment or plan-tense rewriting.
    first, second = model.requests
    assert first[0] == second[0]
    assert second[-1]['content'].startswith('fixed paper\n\n=== 本次任务 ===\n'+DIRECTIVE.text)
    assert '行内引用' in second[-1]['content'] and '完整' in second[-1]['content']
    assert '低于预期' not in second[-1]['content']  # Citation failure is not a recall failure.
    assert BAD['innovations'] not in second[-1]['content']


def test_citation_repair_stops_at_native_limit_and_preserves_unresolved_flag():
    model = Model([BAD])
    data, _, report = run(model)
    assert len(model.requests) == MAX_REPAIR_ROUNDS+1
    assert report.failed
    assert any(g.name == 'has_citation' and not g.ok for g in report.results)
    assert data['innovations'] == BAD['innovations']
    assert '[P' not in data['innovations']


def test_out_of_whitelist_repair_does_not_displace_the_source_valid_candidate():
    invalid = deepcopy(GOOD)
    invalid['innovations'] = '作者计划公开代码 [P99 p.2]。'
    model = Model([BAD, invalid])
    data, _, report = run(model)
    assert report.failed
    assert data['innovations'] == BAD['innovations']
    assert 'P99' not in str(data)


@pytest.mark.parametrize('refs', [[], ['P99 p.2'], ['P1 p.9']])
def test_missing_or_invalid_evidence_is_not_the_citation_layout_repair(refs):
    raw = deepcopy(BAD)
    raw['field_evidence']['innovations'] = refs
    model = Model([raw])
    data, issues, report = run(model)
    assert len(model.requests) == 1 and issues and report.failed
    assert data['innovations'] == ''


def test_non_planned_legacy_call_keeps_its_existing_no_repair_boundary():
    model = Model([BAD, GOOD])
    _, _, report = _run_with_repair(model, 'S', 'P', None, 'F', SCOPE, 'b1')
    assert len(model.requests) == 1 and report.failed


def test_preflight_reserves_both_possible_repair_hints():
    report = evaluate(BAD, _FIELDS, expected=DIRECTIVE.expected)
    largest = repair_text(DIRECTIVE, 0)
    actual = repair_text(DIRECTIVE, 2, report)
    assert '行内引用' in largest and '低于预期' in largest
    assert len(largest) >= len(actual)


def test_repair_audit_retains_first_failure_and_retry_decision():
    audit = []
    model = Model([BAD, GOOD])
    run(model, repairs=audit)
    assert len(audit) == 1
    assert audit[0]['unit'] == 'd1' and audit[0]['attempt'] == 1
    assert audit[0]['retry'] is True
    assert any(g['name'] == 'has_citation' and not g['ok'] for g in audit[0]['gates']['results'])


def test_transport_error_during_repair_is_not_retried_as_a_format_problem():
    model = Model([BAD, TimeoutError('unknown request outcome')])
    with pytest.raises(TimeoutError):
        run(model)
    assert len(model.requests) == 2


def test_one_cited_sentence_does_not_hide_an_uncited_contribution():
    raw = deepcopy(BAD)
    raw['innovations'] = '作者报告方法组合 [P1 p.1]。作者计划公开代码。'
    model = Model([raw, GOOD])
    _, _, report = run(model)
    assert len(model.requests) == 2 and not report.failed


def test_citations_after_sentence_punctuation_still_belong_to_that_sentence():
    raw = deepcopy(GOOD)
    raw['innovations'] = '作者报告方法组合。[P1 p.1] 作者计划公开代码。[P2 p.2]'
    model = Model([raw])
    data, _, report = run(model)
    assert len(model.requests) == 1 and not report.failed
    assert data['innovations'] == raw['innovations']
