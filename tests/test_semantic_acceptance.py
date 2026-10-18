"""The service acceptance gate cannot pass by abstaining or skipping cases."""
import importlib
import json
from pathlib import Path


def test_grade_rejects_false_support_abstention_and_partial_run(monkeypatch):
    monkeypatch.delenv('LITPRISM_HOME',raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools'))
    grade=importlib.import_module('audit_semantic_service').grade
    yes={'id':'true','expected':'supported','released':True,'raw_verdict':'supported','complete':True}
    no={'id':'false','expected':'blocked','released':False,'raw_verdict':'contradicted','complete':True}
    assert grade([yes,no],2)['passed']
    assert not grade([{**yes,'released':False},no],2)['passed']
    assert not grade([yes,{**no,'raw_verdict':'supported'}],2)['passed']
    assert not grade([yes,no],3)['passed']
    assert not grade([yes,{**no,'complete':False}],2)['passed']


def test_frozen_real_cases_are_balanced_and_have_page_and_file_provenance():
    from litprism.semantics import make_units
    fixture=json.loads((Path(__file__).resolve().parents[1]/'tools/fixtures/semantic-real-cases.json').read_text(encoding='utf-8'))
    assert len(fixture['cases'])==18
    assert len({c['id'] for c in fixture['cases']})==18
    for split in ['development','holdout']:
        cases=[c for c in fixture['cases'] if c['split']==split]
        assert sum(c['expected']=='supported' for c in cases)==sum(c['expected']=='blocked' for c in cases)
        for c in cases:
            excerpts=[{**fixture['sources'][sid],'id':sid,'tag':f'P{i}'} for i,sid in enumerate(c['source_ids'],1)]
            assert len(make_units([('key_conclusions',c['text'],[e['tag'] for e in excerpts])],excerpts,1))==1
            for e in excerpts:
                assert e['page']>=1 and len(e['text'])>100
                assert len(fixture['papers'][e['paper']]['sha256'])==64
