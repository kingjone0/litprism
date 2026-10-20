"""覆盖口径：按章节统计陈述，而不是按"指令的块有没有被引用"。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from litprism.extract import section_claim_counts


def _chunks():
    return [
        {'id': 'c1', 'section': '1 Introduction', 'section_role': '引言', 'text': 'a'},
        {'id': 'c2', 'section': '1 Introduction', 'section_role': '引言', 'text': 'b'},
        {'id': 'c3', 'section': '2 Retrieval', 'section_role': '正文', 'text': 'c'},
        {'id': 'c4', 'section': 'References', 'section_role': '参考文献', 'text': 'd'},
    ]


def test_counts_follow_section_of_cited_chunk():
    claims = [{'candidate_tags': ['P1']}, {'candidate_tags': ['P2'], 'adjacent_tags': ['P3']}]
    counts = section_claim_counts(claims, _chunks())
    assert counts == {'1 Introduction': 2, '2 Retrieval': 1}


def test_references_are_never_counted():
    counts = section_claim_counts([{'candidate_tags': ['P4']}], _chunks())
    assert counts == {}


def test_section_without_claims_is_visible():
    """跨节取证是合法的，所以判据是"这一章有没有被理解"，不是"每条指令的块被没被点"。"""
    counts = section_claim_counts([{'candidate_tags': ['P1']}], _chunks())
    names = {c['section'] for c in _chunks() if c['section_role'] != '参考文献'}
    assert [n for n in names if not counts.get(n)] == ['2 Retrieval']
