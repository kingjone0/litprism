"""Explicit witness containment never becomes general semantic certification."""
import copy
import importlib.util
from pathlib import Path

import pytest


def tool():
    path = Path(__file__).resolve().parents[1]/'tools/quote_coverage_offline_acceptance.py'
    spec = importlib.util.spec_from_file_location('quote_coverage', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def sample():
    sources = [{'tag': 'P1', 'text': 'Parallel attention is computed. Outputs are concatenated. Different subspaces are used.'}]
    unit = {'text': 'Attention is computed in parallel and outputs concatenated.[P1]', 'subject': 'Attention',
            'basis': 'direct', 'sources': ['P1'], 'quotes': [{'source': 'P1', 'quote': 'Different subspaces are used.'}]}
    return sources, unit


def test_located_quote_can_still_miss_operator_declared_witnesses():
    t = tool(); sources, unit = sample()
    assert not t.replay({'answer_units': [unit]}, sources)['blocking_issues']
    witnesses = [t.witness(sources[0], 'Parallel attention is computed.'), t.witness(sources[0], 'Outputs are concatenated.')]
    assert t.annotated_coverage(unit, sources, witnesses)['state'] == 'incomplete'
    unit['quotes'] = [{'source': 'P1', 'quote': sources[0]['text']}]
    result = t.annotated_coverage(unit, sources, witnesses)
    assert result['state'] == 'covered' and result['semantic_status'] == 'unreviewed'


def test_no_annotation_is_unknown_even_when_quote_locates():
    t = tool(); sources, unit = sample()
    assert t.annotated_coverage(unit, sources, [])['state'] == 'not_assessed'


def test_same_text_in_another_source_does_not_cover_selected_witness():
    t = tool(); sources, unit = sample(); sources.append({'tag': 'P2', 'text': sources[0]['text']})
    unit['quotes'] = [{'source': 'P2', 'quote': sources[0]['text']}]
    assert t.annotated_coverage(unit, sources, [t.witness(sources[0], sources[0]['text'])])['state'] == 'incomplete'


def test_known_available_part_is_separate_from_unassessed_missing_formula():
    t = tool(); sources, unit = sample(); unit.update(basis='not_located', sources=[], quotes=[])
    assert t.annotated_coverage(unit, sources, [t.witness(sources[0], 'Parallel attention is computed.')])['state'] == 'incomplete'
    assert t.annotated_coverage(unit, sources, [])['state'] == 'not_assessed'
    assert not t.replay({'answer_units': [unit]}, sources)['blocking_issues']


def test_controls_are_hash_bound_and_cannot_rewrite_sibling_units():
    t = tool(); sources, unit = sample(); original = {'answer_units': [unit, copy.deepcopy(unit)]}
    before = copy.deepcopy(original)
    control = {'unit_index': 0, 'original_unit_sha256': t.digest(unit), 'replacement': {'quotes': []}}
    variant = t.apply_control(original, control)
    assert original == before and variant['answer_units'][1] == before['answer_units'][1]
    original['answer_units'][0]['text'] = 'Changed'
    with pytest.raises(ValueError, match='Original unit changed'): t.apply_control(original, control)


def test_witness_hash_and_exact_span_reject_changed_source():
    t = tool(); sources, unit = sample(); item = t.witness(sources[0], 'Outputs are concatenated.')
    sources[0]['text'] = 'Modified source'
    with pytest.raises(ValueError, match='source/span changed'): t.annotated_coverage(unit, sources, [item])


def test_duplicate_or_invented_witness_requires_explicit_resolving_instead_of_guess():
    t = tool()
    with pytest.raises(ValueError, match='unique literal'): t.witness({'tag': 'P1', 'text': 'Same. Same.'}, 'Same.')
    with pytest.raises(ValueError, match='unique literal'): t.witness({'tag': 'P1', 'text': 'We described it.'}, 'We stated it.')


def test_bad_quote_is_still_blocked_without_new_semantic_threshold():
    t = tool(); sources, unit = sample(); unit['quotes'][0]['quote'] = 'Outputs are not concatenated.'
    assert t.replay({'answer_units': [unit]}, sources)['blocking_issues']
