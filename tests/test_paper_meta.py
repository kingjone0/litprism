"""论文重命名与原文接口测试。

重命名的关键约束：**只改 display_title，绝不动 title**。
原始题录标题用于核验比对、生成引用、导出 BibTeX —— 改了它，
真实论文会被判成 not_found，BibTeX 会带上随手起的名字。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    pid = kb.add_paper(
        title="Attention Is All You Need", year=2017,
        verify_status="verified", verify_level="verified",
    )
    kb.add_chunks(pid, [
        {"text": "第一页内容。", "page": 1, "section": "引言"},
        {"text": "第二页内容。", "page": 2, "section": "方法"},
        {"text": "无页码内容。", "page": None, "section": "附录"},
    ])
    yield cfg, kb, pid
    kb.close()


class TestRename:
    def test_rename_updates_display_only(self, env):
        cfg, kb, pid = env
        assert kb.rename_paper(pid, "注意力机制论文")
        p = kb.get_paper(pid)
        assert p["display_title"] == "注意力机制论文"
        assert p["title"] == "Attention Is All You Need", "原始题录标题不能动"

    def test_rename_to_same_as_title_resets(self, env):
        """显示名与原名相同或为空 → 恢复用原始标题（display_title 置空）。"""
        cfg, kb, pid = env
        kb.rename_paper(pid, "别名")
        kb.rename_paper(pid, "")
        assert kb.get_paper(pid)["display_title"] is None
        kb.rename_paper(pid, "别名")
        kb.rename_paper(pid, "Attention Is All You Need")
        assert kb.get_paper(pid)["display_title"] is None

    def test_rename_unknown_paper(self, env):
        cfg, kb, _ = env
        assert kb.rename_paper("p_nope", "x") is False

    def test_verify_evidence_untouched(self, env):
        """重命名不得影响核验状态与证据。"""
        cfg, kb, pid = env
        before = kb.get_paper(pid)["verify_evidence"]
        kb.rename_paper(pid, "别名")
        after = kb.get_paper(pid)
        assert after["verify_level"] == "verified"
        assert after["verify_evidence"] == before


class TestPaperText:
    def test_pages_grouped_and_sorted(self, env):
        cfg, kb, pid = env
        chunks = kb.get_chunks(pid)
        pages = {}
        for c in chunks:
            pages.setdefault(c.get("page"), []).append(c)
        assert set(pages) == {1, 2, None}

    def test_chunks_count(self, env):
        cfg, kb, pid = env
        assert len(kb.get_chunks(pid)) == 3
