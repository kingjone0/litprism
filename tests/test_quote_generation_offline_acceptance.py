"""Frozen control edits must be explicit and cannot rewrite original fixtures."""
import copy
import importlib.util
from pathlib import Path

import pytest


def tool():
    path=Path(__file__).resolve().parents[1]/'tools/quote_generation_offline_acceptance.py'
    spec=importlib.util.spec_from_file_location('quote_generation_offline',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def fixture():
    return {'answer_units':[{'text':'Combined result.[P2]','subject':'Output','basis':'direct',
        'sources':['P2'],'quotes':[{'source':'P2','quote':'First half from previous source.'},
                                 {'source':'P2','quote':'Second half from current source.'}]}]}


def test_authored_cross_source_control_preserves_original_and_checks_actual_quotes():
    t=tool();value=fixture();original=copy.deepcopy(value)
    sources=[{'tag':'P1','text':'First half from previous source.'},{'tag':'P2','text':'Second half from current source.'}]
    assert t.replay(value,sources)['blocking_issues']
    changed=t.controlled_unit(value,{'unit_index':0,'original_unit_sha256':t.digest(value['answer_units'][0]),
        'replacement':{'text':'Combined result.[P1][P2]','sources':['P1','P2'],
            'quotes':[{'source':'P1','quote':sources[0]['text']},{'source':'P2','quote':sources[1]['text']}]}})
    assert value==original and not t.replay(changed,sources)['blocking_issues']
    assert t.replay(changed,sources)['semantic_status']=='unreviewed'


def test_control_cannot_be_applied_to_changed_original_or_change_subject_semantics():
    t=tool();value=fixture()
    control={'unit_index':0,'original_unit_sha256':t.digest(value['answer_units'][0]),
             'replacement':{'quotes':[]}}
    changed=copy.deepcopy(value);changed['answer_units'][0]['text']='Another answer.[P2]'
    with pytest.raises(ValueError,match='Original unit changed'):t.controlled_unit(changed,control)
    control['replacement']={'subject':'Different model'}
    with pytest.raises(ValueError,match='only changes'):t.controlled_unit(value,control)


def test_literal_control_does_not_clear_synonym_number_or_negation_changes():
    t=tool()
    for bad in ['We stated a 67% result.','We described a 68% result.','We did not describe a 67% result.']:
        value=fixture();u=value['answer_units'][0]
        u['quotes']=[{'source':'P2','quote':bad}]
        assert t.replay(value,[{'tag':'P2','text':'We described a 67% result.'}])['blocking_issues']


def test_frozen_headers_body_and_embedded_separator_are_preserved_without_metadata_guessing():
    t=tool()
    sources=[{'tag':'P1','text':'First source.\n\n---\n\nStill this source.'},
             {'tag':'P2','text':'Other source.'}]
    prefix='[P1] Actual title (2017), chapter X\n'+sources[0]['text']+'\n\n---\n\n[P2] Actual timestamp: original\n'+sources[1]['text']
    result=t.frame_frozen_sources(prefix,sources)
    assert '[P1] Actual title (2017), chapter X\n' in result
    assert '[P2] Actual timestamp: original\n' in result
    for s in sources:assert s['text'] in result
    with pytest.raises(ValueError,match='body differs'):
        t.frame_frozen_sources(prefix,[dict(sources[0],text='Changed'),sources[1]])
    with pytest.raises(ValueError,match='Extra text'):
        t.frame_frozen_sources(prefix+' unexpected',sources)
