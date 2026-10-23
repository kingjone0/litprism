"""A real N04 paper returned objects in field_evidence and crashed merging."""
import pytest

from litprism.extract import _validate_fields


@pytest.mark.parametrize('reference', [{'source': 'P1 p.1'}, 1, ['P1 p.1'], None])
def test_non_string_references_are_structure_damage_not_stringified(reference):
    scope = [{'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 1, 'text': 'A method.'}]
    data, issues = _validate_fields({'methodology': 'A method [P1 p.1]',
                                     'field_evidence': {'methodology': [reference]}}, scope)
    assert data['methodology'] == ''
    assert data['field_evidence']['methodology'] == []
    assert any('结构损坏' in issue for issue in issues)


def test_damaged_reference_invalidates_the_batch_instead_of_releasing_other_fields():
    scope = [{'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 1, 'text': 'A method.'}]
    data, issues = _validate_fields({
        'methodology': 'A method [P1 p.1]', 'research_question': 'A question [P1 p.1]',
        'field_evidence': {'methodology': [{'source': 'P1 p.1'}], 'research_question': ['P1 p.1']}}, scope)
    assert not data['methodology'] and not data['research_question']
    assert issues


def test_valid_reference_strings_keep_source_validation():
    scope = [{'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 1, 'text': 'A method.'}]
    data, issues = _validate_fields({'methodology': 'A method [P1 p.1]',
                                     'field_evidence': {'methodology': ['P1 p.1']}}, scope)
    assert data['methodology'] and not issues


def test_structure_repair_names_actual_json_keys():
    from litprism.extract import _run_with_repair
    scope = [{'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 1, 'text': 'A method.'}]
    class Model:
        def __init__(self):
            self.requests = []
        def chat_json(self, messages, **kwargs):
            self.requests.append(messages)
            return {'BROKEN': 'bad'} if len(self.requests) == 1 else {'methodology': 'A method [P1 p.1]'}
    client = Model()
    data, _, _ = _run_with_repair(client, 'system', 'context', None, 'extract', scope, 'unit')
    assert data['methodology']
    repair = client.requests[-1][-1]['content']
    assert 'research_question' in repair and 'methodology' in repair


def test_reference_structure_failure_keeps_original_and_has_bounded_retry():
    import json
    from litprism.extract import _run_with_repair
    scope = [{'tag': 'P1', 'id': 'chunk1', 'paper_id': 'paper1', 'page': 1, 'text': 'A method.'}]
    class Model:
        def __init__(self):
            self.calls = 0
        def chat_json(self, messages, **kwargs):
            self.calls += 1
            return {'methodology': 'A method [P1 p.1]',
                    'field_evidence': {'methodology': [{'source': 'P1 p.1'}]}}
    client = Model()
    failures = []
    data, issues, gates = _run_with_repair(
        client, 'system', 'context', None, 'extract', scope, 'unit', failures=failures)
    assert client.calls == 2
    assert not data['methodology'] and issues and gates.failed
    assert len(failures) == 2
    assert json.loads(failures[0]['raw'])['field_evidence']['methodology'] == [{'source': 'P1 p.1'}]
