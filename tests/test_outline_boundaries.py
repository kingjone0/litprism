"""Outline page numbers locate pages, not every chapter boundary on that page."""
from litprism.ingest import Document, Page, chunk_document, detect_sections
from litprism.plan import build_plan


def bodies(sections):
    return [(page, line) for section in sections for page, line in section.lines]


def test_same_page_chapters_split_at_full_heading_without_losing_continuations():
    pages = [
        Page(1, 'Paper title\nAbstract content\n1\nIntroduction\nINTRO body\n2\nModel architecture\nMETHOD start'),
        Page(2, 'METHOD continuation\n2.1\nHierarchical softmax\nHuffman method\n'
                '2.2\nN-gram features\nHashing method\n3\nExperiments\nEXPERIMENT start'),
        Page(3, 'EXPERIMENT continuation\n4\nDiscussion and conclusion\nCONCLUSION body'),
    ]
    outline = [(1, '1 Introduction', 1), (1, '2 Model architecture', 1),
               (2, '2.1 Hierarchical softmax', 2), (2, '2.2 N-gram features', 2),
               (1, '3 Experiments', 2), (1, '4 Discussion and conclusion', 3)]
    sections = detect_sections(pages, outline)
    assert [s.name for s in sections] == [
        '正文', '1 Introduction', '2 Model architecture', '3 Experiments', '4 Discussion and conclusion']
    assert bodies(sections) == [(p.number, ln) for p in pages for ln in p.text.split('\n')]
    method = sections[2]
    assert method.role == '方法'
    assert method.lines[0] == (1, '2')
    assert (2, 'Huffman method') in method.lines and (2, 'Hashing method') in method.lines
    assert 'EXPERIMENT' not in method.text
    assert (3, 'EXPERIMENT continuation') in sections[3].lines


def test_reading_layout_is_the_source_and_table_model_does_not_create_a_section():
    pages = [Page(1, 'RAW SHOULD NOT BE USED', reading_text=
                  '1. Introduction\nINTRO\n2. Model architecture\nMETHOD\nModel\nevaluation\n'
                  '【表格行】Model=A | Score=1\n3. Results\nRESULT')]
    outline = [(1, '1 Introduction', 1), (1, '2 Model architecture', 1), (1, '3 Results', 1)]
    sections = detect_sections(pages, outline)
    assert [s.name for s in sections] == ['1 Introduction', '2 Model architecture', '3 Results']
    assert 'Model\nevaluation\n【表格行】' in sections[1].text
    assert bodies(sections) == [(1, ln) for ln in pages[0].reading_text.split('\n')]


def test_unlocated_same_page_boundary_keeps_all_names_and_explicit_uncertainty():
    pages = [Page(1, '1 Introduction\nINTRO\nMethod title not recoverable\nMETHOD'),
             Page(2, '3 Results\nRESULT')]
    sections = detect_sections(pages, [(1, '1 Introduction', 1),
                                       (1, '2 Model architecture', 1), (1, '3 Results', 2)])
    assert '1 Introduction' in sections[0].name and '2 Model architecture' in sections[0].name
    assert '边界未定位' in sections[0].name and sections[0].role == '正文'
    assert bodies(sections) == [(p.number, ln) for p in pages for ln in p.text.split('\n')]


def test_duplicate_or_out_of_order_matches_do_not_guess_a_boundary():
    for page_text in ['1 Introduction\nINTRO\n2 Methods\nMETHOD\n2 Methods\nTABLE',
                      '2 Methods\nMETHOD\n1 Introduction\nINTRO']:
        pages = [Page(1, page_text), Page(2, '3 Results\nRESULT')]
        sections = detect_sections(pages, [(1, '1 Introduction', 1), (1, '2 Methods', 1),
                                           (1, '3 Results', 2)])
        assert '边界未定位' in sections[0].name
        assert bodies(sections) == [(p.number, ln) for p in pages for ln in p.text.split('\n')]


def test_outline_level_fallback_keeps_repeated_titles_in_source_order():
    pages = [Page(1, 'Overview\nINTRO\nAblation\nFIRST'),
             Page(2, 'Other\nSECOND\nAblation\nTHIRD')]
    outline = [(1, 'Overview', 1), (2, 'Ablation', 1), (2, 'Other', 2), (2, 'Ablation', 2)]
    sections = detect_sections(pages, outline)
    assert [s.name for s in sections] == ['Overview', 'Ablation', 'Other', 'Ablation']
    assert bodies(sections) == [(p.number, ln) for p in pages for ln in p.text.split('\n')]


def test_short_outline_and_out_of_range_entries_keep_body_without_fake_sections():
    pages = [Page(1, 'Introduction\nINTRO\nMethods\nMETHOD\nResults\nRESULT')]
    outline = [(1, 'Introduction', 1), (1, 'Methods', 1), (1, 'Ghost', 99), (1, 'Results', 1)]
    sections = detect_sections(pages, outline)
    assert [s.name for s in sections] == ['Introduction', 'Methods', 'Results']
    assert 'Ghost' not in ' '.join(s.name for s in sections)
    assert bodies(sections) == [(1, ln) for ln in pages[0].text.split('\n')]


def test_references_and_appendix_still_split_after_precise_outline_boundaries():
    pages = [Page(1, '1 Introduction\n'+'INTRO '*30+'\n2 Methods\n'+'METHOD '*30),
             Page(2, '3 Conclusion\n'+'CONCLUSION '*30+'\nReferences\n'+'CITATION '*30),
             Page(3, 'A\nAdditional Details\n'+'APPENDIX '*30)]
    sections = detect_sections(pages, [(1, '1 Introduction', 1), (1, '2 Methods', 1),
                                       (1, '3 Conclusion', 2)])
    chunks = chunk_document(Document(sections=sections), overlap=0)
    assert not any('CITATION' in c['text'] for c in chunks)
    assert any(c['page'] == 3 and c['section_role'] == '附录' for c in chunks)
    assert [c['page'] for c in chunks] == sorted(c['page'] for c in chunks)


def test_plan_targets_include_embedded_method_subsections_despite_old_section_labels():
    class Meter:
        def estimate_text(self, text):
            return max(1, len(text)//4)

    chunks = [{'id': 'old_chunk', 'section': '1 Introduction', 'page': 2,
               'text': '2.1 Hierarchical softmax\nHuffman tree\n2.2 N-gram features\nHashing'}]
    plan = build_plan({'id': 'p1'}, chunks, Meter(), available_tokens=616000, max_tokens=384000)
    target = plan.directives[0]
    assert target.chunk_ids == ['old_chunk']
    assert target.runs[0].spans[0]['end'] == len(chunks[0]['text'])
    assert '章节标签仅作导航' in target.text
    assert '子标题' in target.text
    assert '不要从中提取陈述' not in target.text


def test_unlocated_bibliography_bookmark_does_not_make_citations_evidence():
    pages = [Page(1, '1 Introduction\n'+'INTRO '*30),
             Page(2, '2 Methods\n'+'METHOD '*30),
             Page(3, 'Unrecoverable heading\n'+'CITATION '*30)]
    sections = detect_sections(pages, [(1, '1 Introduction', 1), (1, '2 Methods', 2),
                                       (1, 'References', 3)])
    assert '边界未定位' in sections[-1].name and sections[-1].role == '参考文献'
    assert 'CITATION' in sections[-1].text
    assert not any('CITATION' in c['text'] for c in chunk_document(Document(sections=sections)))
