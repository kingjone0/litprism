"""记忆系统测试。

重点验证三件事：
1. **一期只做显式记忆** —— 隐式写入必须被拒；
2. **常驻层有硬上限** —— 超预算按 priority 裁减，不能无界增长；
3. **硬边界可执行** —— 记忆不得当事实、不得作引用来源、不得进事实性内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config, MemoryConfig  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.memory.guards import (  # noqa: E402
    MEMORY_BLOCK_CLOSE,
    MEMORY_BLOCK_OPEN,
    MEMORY_GUARD_TEXT,
    assert_memory_preference_only,
    detect_memory_as_fact,
    render_memory_block,
    validate_citations,
)
from litprism.memory.store import KIND_LABELS, MemoryStore, format_memories  # noqa: E402
from litprism.memory.strategy import LayeredStrategy  # noqa: E402
from litprism.memory.view import TurnViewStore, format_views  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.memory = MemoryConfig(resident_token_budget=200, resident_max_items=5, retrieval_top_k=3)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    yield cfg, kb
    kb.close()


# ===========================================================================
# 存储
# ===========================================================================
class TestMemoryStore:
    def test_save_and_get(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        r = s.save("我在做多模态文档理解", kind="research_interest", pinned=True)
        assert r.action == "created"
        m = s.get(r.memory_id)
        assert m is not None and m.kind == "research_interest"
        assert m.label == "研究方向"

    def test_rejects_empty(self, env):
        cfg, kb = env
        r = MemoryStore(kb, cfg).save("   ")
        assert r.action == "rejected"

    def test_rejects_fact_like_kind(self, env):
        """事实性内容不能进记忆层 —— 否则绕过核验流程。"""
        cfg, kb = env
        r = MemoryStore(kb, cfg).save("Attention 是 2017 年的论文", kind="fact")
        assert r.action == "rejected"
        assert "偏好类" in r.reason

    def test_explicit_only_in_phase_one(self, env):
        """一期不做隐式推断。"""
        cfg, kb = env
        r = MemoryStore(kb, cfg).save("系统猜的偏好", kind="misc", source="derived")
        assert r.action == "rejected"
        assert "显式" in r.reason

    def test_identical_save_updates_not_duplicates(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        a = s.save("解读要简短", kind="style_preference")
        b = s.save("解读要简短", kind="style_preference")
        assert a.action == "created"
        assert b.action == "updated"
        assert b.memory_id == a.memory_id
        assert len(s.list()) == 1

    def test_similar_save_merges(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        a = s.save("transformer 统一译作变换器", kind="term_preference", key="transformer")
        b = s.save("transformer 统一译作变换器模型", kind="term_preference", key="transformer")
        assert b.action in ("merged", "updated")
        assert len(s.list()) == 1

    def test_distill_applied(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        r = s.save("嗯那个就是我希望你以后回答问题简短一点", kind="style_preference",
                   distill=lambda t: "回答要简短")
        m = s.get(r.memory_id)
        assert m.value == "回答要简短"

    def test_distill_failure_does_not_block(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)

        def boom(_):
            raise RuntimeError("模型挂了")

        r = s.save("原始文本", kind="misc", distill=boom)
        assert r.action == "created"
        assert s.get(r.memory_id).value == "原始文本"

    def test_soft_delete_and_restore(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        r = s.save("临时偏好", kind="misc")
        assert s.delete(r.memory_id)
        assert s.get(r.memory_id) is None
        assert s.list() == []
        assert s.list(include_deleted=True)
        assert s.restore(r.memory_id)
        assert s.get(r.memory_id) is not None

    def test_update_and_kind_guard(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        r = s.save("x", kind="misc")
        assert s.update(r.memory_id, value="y", pinned=False)
        assert s.get(r.memory_id).value == "y"
        with pytest.raises(ValueError):
            s.update(r.memory_id, kind="fact")

    def test_search(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("我在做多模态文档理解", kind="research_interest")
        s.save("回答要简短", kind="style_preference")
        hits = s.search("多模态")
        assert hits and "多模态" in hits[0].value

    def test_resident_only_pinned(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("常驻的", kind="misc", pinned=True)
        s.save("不常驻的", kind="misc", pinned=False)
        assert [m.value for m in s.resident()] == ["常驻的"]

    def test_stats(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("a", kind="misc")
        s.save("b", kind="term_preference")
        st = s.stats()
        assert st["total"] == 2 and st["pinned"] == 2
        assert st["by_kind"]["misc"] == 1

    def test_format(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("x", kind="misc")
        assert "★" in format_memories(s.list())
        assert format_memories([]) == "（暂无记忆）"

    def test_kind_labels_cover_all(self):
        from litprism.memory.store import MEMORY_KINDS

        assert set(KIND_LABELS) == set(MEMORY_KINDS)


# ===========================================================================
# 硬边界
# ===========================================================================
class TestGuards:
    def test_render_block_always_includes_constraint(self):
        """调用方不得自行拼接记忆文本 —— 必须走 render_memory_block。"""
        text = render_memory_block([{"kind": "misc", "value": "回答要简短"}])
        assert MEMORY_GUARD_TEXT in text
        assert MEMORY_BLOCK_OPEN in text and MEMORY_BLOCK_CLOSE in text
        assert "回答要简短" in text

    def test_render_empty_returns_empty(self):
        assert render_memory_block([]) == ""
        assert render_memory_block([{"value": "  "}]) == ""

    def test_constraint_forbids_three_things(self):
        assert "不得作为事实依据" in MEMORY_GUARD_TEXT
        assert "不得作为引用来源" in MEMORY_GUARD_TEXT
        assert "不得改变任何核验结论" in MEMORY_GUARD_TEXT

    @pytest.mark.parametrize(
        "text",
        [
            "根据我的记忆，这篇论文提出了 Transformer。",
            "你之前说过你在做多模态，所以这篇很相关。",
            "Based on my memory, this paper is relevant.",
        ],
    )
    def test_detect_memory_as_fact(self, text):
        r = detect_memory_as_fact(text)
        assert not r.ok
        assert any(v.code == "mem.as_fact" for v in r.violations)

    def test_clean_text_passes(self):
        r = detect_memory_as_fact("论文报告了 28.4 BLEU 的结果 [P1 p.8]。")
        assert r.ok

    def test_reject_memory_as_citation(self):
        r = validate_citations("按偏好 [mem_abc123] 来看，应该这样。")
        assert not r.ok
        assert any(v.code == "mem.as_citation" for v in r.violations)

    def test_reject_memory_id_in_citation_tags(self):
        r = validate_citations("结论见 [MEM_ABC]。", memory_ids={"MEM_ABC"})
        assert not r.ok

    def test_normal_citations_pass(self):
        r = validate_citations("结论见 [P1 p.3]。", valid_excerpt_tags={"P1"})
        assert r.ok

    def test_preference_only_guard(self):
        assert_memory_preference_only("style_preference")
        for bad in ("fact", "evidence", "citation"):
            with pytest.raises(ValueError):
                assert_memory_preference_only(bad)


# ===========================================================================
# 分层驻留
# ===========================================================================
class TestLayeredStrategy:
    def test_resident_respects_token_budget(self, env):
        """常驻层必须有硬上限，否则会持续挤压检索片段的预算。

        注意内容要各不相同 —— 相同内容会被去重合并（那是另一个测试的事）。
        """
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        for i in range(8):
            s.save(f"第{i}条很长的偏好内容" * 20, kind="misc", priority=i)
        assert len(s.list()) == 8, "内容各异，不应被去重"
        bundle = LayeredStrategy(cfg).assemble(s)
        assert bundle.resident_tokens <= cfg.memory.resident_token_budget
        assert bundle.truncated, "超预算时应记录被裁减的项"
        assert any("硬上限" in n for n in bundle.notes)

    def test_dedup_reduces_resident_pressure(self, env):
        """去重本身就是常驻层的第一道防线。"""
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        for _ in range(8):
            s.save("完全相同的偏好", kind="misc")
        assert len(s.list()) == 1
        bundle = LayeredStrategy(cfg).assemble(s)
        assert len(bundle.resident) == 1 and not bundle.truncated

    def test_resident_respects_item_cap(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        for i in range(10):
            s.save(f"短偏好{i}", kind="misc", priority=i)
        bundle = LayeredStrategy(cfg).assemble(s)
        assert len(bundle.resident) <= cfg.memory.resident_max_items

    def test_priority_ordering(self, env):
        """priority 小者先保。"""
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("低优先级" * 30, kind="misc", priority=900)
        s.save("高优先级", kind="misc", priority=1)
        bundle = LayeredStrategy(cfg).assemble(s)
        assert any("高优先级" in m.value for m in bundle.resident)

    def test_prompt_block_contains_guard(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("回答要简短", kind="style_preference")
        bundle = LayeredStrategy(cfg).assemble(s)
        assert MEMORY_GUARD_TEXT in bundle.prompt_block()

    def test_no_resident_no_block(self, env):
        cfg, kb = env
        bundle = LayeredStrategy(cfg).assemble(MemoryStore(kb, cfg))
        assert bundle.prompt_block() == ""
        assert bundle.total_tokens == 0

    def test_retrieved_layer_uses_callback(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("偏好", kind="misc")

        def retrieval(query, k):
            return [{"id": "c_1", "text": "一段原文片段" * 5}]

        bundle = LayeredStrategy(cfg).assemble(s, query="注意力机制", retrieval_fn=retrieval)
        assert bundle.retrieved and bundle.retrieved_tokens > 0

    def test_retrieval_failure_does_not_break_turn(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("偏好", kind="misc")

        def boom(q, k):
            raise RuntimeError("检索挂了")

        bundle = LayeredStrategy(cfg).assemble(s, query="x", retrieval_fn=boom)
        assert bundle.resident, "常驻层应仍然可用"
        assert any("检索失败" in n for n in bundle.notes)

    def test_read_refs_recorded(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        r = s.save("偏好", kind="misc")
        bundle = LayeredStrategy(cfg).assemble(s)
        assert r.memory_id in bundle.read_refs()["resident"]

    def test_budget_report(self, env):
        cfg, kb = env
        s = MemoryStore(kb, cfg)
        s.save("偏好", kind="misc")
        text = LayeredStrategy(cfg).budget_report(LayeredStrategy(cfg).assemble(s))
        assert "常驻层" in text and "按需层" in text


# ===========================================================================
# 不可变轮次 View
# ===========================================================================
class TestTurnView:
    def _conv(self, kb):
        return kb.add_conversation("测试会话")

    def test_freeze_and_get(self, env):
        cfg, kb = env
        conv = self._conv(kb)
        s = MemoryStore(kb, cfg)
        s.save("偏好一", kind="misc")
        bundle = LayeredStrategy(cfg).assemble(s)

        vs = TurnViewStore(kb)
        v = vs.freeze(conv, bundle)
        assert v.frozen
        assert v.resident_tokens == bundle.resident_tokens
        got = vs.get(conv, v.turn_seq)
        assert got is not None and got.id == v.id

    def test_immutable_cannot_overwrite(self, env):
        """不可变性是硬约束 —— 覆盖会让「可复现」失去意义。"""
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        v = vs.freeze(conv, turn_seq=1)
        with pytest.raises(ValueError, match="不可覆盖"):
            vs.freeze(conv, turn_seq=1)

    def test_auto_increment_turn_seq(self, env):
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        a = vs.freeze(conv)
        b = vs.freeze(conv)
        assert (a.turn_seq, b.turn_seq) == (1, 2)

    def test_bar_text(self, env):
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        v = vs.freeze(
            conv,
            read_refs={"resident": ["mem_a", "mem_b"], "retrieved": ["c_1"]},
            resident_tokens=50,
            retrieved_tokens=100,
        )
        assert "2 条偏好" in v.bar_text()
        assert "1 项检索内容" in v.bar_text()

    def test_bar_text_when_empty(self, env):
        cfg, kb = env
        v = TurnViewStore(kb).freeze(self._conv(kb))
        assert "未读取记忆" in v.bar_text()

    def test_written_refs_append_only(self, env):
        """写入是追加语义，不修改既有读视图 —— 不违反不可变性。"""
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        v = vs.freeze(conv, turn_seq=1)
        assert vs.update_written(v.id, ["mem_x"])
        assert vs.update_written(v.id, ["mem_y"])
        got = vs.get(conv, 1)
        assert got.written_refs == ["mem_x", "mem_y"]
        # 读视图未被改动
        assert got.read_refs == v.read_refs

    def test_reproducible_same_inputs(self, env):
        """同一轮重放得到同样的上下文。"""
        cfg, kb = env
        conv = self._conv(kb)
        s = MemoryStore(kb, cfg)
        s.save("稳定偏好", kind="misc")
        strat = LayeredStrategy(cfg)
        a = strat.assemble(s)
        b = strat.assemble(s)
        assert [m.id for m in a.resident] == [m.id for m in b.resident]
        assert a.resident_tokens == b.resident_tokens

    def test_latest_and_list_and_stats(self, env):
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        vs.freeze(conv)
        vs.freeze(conv)
        assert vs.latest(conv).turn_seq == 2
        assert len(vs.list(conv)) == 2
        assert vs.stats(conv)["turns"] == 2

    def test_format_views(self, env):
        cfg, kb = env
        conv = self._conv(kb)
        vs = TurnViewStore(kb)
        vs.freeze(conv, read_refs={"resident": ["a"]}, resident_tokens=10)
        assert "轮次" in format_views(vs.list(conv))
        assert format_views([]) == "（暂无轮次视图）"
