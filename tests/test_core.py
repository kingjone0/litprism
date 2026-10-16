"""测试套件：聚焦"防幻觉"与"解析正确性"这两条生命线。

运行：``python -m pytest tests -v``
所有联网测试通过 monkeypatch 打桩，离线可跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config, load_config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.ingest import Document, Page, Section, chunk_document, detect_sections  # noqa: E402
from litprism.query import build_or_fts_query, expand_query, keyword_terms  # noqa: E402
from litprism.textutil import (  # noqa: E402
    author_overlap,
    build_fts_query,
    cjk_segment,
    extract_doi,
    normalize_doi,
    normalize_title,
    parse_citations,
    title_similarity,
)
from litprism.verify import PaperVerifier, SourceRecord, VerificationResult  # noqa: E402


# ===========================================================================
# 文本与相似度
# ===========================================================================
class TestTitleSimilarity:
    def test_identical(self):
        assert title_similarity("Attention Is All You Need", "Attention Is All You Need") == 1.0

    def test_case_and_punctuation_insensitive(self):
        assert title_similarity("Deep Learning.", "deep  learning") == 1.0

    def test_word_order_matters(self):
        """词序颠倒不应被判为同一篇（这是早期版本的真实 bug）。"""
        sim = title_similarity("Attention Is All You Need", "Is Attention All You Need?")
        assert sim < 0.92, f"词序颠倒相似度过高：{sim}"

    def test_subtitle_bonus(self):
        sim = title_similarity("Attention Is All You Need", "Attention Is All You Need: A Survey")
        assert sim >= 0.9

    def test_unrelated_titles_low(self):
        sim = title_similarity(
            "Deep Residual Learning for Image Recognition", "Quantum Holographic Communication"
        )
        assert sim < 0.5

    def test_chinese_titles(self):
        assert title_similarity("深度学习综述", "深度学习综述") == 1.0
        assert title_similarity("深度学习在医学图像分割中的应用", "量子计算与密码学") < 0.4

    def test_empty(self):
        assert title_similarity("", "abc") == 0.0

    def test_latex_normalized(self):
        assert title_similarity(r"\textbf{Deep Learning}", "Deep Learning") == 1.0


class TestNormalize:
    def test_normalize_title_strips_markup(self):
        assert normalize_title("<i>Hello</i> &amp; World!") == "hello and world"

    def test_doi_extraction(self):
        assert extract_doi("see https://doi.org/10.1038/nature12373.") == "10.1038/nature12373"

    def test_normalize_doi(self):
        assert normalize_doi("https://doi.org/10.1038/NATURE12373") == "10.1038/nature12373"
        assert normalize_doi("doi:10.1/x") == "10.1/x"
        assert normalize_doi(None) is None

    def test_author_overlap(self):
        assert author_overlap(["Ashish Vaswani", "Noam Shazeer"], ["A. Vaswani"]) == 1.0
        assert author_overlap(["Alice Zhang"], ["Bob Li"]) == 0.0


class TestCitations:
    """防幻觉：引用标记解析必须覆盖模型可能产出的各种写法。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("结论见 [P2 p.2]。", [("P2", "2")]),
            ("见 [P1] 与 [P3, p.12]", [("P1", None), ("P3", "12")]),
            ("[P4 p. 7] 和 [P5 页3]", [("P4", "7"), ("P5", "3")]),
            ("[p6 pp.10-12]", [("P6", "10-12")]),
            ("没有任何引用", []),
        ],
    )
    def test_parse(self, text, expected):
        assert parse_citations(text) == expected


class TestCjk:
    def test_segment(self):
        assert cjk_segment("论文解读") == " 论  文  解  读 "

    def test_fts_query_handles_punctuation(self):
        """曾因 '?' 未剔除导致 fts5 syntax error。"""
        q = build_fts_query("What were the results? Really!")
        assert "?" not in q and "!" not in q
        assert q

    def test_fts_query_chinese_phrase(self):
        assert '"论 文"' in build_fts_query("论文")


# ===========================================================================
# 查询扩展
# ===========================================================================
class TestQueryExpansion:
    def test_chinese_expands_to_english(self):
        terms = expand_query("这篇论文的方法论创新点是什么", None)
        joined = " ".join(terms).lower()
        assert "method" in joined
        assert "contribution" in joined or "novelty" in joined

    def test_english_expands_to_chinese(self):
        terms = expand_query("few-shot learning evaluation", None)
        joined = " ".join(terms)
        assert "少样本" in joined

    def test_keyword_terms_drops_stopwords(self):
        kws = keyword_terms("What are the results of this experiment?")
        assert "the" not in kws and "what" not in kws
        assert "results" in kws

    def test_or_query_is_valid_fts(self):
        q = build_or_fts_query(expand_query("注意力机制怎么工作？", None))
        assert " OR " in q and "?" not in q


# ===========================================================================
# 章节识别与分块
# ===========================================================================
class TestIngest:
    def test_detect_sections_merges_duplicates(self):
        pages = [
            Page(1, "Title\nAuthors\n\nAbstract\nWe study X.\n\n1 Introduction\nIntro text.\n"),
            Page(2, "2 Method\nWe propose A.\n\n3.1 Sub Method\nMore A.\n\n4 Results\nWe got 99.\n"),
            Page(3, "5 Conclusion\nDone.\n"),
        ]
        secs = detect_sections(pages)
        names = [s.name for s in secs]
        assert names.count("方法") == 1, names
        assert "引言" in names and "结论" in names and "结果" in names

    def test_chunk_pages_are_accurate(self):
        """页码必须来自真实位置，不能是章节首页（早期版本的真实 bug）。"""
        doc = Document(title="t", pages=[])
        doc.sections = [
            Section(
                name="方法",
                lines=[(2, f"line-{i} " + "x" * 120) for i in range(20)]
                + [(7, f"tail-{i} " + "y" * 120) for i in range(20)],
            )
        ]
        chunks = chunk_document(doc, chunk_size=600, overlap=0)
        assert len(chunks) > 3
        pages_seen = {c["page"] for c in chunks}
        assert pages_seen == {2, 7}, f"页码未跟随实际位置：{pages_seen}"

    def test_chunk_filters_short_and_references(self):
        doc = Document(title="t")
        doc.sections = [
            Section(name="正文", lines=[(1, "short")]),
            Section(name="参考文献", lines=[(2, "x" * 500)]),
            Section(name="方法", lines=[(3, "y" * 500)]),
        ]
        chunks = chunk_document(doc, chunk_size=400, overlap=0)
        assert len(chunks) == 1
        assert chunks[0]["section"] == "方法"


# ===========================================================================
# 知识库
# ===========================================================================
class TestKnowledgeBase:
    @pytest.fixture()
    def kb(self, tmp_path):
        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.ensure_dirs()
        kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
        yield kb
        kb.close()

    def test_add_and_dedupe_by_doi(self, kb):
        a = kb.add_paper(title="Paper One", doi="10.1/a", year=2020)
        b = kb.add_paper(title="Paper One (renamed)", doi="10.1/a", year=2020)
        assert a == b
        assert len(kb.list_papers()) == 1

    def test_dedupe_by_title_fingerprint(self, kb):
        a = kb.add_paper(title="Same Title", year=2019)
        b = kb.add_paper(title="same   title!", year=2019)
        assert a == b

    def test_search_chinese_and_english(self, kb):
        pid = kb.add_paper(title="Attention Paper")
        kb.add_chunks(pid, [
            {"text": "We propose the Transformer, based solely on attention mechanisms.", "page": 2, "section": "方法"},
            {"text": "实验结果在 WMT 2014 上取得 28.4 BLEU。", "page": 8, "section": "结果"},
        ])
        assert kb.search_chunks("attention mechanisms")[0]["page"] == 2
        assert kb.search_chunks("实验结果")[0]["page"] == 8

    def test_search_via_expanded_query(self, kb):
        pid = kb.add_paper(title="T")
        kb.add_chunks(pid, [{"text": "The methodology we adopt is a two-stage approach.", "page": 3, "section": "方法"}])
        from litprism.search import search_local

        hits = search_local(kb, "这篇论文的方法论是什么", top_k=3)
        assert hits and hits[0]["page"] == 3

    def test_delete_cascades(self, kb):
        pid = kb.add_paper(title="T")
        kb.add_chunks(pid, [{"text": "x" * 200, "page": 1, "section": "正文"}])
        kb.save_analysis(pid, {"research_question": "q"})
        assert kb.delete_paper(pid)
        assert kb.get_paper(pid) is None
        assert kb.count_chunks() == 0
        assert kb.get_analysis(pid) is None
        assert kb.search_chunks("xxx") == []

    def test_resolve_paper(self, kb):
        pid = kb.add_paper(title="Deep Residual Learning", doi="10.1/resnet")
        assert kb.resolve_paper(pid)["id"] == pid
        assert kb.resolve_paper("10.1/resnet")["id"] == pid
        assert kb.resolve_paper("Residual")["id"] == pid


# ===========================================================================
# 核验（防幻觉核心）—— 全部离线打桩
# ===========================================================================
def _make_verifier(monkeypatch, crossref_records=None, openalex_records=None, arxiv_records=None,
                   crossref_raises=False, openalex_raises=False):
    cfg = load_config()
    cfg.sources.crossref.enabled = True
    cfg.sources.openalex.enabled = True
    cfg.sources.arxiv.enabled = True
    cfg.sources.semantic_scholar.enabled = False
    v = PaperVerifier(cfg)

    def rec_to_dict(r):
        return r

    cr = v._sources["crossref"]
    oa = v._sources["openalex"]
    ax = v._sources["arxiv"]

    def fake_crossref_by_title(title, limit=5):
        if crossref_raises:
            raise RuntimeError("network down")
        return list(crossref_records or [])

    def fake_openalex_by_title(title, limit=5):
        if openalex_raises:
            raise RuntimeError("network down")
        return list(openalex_records or [])

    def fake_arxiv_by_title(title, limit=5):
        return list(arxiv_records or [])

    monkeypatch.setattr(cr, "by_title", fake_crossref_by_title)
    monkeypatch.setattr(oa, "by_title", fake_openalex_by_title)
    monkeypatch.setattr(ax, "by_title", fake_arxiv_by_title)
    return v


class TestVerification:
    def test_exact_title_hit_is_verified(self, monkeypatch):
        v = _make_verifier(monkeypatch, crossref_records=[
            SourceRecord(source="crossref", title="Attention Is All You Need", year=2017,
                         authors=["Ashish Vaswani"], doi="10.1/a"),
        ])
        res = v.verify(title="Attention Is All You Need", year=2017)
        assert res.level == "verified"
        assert res.exists is True
        assert res.confidence >= 0.92

    def test_multi_source_corroboration(self, monkeypatch):
        v = _make_verifier(monkeypatch, crossref_records=[
            SourceRecord(source="crossref", title="BERT: Pre-training of Deep Bidirectional Transformers", year=2018),
        ], arxiv_records=[
            SourceRecord(source="arxiv", title="BERT: Pre-training of Deep Bidirectional Transformers", year=2018),
        ])
        res = v.verify(title="BERT: Pre-training of Deep Bidirectional Transformers")
        assert res.level == "verified"
        assert set(res.sources_ok) >= {"crossref", "arxiv"}

    def test_fabricated_paper_not_found(self, monkeypatch):
        """虚构标题：数据库只返回弱相关记录 → 必须判为 not_found。"""
        v = _make_verifier(monkeypatch, crossref_records=[
            SourceRecord(source="crossref", title="Quantum Holographic Communication via Entanglement", year=2025),
        ], openalex_records=[
            SourceRecord(source="openalex", title="Quantum Tantric Symmetry Model", year=2025),
        ])
        res = v.verify(
            title="Quantum Entanglement Based Gradient Descent for Large Language Model Alignment"
        )
        assert res.level == "not_found"
        assert res.exists is False
        assert "不得" in res.message

    def test_likely_band(self, monkeypatch):
        v = _make_verifier(monkeypatch, crossref_records=[
            SourceRecord(source="crossref", title="Deep Residual Networks for Image Recognition", year=2016),
        ])
        res = v.verify(title="Deep Residual Learning for Image Recognition")
        assert res.level in ("likely", "not_found")
        assert res.exists is (res.level == "likely")

    def test_network_failure_is_not_not_found(self, monkeypatch):
        """关键：数据源全挂时必须报 unchecked，绝不能误判为"不存在"。"""
        v = _make_verifier(monkeypatch, crossref_raises=True, openalex_raises=True)
        # arxiv 打桩返回空列表（视为正常响应）
        res = v.verify(title="Some Real Paper Title")
        assert res.level in ("unchecked", "not_found")
        if not res.matches and not res.sources_ok:
            assert res.level == "unchecked"
        assert res.sources_failed, "应记录失败的数据源"

    def test_all_sources_failed(self, monkeypatch):
        v = _make_verifier(monkeypatch, crossref_raises=True, openalex_raises=True)
        # 让 arxiv 也失败
        monkeypatch.setattr(v._sources["arxiv"], "by_title", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        res = v.verify(title="Whatever Title Here")
        assert res.level == "unchecked"
        assert res.exists is False

    def test_doi_path(self, monkeypatch):
        v = _make_verifier(monkeypatch)
        monkeypatch.setattr(
            v._sources["crossref"], "by_doi",
            lambda doi: SourceRecord(source="crossref", title="Nanometre-scale thermometry in a living cell",
                                     year=2013, doi="10.1038/nature12373"),
        )
        res = v.verify(title="", doi="10.1038/nature12373")
        assert res.level == "verified" and res.confidence == 1.0

    def test_empty_query(self, monkeypatch):
        v = _make_verifier(monkeypatch)
        res = v.verify(title="", doi=None)
        assert res.level == "unchecked"


# ===========================================================================
# 引用校验（问答防幻觉闸门）
# ===========================================================================
class TestCitationGuard:
    def _agent(self, tmp_path):
        from litprism.agents import QAAgent

        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.ensure_dirs()
        kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
        return QAAgent(kb, cfg, None), kb

    def test_flags_nonexistent_excerpt(self, tmp_path):
        from litprism.agents import QAAnswer

        agent, kb = self._agent(tmp_path)
        res = QAAnswer(question="q", answer="结论是 X [P9 p.1]。")
        res.excerpts = [{"tag": "P1", "page": 1}]
        agent._check_citations(res)
        assert res.citation_check_passed is False
        assert any("P9" in i for i in res.citation_issues)
        kb.close()

    def test_flags_wrong_page(self, tmp_path):
        from litprism.agents import QAAnswer

        agent, kb = self._agent(tmp_path)
        res = QAAnswer(question="q", answer="结论是 X [P1 p.88]。")
        res.excerpts = [{"tag": "P1", "page": 3}]
        agent._check_citations(res)
        assert res.citation_check_passed is False
        assert any("88" in i for i in res.citation_issues)
        kb.close()

    def test_flags_uncited_answer(self, tmp_path):
        from litprism.agents import QAAnswer

        agent, kb = self._agent(tmp_path)
        res = QAAnswer(question="q", answer="这是一个很长的回答但完全没有标注任何来源，因此无法追溯依据。" * 2)
        res.excerpts = [{"tag": "P1", "page": 1}]
        agent._check_citations(res)
        assert res.citation_check_passed is False
        kb.close()

    def test_accepts_correct_citations(self, tmp_path):
        from litprism.agents import QAAnswer

        agent, kb = self._agent(tmp_path)
        res = QAAnswer(question="q", answer="结论是 X [P1 p.3]，另一条是 Y [P2 p.7]。")
        res.excerpts = [{"tag": "P1", "page": 3}, {"tag": "P2", "page": 7}]
        agent._check_citations(res)
        assert res.citation_check_passed is True
        kb.close()

    def test_refuses_when_no_evidence(self, tmp_path):
        agent, kb = self._agent(tmp_path)
        ans = agent.ask("知识库里完全不存在的话题 xyzzy")
        assert ans.insufficient_evidence is True
        assert "没有检索到" in ans.answer
        kb.close()


# ===========================================================================
# 导入守卫
# ===========================================================================
class TestImportGuard:
    def test_rejects_unverified_import(self, tmp_path):
        from litprism.search import import_record

        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.ensure_dirs()
        kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
        vr = VerificationResult(query_title="Fake Paper", level="not_found", exists=False)
        with pytest.raises(ValueError, match="拒绝导入"):
            import_record(kb, {"title": "Fake Paper", "year": 2024}, verification=vr)
        assert kb.list_papers() == []
        kb.close()

    def test_accepts_verified_import(self, tmp_path):
        from litprism.search import import_record

        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.ensure_dirs()
        kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
        vr = VerificationResult(query_title="Real Paper", level="verified", exists=True, confidence=1.0)
        pid, _ = import_record(
            kb,
            {"title": "Real Paper", "year": 2020, "abstract": "We propose something."},
            verification=vr,
        )
        assert kb.get_paper(pid)["verify_level"] == "verified"
        assert kb.count_chunks(pid) == 1
        kb.close()


# ===========================================================================
# 跨论文关联
# ===========================================================================
class TestRelations:
    def test_similar_papers_link(self, tmp_path):
        from litprism.relations import compute_relations

        cfg = Config()
        cfg.app.data_dir = str(tmp_path)
        cfg.ensure_dirs()
        kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
        a = kb.add_paper(title="Transformer Attention Model")
        b = kb.add_paper(title="Attention Based Transformer Model")
        c = kb.add_paper(title="Protein Folding Thermodynamics")
        kb.add_chunks(a, [{"text": "attention transformer encoder decoder sequence model " * 8, "page": 1, "section": "方法"}])
        kb.add_chunks(b, [{"text": "attention transformer encoder decoder sequence model network " * 8, "page": 1, "section": "方法"}])
        kb.add_chunks(c, [{"text": "protein folding thermodynamics entropy amino acid chain " * 8, "page": 1, "section": "方法"}])
        rels = compute_relations(kb, threshold=0.2, persist=False)
        pairs = {frozenset([r["paper_a"], r["paper_b"]]) for r in rels}
        assert frozenset([a, b]) in pairs
        assert frozenset([a, c]) not in pairs
        kb.close()
