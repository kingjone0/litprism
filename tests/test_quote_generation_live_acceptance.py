"""The live protocol is frozen and audited offline before any paid request."""
import copy
import importlib.util
from pathlib import Path

import pytest


def tool():
    path = Path(__file__).resolve().parents[1]/'tools/quote_generation_live_acceptance.py'
    spec = importlib.util.spec_from_file_location('quote_live', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fixture(t):
    sources = [{'tag': 'P1', 'text': 'We described a 67% result.\n\n---\n\nOther sentence.'},
               {'tag': 'P2', 'text': 'These outputs are concatenated.'}]
    messages = [{'role': 'system', 'content': 'old system\n\n'+t.HISTORY_GUARD+'\n\noriginal memory'},
                {'role': 'user', 'content': 'scope\n\n【片段】\n[P1] Original title\n'+sources[0]['text']
                 +'\n\n---\n\n[P2] Other title\n'+sources[1]['text']
                 +'\n\n【程序送达记录】\noriginal metadata\noriginal history\n【问题】original question\noriginal tail'}]
    return messages, sources


def test_reframes_only_contract_and_boundaries_preserving_data_and_question():
    t = tool(); messages, sources = fixture(t); original = copy.deepcopy(messages)
    result = t.make_request(messages, sources)
    assert messages == original
    assert result[0]['content'] == t.QA_SYSTEM+'\n\n'+t.HISTORY_GUARD+'\n\noriginal memory'
    for s in sources:
        assert f'【来源正文 {s["tag"]} 开始】\n'+s['text']+f'\n【来源正文 {s["tag"]} 结束】' in result[1]['content']
    assert result[1]['content'].split('【程序送达记录】')[1] == messages[1]['content'].split('【程序送达记录】')[1]+'\n'+t.QA_QUOTE_CHECK


@pytest.mark.parametrize('change', ['source', 'guard', 'roles', 'duplicate_check'])
def test_changed_historical_contract_rejected(change):
    t = tool(); messages, sources = fixture(t)
    if change == 'source': sources[0]['text'] = 'different actual content'
    elif change == 'guard': messages[0]['content'] = 'no known boundary'
    elif change == 'roles': messages.append({'role': 'assistant', 'content': 'unplanned'})
    else: messages[1]['content'] += t.QA_QUOTE_CHECK
    with pytest.raises(ValueError): t.make_request(messages, sources)


def response(quote='We described a 67% result.', source='P1'):
    return {'answer_units': [{'text': 'The result is 67%.[P1]', 'subject': 'Study', 'basis': 'direct',
            'sources': [source], 'quotes': [{'source': source, 'quote': quote}]}]}


def test_numbered_citations_are_tags_and_location_is_not_semantic_certification():
    t = tool(); _, sources = fixture(t)
    row = t.inspect_generation(response(), sources)
    assert row['citation_count'] == 1 and not row['unknown_citation_tags']
    assert row['proposed_quotes'] == row['byte_exact_quotes'] == row['located_quotes'] == 1
    assert row['semantic_judgment'] is None and row['historical_sources_not_current_trust_check']


@pytest.mark.parametrize('quote,source', [('We stated a 67% result.', 'P1'), ('We described a 67% result.', 'P2')])
def test_synonym_and_cross_block_misbinding_remain_blocked(quote, source):
    t = tool(); _, sources = fixture(t)
    assert t.inspect_generation(response(quote, source), sources)['audit']['blocking_issues']


def test_non_json_or_empty_answers_cannot_be_successful_inspections():
    t = tool()
    for value in [None, [], {}, {'answer_units': []}]:
        with pytest.raises(ValueError): t.inspect_generation(value, [])


def test_unknown_tags_are_not_counted_as_valid_frozen_citations():
    t = tool(); _, sources = fixture(t); value = response()
    value['answer_units'][0]['text'] = 'Unprovided source.[P77]'
    assert t.inspect_generation(value, sources)['unknown_citation_tags'] == ['P77']
