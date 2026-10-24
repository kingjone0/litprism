"""Offline refinement may reduce known locations; never guess a replacement source."""
import copy
import importlib.util
from pathlib import Path

from litprism.qa_claims import audit_map
from litprism.qa_anchors import _paragraphs


def tool():
    path = Path(__file__).resolve().parents[1] / 'tools/answer_anchor_offline_acceptance.py'
    spec = importlib.util.spec_from_file_location('answer_anchor_offline', path)
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    return t


def item(quote):
    return {'claim': '模型转述。[P1]', 'subject': '方法', 'basis': 'direct', 'sources': ['P1'], 'quotes': [quote]}


def test_literal_control_refines_exact_ranges_without_repairing_bad_quotes():
    t = tool()
    source = {'tag': 'P1', 'chunk_id': 'original', 'text': 'First actual fact. Second actual fact. Unrelated context follows.'}
    records = [item({'source': 'P1', 'quote': 'First actual fact. Second actual fact.'}),
               item({'source': 'P1', 'quote': 'An invented result.'})]
    original = copy.deepcopy(records)
    refined, controls = t.refine_located_quotes(records, [source])
    assert records == original and len(controls) == 1
    assert controls[0]['fine_characters'] < controls[0]['coarse_characters']
    old = audit_map('模型转述。[P1]', records, [source])
    new = audit_map('模型转述。[P1]', refined, [source], anchor_trial=True)
    assert new['blocking_issues'] == old['blocking_issues']
    assert [q['quote'] for q in new['items'][0]['quotes']] == ['First actual fact.', 'Second actual fact.']
    assert new['semantic_status'] == 'unreviewed'


def test_legacy_anchor_is_not_narrowed_using_claim_similarity_or_other_sources():
    t = tool()
    source = {'tag': 'P1', 'chunk_id': 'original', 'text': 'Our model uses fixed features. Another model uses retrieval.'}
    other = dict(source, tag='P2', chunk_id='different')
    original = [item({'source': 'P1', 'anchor_id': _paragraphs(source)[0]['id']}),
                item({'source': 'P1', 'quote': 'An invented result.'})]
    refined, controls = t.refine_located_quotes(original, [source, other])
    assert refined == original and not controls
