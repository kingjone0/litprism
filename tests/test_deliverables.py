"""交付物管理测试。

重点：
1. **没有引用清单的交付物是不可追溯的** —— 导出必须带出来源；
2. 导出后状态流转（draft → exported），但定稿不被降级；
3. 类型与状态取值受约束，非法值直接拒绝。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.deliverables import (  # noqa: E402
    DELIVERABLE_TYPES,
    STATUS_LABELS,
    STATUSES,
    DeliverableStore,
    _safe_filename,
    format_deliverables,
)


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    yield cfg, kb
    kb.close()


CITES = [
    {"tag": "P1", "paper_id": "p_1", "paper_title": "Attention Is All You Need", "page": 3, "chunk_id": "c_a"},
    {"tag": "P2", "paper_id": "p_2", "paper_title": "BERT", "page": 7, "chunk_id": "c_b"},
]


# ===========================================================================
# 增删改查
# ===========================================================================
class TestCRUD:
    def test_add_and_get(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="review", title="注意力机制综述", content="正文", citations=CITES)
        d = s.get(did)
        assert d is not None
        assert d.type_label == "文献综述"
        assert d.status_label == "草稿"
        assert d.citation_count == 2
        assert d.word_count == 2

    def test_rejects_unknown_type(self, env):
        cfg, kb = env
        with pytest.raises(ValueError, match="未知交付物类型"):
            DeliverableStore(kb).add(type="novel", title="x", content="y")

    def test_rejects_unknown_status(self, env):
        cfg, kb = env
        with pytest.raises(ValueError, match="未知状态"):
            DeliverableStore(kb).add(type="notes", title="x", content="y", status="weird")

    def test_rejects_empty_title(self, env):
        cfg, kb = env
        with pytest.raises(ValueError, match="标题不能为空"):
            DeliverableStore(kb).add(type="notes", title="  ", content="y")

    def test_list_filters(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        s.add(type="review", title="综述一", content="a")
        s.add(type="comparison", title="对比一", content="b", status="final")
        assert len(s.list()) == 2
        assert len(s.list(type="review")) == 1
        assert len(s.list(status="final")) == 1

    def test_resolve_by_prefix_and_title(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="review", title="稀疏注意力综述", content="a")
        assert s.resolve(did).id == did
        assert s.resolve(did[:8]).id == did
        assert s.resolve("稀疏注意力").id == did

    def test_resolve_empty_returns_latest(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        s.add(type="notes", title="旧", content="a")
        new_id = s.add(type="notes", title="新", content="b")
        assert s.resolve("").id == new_id

    def test_update(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="初稿")
        assert s.update(did, content="改过", status="final")
        d = s.get(did)
        assert d.content == "改过" and d.status == "final"

    def test_update_rejects_bad_status(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="x")
        with pytest.raises(ValueError):
            s.update(did, status="nope")

    def test_soft_delete_and_restore(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="x")
        assert s.delete(did)
        assert s.get(did) is None
        assert s.list() == []
        assert s.restore(did)
        assert s.get(did) is not None

    def test_stats(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        s.add(type="review", title="a", content="x", citations=CITES)
        s.add(type="review", title="b", content="y", status="final")
        st = s.stats()
        assert st["total"] == 2
        assert st["by_type"]["review"] == 2
        assert st["by_status"]["draft"] == 1


# ===========================================================================
# 导出
# ===========================================================================
class TestExport:
    def test_markdown_has_frontmatter_and_citations(self, env, tmp_path):
        """⭐ 导出必须带出来源 —— 没有引用清单的交付物是不可追溯的。"""
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="review", title="综述", content="正文内容", citations=CITES)
        p = s.export(did, tmp_path / "out.md")
        text = p.read_text(encoding="utf-8")

        assert text.startswith("---")
        assert "type: review" in text
        assert "citations: 2" in text
        assert "# 综述" in text
        assert "正文内容" in text
        assert "## 引用来源" in text
        assert "Attention Is All You Need" in text
        assert "p.3" in text
        assert "chunk:c_a" in text

    def test_no_citations_still_exports(self, env, tmp_path):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="x")
        text = s.export(did, tmp_path / "n.md").read_text(encoding="utf-8")
        assert "## 引用来源" not in text
        assert "citations: 0" in text

    def test_json_export(self, env, tmp_path):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="comparison", title="对比", content="x", citations=CITES)
        p = s.export(did, tmp_path / "c.json", fmt="json")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["type"] == "comparison"
        assert data["citation_count"] == 2
        assert data["content"] == "x"

    def test_export_promotes_draft_to_exported(self, env, tmp_path):
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="x")
        s.export(did, tmp_path / "a.md")
        assert s.get(did).status == "exported"

    def test_export_does_not_downgrade_final(self, env, tmp_path):
        """定稿状态更「重」，导出不该把它降级。"""
        cfg, kb = env
        s = DeliverableStore(kb)
        did = s.add(type="notes", title="笔记", content="x", status="final")
        s.export(did, tmp_path / "a.md")
        assert s.get(did).status == "final"

    def test_export_unknown_raises(self, env, tmp_path):
        cfg, kb = env
        with pytest.raises(ValueError, match="不存在"):
            DeliverableStore(kb).export("del_nope", tmp_path / "x.md")

    def test_export_all(self, env, tmp_path):
        cfg, kb = env
        s = DeliverableStore(kb)
        s.add(type="review", title="综述 A", content="x")
        s.add(type="notes", title="笔记 B", content="y")
        paths = s.export_all(tmp_path / "out")
        assert len(paths) == 2
        assert all(p.exists() for p in paths)

    def test_safe_filename(self):
        assert "/" not in _safe_filename("a/b:c*d")
        assert _safe_filename("") == "untitled"
        assert len(_safe_filename("很长的标题" * 100)) <= 80


class TestFormat:
    def test_format_list(self, env):
        cfg, kb = env
        s = DeliverableStore(kb)
        s.add(type="review", title="综述", content="x", citations=CITES)
        text = format_deliverables(s.list())
        assert "文献综述" in text and "2 条引用" in text
        assert format_deliverables([]) == "（暂无交付物）"

    def test_type_and_status_vocab(self):
        assert set(DELIVERABLE_TYPES) == {"review", "comparison", "notes", "peer_review"}
        assert set(STATUSES) == {"draft", "final", "exported"}
        assert set(STATUS_LABELS) == set(STATUSES)
