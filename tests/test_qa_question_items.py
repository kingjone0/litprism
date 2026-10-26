"""Question-item links are structural diagnostics, never entailment verdicts."""
from copy import deepcopy
import hashlib

import pytest

from litprism.qa_answer import compile_answer
from litprism.qa_claims import audit_map
from litprism.qa_question_items import record


def sample():
    return {'question_items': [{'id': 'Q1', 'object': 'A', 'aspect': 'prediction objective'},
                               {'id': 'Q2', 'object': 'A', 'aspect': 'loss formula'}],
            'answer_units': [
                {'text': 'A predicts the next symbol.[P1]', 'subject': 'A', 'basis': 'direct',
                 'addresses': ['Q1'], 'sources': ['P1'],
                 'quotes': [{'source': 'P1', 'quote': 'A predicts the next symbol.'}]},
                {'text': 'The exact loss formula has not been located.', 'subject': 'A formula',
                 'basis': 'not_located', 'addresses': ['Q2'], 'sources': [], 'quotes': []}]}


def inspect(value, question='Describe A prediction objective and loss formula.'):
    answer, mapping, bindings, protocol = compile_answer(value)
    audited = audit_map(answer, mapping, [{'tag': 'P1', 'text': 'A predicts the next symbol.'}],
                        answer_bindings=bindings, answer_protocol=protocol)
    return record(question, value, answer, audited['items'])


def test_text_and_formula_gap_remain_distinct_and_bound_to_server_owned_positions():
    value = sample(); before = deepcopy(value)
    result = inspect(value)
    assert value == before and result['state'] == 'recorded'
    assert [i['state'] for i in result['items']] == ['text_linked', 'gap_only']
    assert result['items'][0]['units'][0]['answer_span']['text'] == value['answer_units'][0]['text']
    assert result['semantic_status'] == 'unreviewed'
    assert result['question_item_completeness'] == 'unknown' and not result['production_gate']


def test_single_sentence_can_address_multiple_items_and_one_item_can_bind_multiple_units():
    value = sample()
    value['answer_units'][0]['addresses'] = ['Q1', 'Q2']
    value['answer_units'][1]['addresses'] = ['Q1', 'Q2']
    result = inspect(value)
    assert [i['state'] for i in result['items']] == ['text_and_gap', 'text_and_gap']
    assert all(len(i['units']) == 2 for i in result['items'])


def test_unlinked_item_and_open_question_do_not_count_as_addressed_body():
    value = sample(); value['answer_units'][0]['addresses'] = []
    value['open_questions'] = ['What is the prediction objective?']
    result = inspect(value)
    assert result['items'][0]['state'] == 'unmapped'


def test_wrong_prose_and_model_omitted_required_item_cannot_be_certified_by_complete_links():
    value = sample(); value['question_items'].pop(); value['answer_units'].pop()
    value['answer_units'][0]['text'] = 'A has no prediction objective.[P1]'
    result = inspect(value)
    assert result['state'] == 'recorded' and result['items'][0]['state'] == 'text_linked'
    assert result['question_item_completeness'] == 'unknown'
    assert result['semantic_status'] == 'unreviewed' and not result['production_gate']


@pytest.mark.parametrize('declared', [None, {}, [], [None], [{'id': 'Q1', 'object': 'A'}]])
def test_invalid_declarations_are_diagnostics_without_changing_answer(declared):
    value = sample(); value['question_items'] = declared
    assert inspect(value)['state'] == 'invalid'
    assert value['answer_units'][0]['text'] == 'A predicts the next symbol.[P1]'


def test_duplicate_ids_and_unknown_or_malformed_references_are_not_silently_rebound():
    value = sample(); value['question_items'].append(deepcopy(value['question_items'][0]))
    assert inspect(value)['issues'] == ['duplicate_question_item_id']
    value = sample(); value['answer_units'][0]['addresses'] = ['Q99']
    result = inspect(value)
    assert result['state'] == 'invalid' and result['items'][0]['state'] == 'unmapped'
    value['answer_units'][0]['addresses'] = 'Q1'
    assert inspect(value)['state'] == 'invalid'
    value['answer_units'][0]['addresses'] = [True]
    assert inspect(value)['state'] == 'invalid'
    value['answer_units'][0]['addresses'] = ['Q1', 'Q1']
    assert inspect(value)['state'] == 'invalid'


def test_missing_and_legacy_metadata_stay_unknown_without_invented_items():
    value = sample(); value.pop('question_items')
    result = inspect(value)
    assert result['state'] == 'missing' and result['items'] == []
    legacy = {'answer': 'A predicts the next symbol.[P1]', 'question_items': sample()['question_items']}
    assert inspect(legacy)['state'] == 'unavailable'


def test_model_success_labels_and_offsets_are_ignored_but_server_binding_changes_are_rejected():
    value = sample()
    value['question_items'][0].update(state='passed', semantic_status='supported', sources=['P99'])
    value['answer_units'][0].update(start=999, answer_binding={'state': 'supported'})
    result = inspect(value)
    assert 'sources' not in result['items'][0] and result['semantic_status'] == 'unreviewed'
    answer, mapping, bindings, protocol = compile_answer(value)
    audited = audit_map(answer, mapping, [{'tag': 'P1', 'text': 'A predicts the next symbol.'}],
                        answer_bindings=bindings, answer_protocol=protocol)['items']
    audited[0]['answer_binding']['span']['start'] += 1
    assert record('A?', value, answer, audited)['state'] == 'invalid'


def test_record_is_bound_to_current_question_and_answer_and_preserves_invalid_source_state():
    value = sample()
    value['answer_units'][0]['quotes'][0]['quote'] = 'invented source sentence'
    result = inspect(value, question='Different question')
    assert result['question_sha256'] == hashlib.sha256(b'Different question').hexdigest()
    assert result['answer_sha256'] == hashlib.sha256(compile_answer(value)[0].encode()).hexdigest()
    assert result['items'][0]['units'][0]['source_record_state'] == 'invalid'
    assert result['semantic_status'] == 'unreviewed'
