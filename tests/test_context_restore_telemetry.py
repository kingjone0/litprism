"""Restore（可逆性）与 Telemetry（可观测性）测试。

重点：
1. **chunks 表就是内容寻址存储** —— 被折掉的引用能按 chunk_id 一条 SQL 取回原文；
2. **还原到文件** —— 全保真写盘，而不是灌回上下文；
3. **两种占用必须分开展示** —— 否则用户以为压缩没生效；
4. **miss 归因如实标注"含无法归因部分"** —— 不假装能精确归因。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config, ContextConfig  # noqa: E402
from litprism.context.restore import Restorer, format_restore  # noqa: E402
from litprism.context.summarizer import frame_summary  # noqa: E402
from litprism.context.telemetry import (  # noqa: E402
    Telemetry,
    TokenUsage,
    attribute_miss,
    format_snapshot,
)
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.refs import make_ref  # noqa: E402


def _cfg(**over) -> ContextConfig:
    base = dict(window_tokens=200000, step_tokens=30000, retain_tokens=6000, recent_turns_protected=2)
    base.update(over)
    return ContextConfig(**base)


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.context = _cfg()
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")

    conv = "conv_r"
    kb._con.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at, compact_anchor_seq)"
        " VALUES (?,?,?,?,0)",
        (conv, "还原测试", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    # 论文 + 分块（内容寻址存储）
    kb._con.execute(
        "INSERT INTO papers (id, title, year, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("p_1", "Attention Is All You Need", 2017, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    for i in range(3):
        kb._con.execute(
            "INSERT INTO chunks (id, paper_id, page, section, text) VALUES (?,?,?,?,?)",
            (f"c_{i}", "p_1", i + 1, "方法", f"这是第 {i + 1} 页的原文内容。" * 20),
        )

    # 消息：seq 3、4 带引用，将被折叠
    rows = [
        (1, "system", "系统提示词", None),
        (2, "user", "第一个问题", None),
        (3, "assistant", "回答一", json.dumps([{"chunk_id": "c_0", "paper_id": "p_1", "page": 1}])),
        (4, "assistant", "回答二", json.dumps([{"chunk_id": "c_1", "paper_id": "p_1", "page": 2}])),
        (5, "user", "最近问题", None),
    ]
    for seq, role, content, cites in rows:
        kb._con.execute(
            "INSERT INTO messages (id, conversation_id, seq, ref, role, content, citations, kind, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (f"m_{seq}", conv, seq, make_ref(seq), role, content, cites, "normal", "2026-01-01T00:00:00"),
        )

    # 压缩块：折叠 seq 3-4，保留引用句柄
    kb._con.execute(
        """INSERT INTO compaction_blocks
           (id, conversation_id, tier, topic, summary, direct_refs, effective_refs,
            created_at, survived_count, active, shadowed_token_count, summary_token_count, citation_handles)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "blk_1", conv, 1, "早期问答",
            frame_summary("此前讨论了两轮问答。"),
            json.dumps(["m00003", "m00004"]), json.dumps(["m00003", "m00004"]),
            "2026-01-01T00:00:00", 2, 1, 5000, 200,
            json.dumps([
                {"chunk_id": "c_0", "paper_id": "p_1", "page": 1},
                {"chunk_id": "c_1", "paper_id": "p_1", "page": 2},
            ]),
        ),
    )
    # 替换消息
    kb._con.execute(
        "INSERT INTO messages (id, conversation_id, seq, ref, role, content, kind, shadowed_refs, block_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("m_6", conv, 6, make_ref(6), "user", frame_summary("此前讨论了两轮问答。"),
         "compact-checkpoint", json.dumps(["m00003", "m00004"]), "blk_1", "2026-01-01T00:00:00"),
    )
    # 账本
    kb._con.execute(
        "INSERT INTO compaction_ledger (id, conversation_id, block_id, trigger, shadowed_tokens,"
        " summary_tokens, repriced_tokens, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("lg_1", conv, "blk_1", "nudge", 5000, 200, 1800, "2026-01-01T00:00:00"),
    )
    kb._con.commit()
    yield cfg, kb, conv
    kb.close()


# ===========================================================================
# Restore
# ===========================================================================
class TestContentAddressableFetch:
    def test_fetch_chunk_from_store(self, env):
        """chunks 表就是内容寻址存储 —— 这是 LitPrism 相对通用 agent 的结构性优势。"""
        cfg, kb, _ = env
        r = Restorer(kb, cfg)
        c = r.fetch_chunk("c_0")
        assert c is not None
        assert c["page"] == 1
        assert c["paper_title"] == "Attention Is All You Need"
        assert "原文内容" in c["text"]

    def test_fetch_missing_chunk_returns_none(self, env):
        cfg, kb, _ = env
        assert Restorer(kb, cfg).fetch_chunk("c_nope") is None

    def test_fetch_chunks_batch(self, env):
        cfg, kb, _ = env
        out = Restorer(kb, cfg).fetch_chunks(["c_0", "c_1", "c_missing"])
        assert len(out) == 2


class TestRestoreBlock:
    def test_restores_messages_and_citations(self, env):
        cfg, kb, _ = env
        res = Restorer(kb, cfg).restore_block("blk_1")
        assert res.ok
        assert [m["seq"] for m in res.messages] == [3, 4]
        assert len(res.citations) == 2
        assert res.missing_chunks == []
        assert res.restored_tokens > 0

    def test_reports_missing_handles(self, env):
        """句柄失效（论文被删）时必须显式报告，不能静默。"""
        cfg, kb, _ = env
        kb._con.execute("DELETE FROM chunks WHERE id = 'c_1'")
        kb._con.commit()
        res = Restorer(kb, cfg).restore_block("blk_1")
        assert res.ok
        assert res.missing_chunks == ["c_1"]

    def test_unknown_block(self, env):
        cfg, kb, _ = env
        res = Restorer(kb, cfg).restore_block("blk_nope")
        assert res.ok is False
        assert "不存在" in res.error

    def test_render_includes_everything(self, env):
        cfg, kb, _ = env
        text = Restorer(kb, cfg).render(Restorer(kb, cfg).restore_block("blk_1"))
        assert "被折叠的原始消息" in text
        assert "引用的原文" in text
        assert "c_0" in text
        assert "回答一" in text

    def test_restore_to_file_writes_full_fidelity(self, env, tmp_path):
        """还原到文件 —— 既不炸上下文窗口，也不作废前缀缓存。"""
        cfg, kb, _ = env
        out = tmp_path / "restored.md"
        p = Restorer(kb, cfg).restore_to_file("blk_1", out)
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "回答一" in text and "原文内容" in text

    def test_list_blocks(self, env):
        cfg, kb, conv = env
        blocks = Restorer(kb, cfg).list_blocks(conv)
        assert len(blocks) == 1
        assert blocks[0]["direct_refs"] == ["m00003", "m00004"]
        assert len(blocks[0]["citation_handles"]) == 2

    def test_stats(self, env):
        cfg, kb, conv = env
        st = Restorer(kb, cfg).stats(conv)
        assert st["blocks"] == 1 and st["active_blocks"] == 1
        assert st["citation_handles"] == 2
        assert st["shadowed_tokens"] == 5000

    def test_format_restore(self, env):
        cfg, kb, _ = env
        assert "还原 2 条消息" in format_restore(Restorer(kb, cfg).restore_block("blk_1"))


# ===========================================================================
# Telemetry
# ===========================================================================
class TestMissAttribution:
    def test_identity_holds(self):
        """恒等式：new_content + comp_repay == missed"""
        prev = TokenUsage(uncached_input=1000, cache_read=9000)
        cur = TokenUsage(uncached_input=3000, cache_read=12000)
        a = attribute_miss(prev, cur)
        assert a.new_content + a.comp_repay == a.missed

    def test_no_prev_counts_all_as_new(self):
        a = attribute_miss(None, TokenUsage(uncached_input=500, cache_read=500))
        assert a.missed == 500 and a.new_content == 500 and a.comp_repay == 0

    def test_compression_repay_detected(self):
        """输入没长但 miss 增加 → 归因为压缩重付。"""
        prev = TokenUsage(uncached_input=1000, cache_read=9000)
        cur = TokenUsage(uncached_input=4000, cache_read=6000)   # input 总量不变
        a = attribute_miss(prev, cur)
        assert a.missed == 4000
        assert a.new_content == 0
        assert a.comp_repay == 4000

    def test_note_admits_imprecision(self):
        """如实标注「含无法归因部分」，不假装精确。"""
        a = attribute_miss(TokenUsage(1000, 0), TokenUsage(2000, 0))
        assert "无法归因" in a.note

    def test_describe(self):
        a = attribute_miss(TokenUsage(1000, 0), TokenUsage(2000, 0))
        assert "miss" in a.describe()
        assert "无缓存 miss" in attribute_miss(None, TokenUsage(0, 0)).describe()


class TestTokenUsage:
    def test_cache_hit_ratio(self):
        u = TokenUsage(uncached_input=1000, cache_read=3000)
        assert u.input_total == 4000
        assert u.cache_hit_ratio == 0.75

    def test_zero_input_no_divide_by_zero(self):
        assert TokenUsage().cache_hit_ratio == 0.0


class TestSnapshot:
    def test_snapshot_has_three_projections(self, env):
        cfg, kb, conv = env
        snap = Telemetry(kb, cfg).snapshot(conv)
        assert "token_usage" in snap
        assert "context_pressure" in snap
        assert "context_breakdown" in snap
        assert snap["context_pressure"]["source"] in ("估算值", "提供方上报")

    def test_two_kinds_of_occupancy_are_separate(self, env):
        """两种占用必须分开 —— 混用会让用户以为压缩没生效。"""
        cfg, kb, conv = env
        snap = Telemetry(kb, cfg).snapshot(conv)
        occ = snap["two_kinds_of_occupancy"]
        assert occ["total_tokens"] > occ["surface_tokens"]
        assert "必须分开看" in occ["note"]

    def test_breakdown_has_summary_not_folded_into_system(self, env):
        cfg, kb, conv = env
        bd = Telemetry(kb, cfg).snapshot(conv)["context_breakdown"]
        assert bd["summary"] > 0, "摘要块必须单独计，不能并进 system"
        assert bd["total"] > 0

    def test_blocks_and_ledger_present(self, env):
        cfg, kb, conv = env
        snap = Telemetry(kb, cfg).snapshot(conv)
        assert len(snap["blocks"]) == 1
        assert snap["blocks"][0]["handles"] == 2
        assert len(snap["ledger"]) == 1
        assert snap["ledger"][0]["saved_per_turn"] == 4800

    def test_ledger_breakeven(self, env):
        cfg, kb, conv = env
        entry = Telemetry(kb, cfg).ledger(conv)[0]
        # 重付 1800，每轮省 4800 → 1 轮回本
        assert entry["breakeven_turns"] == 1

    def test_totals(self, env):
        cfg, kb, conv = env
        t = Telemetry(kb, cfg).totals(conv)
        assert t["compactions"] == 1
        assert t["net_saving_per_turn"] == 4800
        assert t["compression_ratio"] == round(200 / 5000, 4)

    def test_reported_usage_preferred(self, env):
        cfg, kb, conv = env
        tel = Telemetry(kb, cfg)
        tel.record_usage(conv, TokenUsage(uncached_input=1000, cache_read=5000))
        snap = tel.snapshot(conv)
        assert snap["token_usage"]["cache_hit_ratio"] == round(5000 / 6000, 4)

    def test_format_snapshot_renders(self, env):
        cfg, kb, conv = env
        text = format_snapshot(Telemetry(kb, cfg).snapshot(conv))
        assert "会话记账" in text and "发送视图" in text
        assert "构成分解" in text and "压缩账本" in text
