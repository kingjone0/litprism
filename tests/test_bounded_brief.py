"""有界对话概要：只回放"读过什么"，不回放原文，且明确禁止当作引用依据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from litprism.extract import bounded_brief


def test_empty_brief_is_empty_string():
    assert bounded_brief([]) == ''


def test_brief_lists_units_and_forbids_citation():
    entries = [{'unit': 'd1', 'section': 'Retrieval', 'claims': 12, 'fields': '方法、结论'}]
    text = bounded_brief(entries)
    assert 'Retrieval' in text and '12 条' in text
    assert '不得作为引用依据' in text
    assert '原文标签' in text


def test_brief_keeps_only_recent_units():
    entries = [{'unit': f'd{i}', 'section': f'sec{i}', 'claims': i, 'fields': '方法'} for i in range(1, 13)]
    text = bounded_brief(entries, limit=3)
    assert '- d12（' in text and '- d10（' in text
    assert '- d9（' not in text and '- d1（' not in text
