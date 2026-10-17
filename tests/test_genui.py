"""生成式 UI 后端测试。

核心断言：**框架注入的字段一律覆盖模型输入。**
模型可以声称「已核验」、可以编造页码，但渲染出来的必须是数据库的真实值。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.genui import (  # noqa: E402
    COMPONENTS,
    MAX_NODES,
    enrich,
    parse_fences,
    process,
    render_fallback,
    sanitize_text,
    validate_component,
)


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")

    pid = kb.add_paper(
        title="Attention Is All You Need", year=2017,
        authors=["Ashish Vaswani", "Noam Shazeer"],
        verify_status="verified", verify_level="verified",
    )
    kb.add_chunks(pid, [{"text": "注意力机制的核心是缩放点积注意力。", "page": 5, "section": "方法"}])
    chunk = kb._con.execute("SELECT id FROM chunks WHERE paper_id = ?", (pid,)).fetchone()["id"]

    kb.save_analysis(pid, {'methodology': '数据库方法 [P1 p.5]', '_citation_check': {'passed': True}})

    pid2 = kb.add_paper(
        title="Fabricated Paper", year=2024,
        verify_status="not_found", verify_level="not_found",
    )

    yield cfg, kb, pid, pid2, chunk
    kb.close()


def fence(obj: dict) -> str:
    return "```lit-ui\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


# ===========================================================================
# 解析
# ===========================================================================
class TestParse:
    def test_single_fence(self, env):
        _, _, pid, _, _ = env
        text = "前言\n" + fence({"type": "paper-card", "paper_id": pid}) + "\n后记"
        fs = parse_fences(text)
        assert len(fs) == 1
        assert fs[0].ok and fs[0].component["type"] == "paper-card"

    def test_multiple_fences(self, env):
        _, _, pid, _, _ = env
        text = fence({"type": "paper-card", "paper_id": pid}) + "\n" + fence({"type": "callout", "text": "x"})
        assert len(parse_fences(text)) == 2

    def test_broken_json_marks_error_not_raises(self):
        fs = parse_fences("```lit-ui\n{not json}\n```")
        assert len(fs) == 1 and not fs[0].ok
        assert "JSON 解析失败" in fs[0].error

    def test_empty_fence(self):
        fs = parse_fences("```lit-ui\n\n```")
        assert len(fs) == 1 and not fs[0].ok

    def test_non_object_fence(self):
        fs = parse_fences("```lit-ui\n[1,2,3]\n```")
        assert len(fs) == 1 and not fs[0].ok
        assert "JSON 对象" in fs[0].error

    def test_no_fence(self):
        assert parse_fences("普通文本，没有围栏") == []


# ===========================================================================
# 校验
# ===========================================================================
class TestValidate:
    def test_valid(self):
        assert validate_component({"type": "callout", "text": "x"}) == []

    def test_unknown_type(self):
        issues = validate_component({"type": "iframe"})
        assert issues and issues[0].code == "type.unknown"
        assert "白名单" in issues[0].message

    def test_missing_type(self):
        assert validate_component({})[0].code == "type.missing"

    def test_missing_required(self):
        issues = validate_component({"type": "paper-card"})
        assert any(i.code == "field.missing" and "paper_id" in i.message for i in issues)

    def test_node_limit_warns(self):
        big = {"type": "concept-list", "items": [{"term": f"t{i}", "definition": "d"} for i in range(MAX_NODES)]}
        assert any(i.code == "limit.nodes" for i in validate_component(big))

    def test_bad_callout_kind_warns(self):
        issues = validate_component({"type": "callout", "text": "x", "kind": "shout"})
        assert any(i.code == "callout.kind" and i.severity == "warning" for i in issues)

    def test_component_whitelist(self):
        assert set(COMPONENTS) == {
            "paper-card", "paper-list", "analysis", "comparison-table",
            "concept-list", "citation-list", "verify-result", "callout",
        }


# ===========================================================================
# ⭐ 框架注入（核心）
# ===========================================================================
class TestInjection:
    def test_paper_card_overrides_model_claims(self, env):
        """⭐ 模型声称「已核验」也不行 —— 徽章以数据库为准。"""
        _, kb, pid, _, _ = env
        comp = {
            "type": "paper-card", "paper_id": pid,
            "title": "模型伪造的标题", "verify_level": "verified", "citable": True,
        }
        out = enrich(comp, kb)
        assert out["title"] == "Attention Is All You Need"
        assert out["verify_level"] == "verified"
        assert out["year"] == 2017

    def test_paper_card_blocks_fake_verification(self, env):
        """⭐ 模型把 not_found 的论文标成 verified，注入后必须还原为 not_found。"""
        _, kb, _, pid2, _ = env
        comp = {"type": "paper-card", "paper_id": pid2, "verify_level": "verified", "title": "假标题"}
        out = enrich(comp, kb)
        assert out["verify_level"] == "not_found"
        assert out["citable"] is False
        assert "❌" in out["verify_badge"]

    def test_paper_card_missing_paper(self, env):
        _, kb, _, _, _ = env
        out = enrich({"type": "paper-card", "paper_id": "p_nope"}, kb)
        assert out["_missing"] is True

    def test_citation_page_injected_not_model_value(self, env):
        """⭐ 页码由 chunk_id 查出，模型填什么都不算数。"""
        _, kb, _, _, chunk = env
        out = enrich({"type": "citation-list", "chunk_ids": [chunk], "page": 999}, kb)
        item = out["items"][0]
        assert item["page"] == 5, "必须用数据库里的页码"
        assert item["section"] == "方法"
        assert "缩放点积" in item["text"]
        assert item["paper_title"] == "Attention Is All You Need"

    def test_citation_missing_chunk(self, env):
        _, kb, _, _, _ = env
        out = enrich({"type": "citation-list", "chunk_ids": ["c_nope"]}, kb)
        assert out["items"][0]["_missing"] is True

    def test_analysis_injects_title_and_badge(self, env):
        _, kb, pid, _, _ = env
        out = enrich(
            {"type": "analysis", "paper_id": pid, "title": "假标题",
             "fields": [{"key": "methodology", "text": "Transformer"}]},
            kb,
        )
        assert out["title"] == "Attention Is All You Need"
        assert out["fields"][0]["text"] == "数据库方法 [P1 p.5]"

    def test_paper_list_enriches_each(self, env):
        _, kb, pid, pid2, _ = env
        out = enrich({"type": "paper-list", "paper_ids": [pid, pid2, "p_nope"]}, kb)
        assert out["items"][0]["title"] == "Attention Is All You Need"
        assert out["items"][1]["verify_level"] == "not_found"
        assert out["items"][2]["_missing"] is True

    def test_comparison_injects_paper_titles(self, env):
        _, kb, pid, pid2, _ = env
        out = enrich(
            {"type": "comparison-table", "paper_ids": [pid, pid2],
             "dimensions": [{"key": "methodology", "label": "方法", "values": ["Transformer", "未提及"]}]},
            kb,
        )
        assert out["papers"][0]["title"] == "Attention Is All You Need"
        assert out["dimensions"][0]["values"] == ["数据库方法 [P1 p.5]", "未提及"]

    def test_verify_result_from_db(self, env):
        _, kb, pid, _, _ = env
        out = enrich({"type": "verify-result", "paper_id": pid}, kb)
        assert out["level"] == "verified"
        assert "✅" in out["badge"]

    def test_verify_result_unknown_paper(self, env):
        _, kb, _, _, _ = env
        out = enrich({"type": "verify-result", "paper_id": "p_nope"}, kb)
        assert out["level"] == "unchecked"

    def test_callout_sanitized(self, env):
        _, kb, _, _, _ = env
        out = enrich({"type": "callout", "text": "很长" * 3000, "kind": "bogus"}, kb)
        assert len(out["text"]) <= 4001
        assert out["kind"] == "note"

    def test_concept_list_truncates(self, env):
        _, kb, _, _, _ = env
        out = enrich({"type": "concept-list", "items": [{"term": "t", "definition": "d" * 2000}]}, kb)
        assert len(out["items"][0]["definition"]) <= 801


class TestSanitize:
    def test_truncates(self):
        assert len(sanitize_text("x" * 5000)) == 4001

    def test_none_becomes_empty(self):
        assert sanitize_text(None) == ""


# ===========================================================================
# 主流程
# ===========================================================================
class TestProcess:
    def test_text_only(self):
        p = process("纯文本回答", None)
        assert len(p.segments) == 1 and p.segments[0]["kind"] == "text"
        assert not p.has_component

    def test_empty(self):
        p = process("", None)
        assert p.segments == []

    def test_mixed_segments(self, env):
        _, kb, pid, _, _ = env
        text = "前置说明\n" + fence({"type": "paper-card", "paper_id": pid}) + "\n后置说明"
        p = process(text, kb)
        kinds = [s["kind"] for s in p.segments]
        assert kinds == ["text", "component", "text"]
        assert p.has_component and not p.degraded

    def test_broken_fence_degrades_to_text(self):
        """⭐ 渲染失败也要让内容可读 —— 绝不出现空白。"""
        p = process("说明\n```lit-ui\n{broken\n```\n结尾", None)
        assert p.degraded
        assert p.issues
        joined = "".join(s.get("text", "") for s in p.segments)
        assert "未能渲染" in joined
        assert joined.strip(), "降级后不能是空白"

    def test_unknown_component_degrades(self):
        p = process(fence({"type": "iframe", "src": "x"}), None)
        assert p.degraded
        assert any("iframe" in i for i in p.issues)

    def test_missing_required_degrades(self, env):
        _, kb, _, _, _ = env
        p = process(fence({"type": "paper-card"}), kb)
        assert p.degraded

    def test_component_enriched_in_process(self, env):
        _, kb, pid, _, _ = env
        p = process(fence({"type": "paper-card", "paper_id": pid, "title": "假"}), kb)
        comp = p.segments[0]["component"]
        assert comp["title"] == "Attention Is All You Need"

    def test_surrounding_text_preserved(self, env):
        _, kb, pid, _, _ = env
        text = "下面是论文：\n" + fence({"type": "paper-card", "paper_id": pid}) + "\n以上。"
        p = process(text, kb)
        texts = [s["text"] for s in p.segments if s["kind"] == "text"]
        assert any("下面是论文" in t for t in texts)
        assert any("以上" in t for t in texts)


class TestFallback:
    @pytest.mark.parametrize("ctype,comp", [
        ("paper-card", {"type": "paper-card", "title": "T", "year": 2020}),
        ("paper-list", {"type": "paper-list", "items": [{"title": "T", "year": 2020}]}),
        ("analysis", {"type": "analysis", "title": "T", "fields": [{"key": "方法", "text": "X"}]}),
        ("comparison-table", {"type": "comparison-table", "dimensions": [{"label": "方法", "values": ["A", "B"]}]}),
        ("concept-list", {"type": "concept-list", "items": [{"term": "t", "definition": "d"}]}),
        ("citation-list", {"type": "citation-list", "items": [{"paper_title": "T", "page": 1, "text": "x"}]}),
        ("verify-result", {"type": "verify-result", "badge": "✅", "message": "ok"}),
        ("callout", {"type": "callout", "kind": "fact", "text": "x"}),
    ])
    def test_fallback_non_empty(self, ctype, comp):
        assert render_fallback(comp).strip(), f"{ctype} 降级后不能为空"

    def test_unknown_type_fallback_empty_string(self):
        assert render_fallback({"type": "nope"}) == ""
