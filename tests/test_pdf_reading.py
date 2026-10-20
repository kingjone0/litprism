"""Physical-page and table-binding counterexamples from the long-paper audit."""
import json
from pathlib import Path

import pytest

from litprism.ingest import Document, Page, Section, chunk_document, detect_sections, load_document
from litprism.pdf_layout import reading_layout


def test_toc_cannot_supply_body_evidence_or_reset_later_sections():
    pages = [Page(2, 'Contents\n5\nLimitations\n33\n8\nConclusion\n40'),
             Page(3, '1 Introduction\n'+'EARLY source. '*12),
             Page(33, '5 Limitations\n'+'ACTUAL limitation. '*12)]
    sections = detect_sections(pages)
    chunks = chunk_document(Document(sections=sections))
    # 章节名保留原文标题；归类（引言/局限）放在 role 里
    assert [s.name for s in sections] == ['目录', '1 Introduction', '5 Limitations']
    assert [s.role for s in sections] == ['目录', '引言', '局限']
    assert not any(c['page'] == 2 for c in chunks)
    assert all(c['page'] == 33 for c in chunks if 'ACTUAL' in c['text'])


def test_distant_same_name_sections_keep_physical_order():
    pages = [Page(3, '1 Method\n'+'EARLY. '*20), Page(4, '2 Results\n'+'MIDDLE. '*20),
             Page(43, '3 Method\n'+'APPENDIX. '*20)]
    chunks = chunk_document(Document(sections=detect_sections(pages)))
    assert [c['page'] for c in chunks] == [3, 4, 43]
    assert 'APPENDIX' not in chunks[0]['text']


@pytest.mark.parametrize('overlap', [0, 150])
def test_short_page_tail_is_preserved_without_cross_page_overlap(overlap):
    lines = [(11, 'FIRST '+('x'*120)), (11, 'Important qualification.'),
             (12, 'NEXT '+('y'*120)), (12, 'Another qualification.')]
    chunks = chunk_document(Document(sections=[Section(name='结果', lines=lines)]), overlap=overlap)
    assert len(chunks) == 2
    assert 'Important qualification.' in chunks[0]['text']
    assert 'Another qualification.' in chunks[1]['text']
    assert 'NEXT' not in chunks[0]['text'] and 'FIRST' not in chunks[1]['text']


def _table_page(duplicate_header=False, incomplete=False, numeric_headers=False):
    """In-memory PDF exercises actual geometry extraction, not a mocked cell matrix."""
    fitz = pytest.importorskip('pymupdf')
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for y in [100, 132, 194]:
        page.draw_line((60, y), (550, y))
    def centered(x, y, text):
        page.insert_text((x-fitz.get_text_length(text, fontsize=10)/2, y), text, fontsize=10)
    centered(310, 113, '12.3' if numeric_headers else 'Score' if duplicate_header else 'SuperGLUE')
    centered(310, 126, '' if duplicate_header or numeric_headers else 'Average')
    centered(480, 113, '45.6' if numeric_headers else 'Score' if duplicate_header else 'RTE')
    centered(480, 126, '' if duplicate_header or numeric_headers else 'Accuracy')
    for y, name, first, second in [(147, 'Fine-tuned SOTA', '89.0', '92.5'),
                                    (165, 'Fine-tuned BERT-Large', '69.0', '71.7'),
                                    (183, 'GPT-3 Few-Shot', '71.8', '69.0')]:
        page.insert_text((65, y), name, fontsize=10)
        centered(310, y, first)
        if not (incomplete and name == 'GPT-3 Few-Shot'):
            centered(480, y, second)
    page.insert_text((60, 215), 'Table 3.8: Comparison results.', fontsize=10)
    return doc, page


def test_table_rows_bind_average_and_rte_without_flattening_columns():
    doc, page = _table_page()
    try:
        text, tables = reading_layout(page)
        assert tables[0]['status'] == 'structured'
        records = [json.loads(line[line.index('{'):]) for line in text.splitlines() if line.startswith('【表格行】')]
        gpt = next(r for r in records if r['行'] == 'GPT-3 Few-Shot')
        assert gpt['SuperGLUE Average'] == '71.8'
        assert gpt['RTE Accuracy'] == '69.0'
        assert all('SuperGLUE Average' in r and 'RTE Accuracy' in r for r in records)
    finally:
        doc.close()


def test_side_by_side_tables_do_not_hide_each_other():
    fitz = pytest.importorskip('pymupdf')
    with fitz.open() as doc:
        page = doc.new_page()
        for left in [50, 330]:
            for y in [100, 130, 195]:
                page.draw_line((left, y), (left+220, y))
            page.insert_text((left+130, 118), 'Score', fontsize=10)
            for y, label, value in [(146, 'Alpha', '11.1'), (165, 'Beta', '22.2'), (185, 'Gamma', '33.3')]:
                page.insert_text((left+8, y), label, fontsize=10)
                page.insert_text((left+130, y), value, fontsize=10)
        _, tables = reading_layout(page)
        assert len(tables) == 2
        assert all(t['status'] == 'structured' for t in tables)


def test_numeric_data_rows_cannot_be_misread_as_column_headers():
    doc, page = _table_page(numeric_headers=True)
    try:
        _, tables = reading_layout(page)
        assert tables[0]['status'] == 'needs_review'
    finally:
        doc.close()


def test_small_reading_budget_cannot_split_a_header_value_record():
    from types import SimpleNamespace

    from litprism.config import Config
    from litprism.reading import reading_batches
    cfg = Config()
    cfg.context.window_tokens = 400
    cfg.context.output_reserve_tokens = 200
    row = '【表格行】'+json.dumps({'行': 'GPT', 'A very long header': '1'*3000})
    kb = SimpleNamespace(get_chunks=lambda _: [{'id': 'chunk', 'paper_id': 'paper', 'page': 1, 'text': row}])
    with pytest.raises(ValueError, match='完整表格行'):
        reading_batches(kb, cfg, [{'id': 'paper', 'title': 'Audit'}], 'Instruction')


def test_table_batch_boundary_stays_between_complete_rows():
    from types import SimpleNamespace

    from litprism.config import Config
    from litprism.context.meter import Meter
    from litprism.reading import excerpt_text, reading_batches
    cfg = Config()
    cfg.context.window_tokens = 500
    cfg.context.output_reserve_tokens = 200
    rows = ['【表格行】'+json.dumps({'行': label, 'Metric': '9'*700}) for label in ['One', 'Two']]
    kb = SimpleNamespace(get_chunks=lambda _: [{'id': 'chunk', 'paper_id': 'paper', 'page': 1,
                                              'text': '\n'.join(rows)}])
    batches, _, _ = reading_batches(kb, cfg, [{'id': 'paper', 'title': 'Audit'}], 'Instruction')
    assert len(batches) == 2
    assert [b[0]['text'].strip() for b in batches] == rows
    for batch in batches:
        assert sum(Meter(cfg).estimate_message(m) for m in [
            {'role': 'system', 'content': 'Instruction'},
            {'role': 'user', 'content': '\n\n'+excerpt_text(batch)}]) <= cfg.context.available_tokens


@pytest.mark.parametrize('duplicate,incomplete', [(True, False), (False, True)])
def test_ambiguous_or_incomplete_table_preserves_locator_without_guessing(duplicate, incomplete):
    doc, page = _table_page(duplicate, incomplete)
    try:
        text, tables = reading_layout(page)
        assert tables[0]['status'] == 'needs_review'
        assert '表格待核对' in text and 'p.1' in text
        assert '71.8' not in text and '92.5' not in text
        assert '71.8' in page.get_text()  # Original is not destroyed.
    finally:
        doc.close()


def test_frozen_real_paper_page_and_table_regression():
    path = Path(__file__).resolve().parents[1] / '_selftest.long-paper/gpt3-2005.14165v4.pdf'
    if not path.exists():
        pytest.skip('Explicit audit PDF not downloaded; offline synthetic geometry tests still run')
    doc = load_document(path)
    chunks = chunk_document(doc)
    assert [c['page'] for c in chunks] == sorted(c['page'] for c in chunks)
    limitations = [c for c in chunks if 'GPT-3 and our analysis of it have a number of limitations' in c['text']]
    assert limitations and all(c['page'] == 33 for c in limitations)
    assert not any(c['page'] == 2 for c in chunks)
    records = [t for t in doc.pages[18].tables if t['status'] == 'structured']
    assert len(records) == 2
    assert records[0]['headers'][1] == 'SuperGLUE Average'
    assert records[0]['rows'][2][1] == '71.8'
    assert records[0]['rows'][2][-1] == '69.0'
    assert doc.pages[62].tables[0]['status'] == 'needs_review'
    # Actual failures found during visual review: data-as-header, split words,
    # and lost row labels must not pass as recovered numeric tables.
    for index in [14, 15, 26]:
        assert doc.pages[index].tables
        assert all(t['status'] == 'needs_review' for t in doc.pages[index].tables)
    assert doc.warnings


def test_outline_defines_real_chapters_and_roles():
    """有 PDF 大纲时按大纲分节：标题保留原文，role 才是归类。"""
    from litprism.ingest import Page, detect_sections
    pages = [Page(1, 'title\n' * 4), Page(2, 'retrieval\n' * 4),
             Page(3, 'augment\n' * 4), Page(4, 'conclusion\n' * 4)]
    outline = [(1, '1 Introduction', 1), (1, '2 Retrieval', 2), (1, '3 Conclusion', 4)]
    sections = detect_sections(pages, outline)
    assert [s.name for s in sections] == ['1 Introduction', '2 Retrieval', '3 Conclusion']
    assert [s.role for s in sections] == ['引言', '正文', '结论']
    assert sections[1].start_page == 2
