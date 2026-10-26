"""Authored comparison bindings must not masquerade as semantic inference."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('comparison_offline', Path(__file__).parents[1] / 'tools/qa_comparison_offline_acceptance.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def sample():
    sources = [{'tag': 'P1', 'text': 'A predicts next tokens. A translates sequences.'},
               {'tag': 'P2', 'text': 'Prior hybrid models combine memories.'}]
    first = {'text': 'A translates sequences.[P1]', 'subject': 'A tasks', 'basis': 'direct',
             'sources': ['P1'], 'quotes': [{'source': 'P1', 'quote': 'A translates sequences.'}]}
    goal = {'text': 'A predicts next tokens.[P1]', 'subject': 'A goal', 'basis': 'direct',
            'sources': ['P1'], 'quotes': [{'source': 'P1', 'quote': 'A predicts next tokens.'}]}
    return sources, first, goal


def slot(unit=None):
    from quote_coverage_offline_acceptance import witness
    sources, _, _ = sample()
    return {'id': 'a-goal', 'object': 'A', 'dimension': 'goal',
            'unit_sha256': [t.digest(unit)] if unit else [],
            'witnesses': [witness(sources[0], 'A predicts next tokens.')]}


def test_located_task_quote_does_not_fill_an_authored_goal_slot():
    sources, first, goal = sample()
    old = t.assess({'answer_units': [first]}, sources, [slot()])
    assert old['state'] == 'incomplete' and old['slots'][0]['state'] == 'unanswered'
    new = t.assess({'answer_units': [first, goal]}, sources, [slot(goal)])
    assert new['state'] == 'covered' and new['semantic_status'] == 'unreviewed'
    assert new['production_gate'] is False


def test_same_sentence_can_cover_multiple_explicitly_bound_dimensions():
    sources, _, _ = sample()
    unit = {'text': 'A predicts next tokens and translates sequences.[P1]', 'subject': 'A', 'basis': 'direct',
            'sources': ['P1'], 'quotes': [{'source': 'P1', 'quote': sources[0]['text']}]}
    slots = [slot(unit)]
    other = deepcopy(slots[0]); other.update(id='a-task', dimension='task')
    slots.append(other)
    assert t.assess({'answer_units': [unit]}, sources, slots)['state'] == 'covered'


def test_unannotated_dimensions_and_attribution_stay_unknown():
    sources, first, _ = sample()
    result = t.assess({'answer_units': [first]}, sources, [])
    assert result['state'] == result['attribution_state'] == 'not_assessed'


def test_explicit_dimension_binding_and_quote_containment_do_not_certify_prose():
    sources, _, goal = sample()
    goal['text'] = 'A has no prediction objective.[P1]'
    result = t.assess({'answer_units': [goal]}, sources, [slot(goal)])
    # The operator supplied the dimension membership; only its literal witness
    # is covered. This cannot become an automatic judgement of this wrong text.
    assert result['state'] == 'covered'
    assert result['slots'][0]['operator_assigned_dimension'] is True
    assert result['semantic_status'] == 'unreviewed' and not result['production_gate']


def test_changed_or_duplicate_answer_unit_cannot_inherit_bindings():
    sources, _, goal = sample()
    changed = deepcopy(goal); changed['text'] = 'A has no prediction objective.[P1]'
    with pytest.raises(ValueError, match='changed or is ambiguous'):
        t.assess({'answer_units': [changed]}, sources, [slot(goal)])
    with pytest.raises(ValueError, match='changed or is ambiguous'):
        t.assess({'answer_units': [goal, deepcopy(goal)]}, sources, [slot(goal)])


def test_same_words_under_another_source_do_not_cover_expected_witness():
    sources, _, goal = sample()
    sources.append({'tag': 'P3', 'text': sources[0]['text']})
    goal.update(sources=['P3'], quotes=[{'source': 'P3', 'quote': 'A predicts next tokens.'}])
    result = t.assess({'answer_units': [goal]}, sources, [slot(goal)])
    assert result['state'] == 'incomplete'


def test_not_located_is_an_addressed_local_gap_not_covered_evidence():
    sources, _, _ = sample()
    gap = {'text': 'An exact formula has not been located.', 'subject': 'A formula',
           'basis': 'not_located', 'sources': [], 'quotes': []}
    result = t.assess({'answer_units': [gap]}, sources, [slot(gap)])
    assert result['slots'][0]['answer_present'] and result['slots'][0]['state'] == 'not_located'
    assert result['semantic_status'] == 'unreviewed' and not result['production_gate']


def test_attribution_is_authored_and_bound_to_both_unit_and_source():
    sources, first, _ = sample()
    annotation = {'unit_sha256': t.digest(first), 'label': 'supported_wording',
                  'source_text_sha256': {'P1': t.digest(sources[0]['text'])}}
    result = t.assess({'answer_units': [first]}, sources, [], [annotation])
    assert result['attribution_annotations'][0]['automatically_inferred'] is False
    sources[0]['text'] = 'Changed evidence'
    with pytest.raises(ValueError, match='source changed'):
        t.assess({'answer_units': [first]}, sources, [], [annotation])


def test_proposes_and_discusses_are_not_classified_by_regex():
    sources, first, _ = sample()
    first['text'] = 'The authors propose a model.[P1]'
    assert t.assess({'answer_units': [first]}, sources, [])['attribution_state'] == 'not_assessed'
    first['text'] = 'The authors discuss a model.[P1]'
    assert t.assess({'answer_units': [first]}, sources, [])['attribution_state'] == 'not_assessed'


def test_control_changes_only_the_authored_copy_and_keeps_siblings():
    _, first, goal = sample()
    value = {'answer_units': [first], 'open_questions': ['Exact formula unknown']}
    original = deepcopy(value)
    control = {'original_answer_sha256': t.digest(value),
               'operation': {'kind': 'insert', 'unit_index': 1, 'unit': goal}}
    variant = t.apply_control(value, control)
    assert value == original and variant['answer_units'][0] == first
    assert variant['open_questions'] == value['open_questions']
    value['open_questions'] = []
    with pytest.raises(ValueError, match='Frozen answer changed'):
        t.apply_control(value, control)


def test_changed_replacement_hash_and_invalid_operation_are_rejected():
    _, first, goal = sample()
    value = {'answer_units': [first]}
    control = {'original_answer_sha256': t.digest(value),
               'operation': {'kind': 'replace', 'unit_index': 0, 'unit': goal, 'original_unit_sha256': 'wrong'}}
    with pytest.raises(ValueError, match='replacement unit changed'):
        t.apply_control(value, control)
    control['operation'].update(kind='insert', unit_index=True)
    with pytest.raises(ValueError, match='Invalid authored operation'):
        t.apply_control(value, control)


def test_duplicate_dimension_ids_and_changed_witness_are_rejected():
    sources, _, goal = sample(); s = slot(goal)
    with pytest.raises(ValueError, match='unique IDs'):
        t.assess({'answer_units': [goal]}, sources, [s, deepcopy(s)])
    sources[0]['text'] += ' changed'
    with pytest.raises(ValueError, match='source/span changed'):
        t.assess({'answer_units': [goal]}, sources, [s])


def test_request_replaces_only_contract_boundaries_without_touching_materials():
    messages = [{'role': 'system', 'content': 'OLD_SYSTEM\nMEMORY'},
                {'role': 'user', 'content': 'SOURCE\nHISTORY\nQUESTION\nOLD_CHECK'}]
    original = deepcopy(messages)
    updated = t.prepare_request(messages, {'system': 'OLD_SYSTEM', 'final_check': 'OLD_CHECK'})
    assert messages == original
    assert updated[0]['content'] == t.QA_SYSTEM + '\nMEMORY'
    assert updated[1]['content'] == 'SOURCE\nHISTORY\nQUESTION\n' + t.QA_QUOTE_CHECK
    with pytest.raises(ValueError, match='boundary differs'):
        t.prepare_request(messages, {'system': 'OTHER', 'final_check': 'OLD_CHECK'})


def test_existing_and_overlapping_output_are_rejected_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(t, 'ROOT', tmp_path)
    source = tmp_path / '_selftest.source'
    output = tmp_path / '_selftest.output'; output.mkdir()
    with pytest.raises(ValueError): t.validate_paths(source, output)
    with pytest.raises(ValueError): t.validate_paths(source, source)
    with pytest.raises(ValueError): t.validate_paths(source, source / '_selftest.nested')
