"""Summarizer 与 Seam 测试。

重点：
1. **收缩验证不可省** —— 摘要长于被遮蔽内容时必须被拒，且要把原因反馈给模型重写；
2. **先立据再干活** —— 提交失败时不留下半成品；
3. **失败码是闭集** —— 每种失败都能被分类；
4. **引用句柄永不折叠** —— 被折掉的引用仍能按 chunk_id 取回原文。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config, ContextConfig  # noqa: E402
from litprism.context.meter import Meter  # noqa: E402
from litprism.context.policy import Policy  # noqa: E402
from litprism.context.seam import Seam, estimate_payback  # noqa: E402
from litprism.context.summarizer import (  # noqa: E402
    CHECKPOINT_PREAMBLE,
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    ExtractiveSummarizer,
    Summarizer,
    frame_summary,
    parse_summary,
    split_topic,
)
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.invariants import FAILURE_CODES  # noqa: E402
from litprism.refs import make_ref  # noqa: E402


def _cfg(**over) -> ContextConfig:
    base = dict(window_tokens=200000, step_tokens=30000, retain_tokens=6000, recent_turns_protected=2)
    base.update(over)
    return ContextConfig(**base)


# ===========================================================================
# 摘要
# ===========================================================================
class TestFrameAndParse:
    def test_frame_contains_preamble_and_fence(self):
        framed = frame_summary("摘要正文")
        assert CHECKPOINT_PREAMBLE in framed
        assert SUMMARY_OPEN in framed and SUMMARY_CLOSE in framed
        assert "摘要正文" in framed

    def test_preamble_prevents_meta_commentary(self):
        """preamble 要明确告诉模型别评论这个检查点。"""
        assert "不要提及本检查点" in CHECKPOINT_PREAMBLE
        assert "不要复述" in CHECKPOINT_PREAMBLE

    def test_roundtrip(self):
        assert parse_summary(frame_summary("要点一\n要点二")) == "要点一\n要点二"

    def test_parse_without_fence_returns_input(self):
        assert parse_summary("裸文本") == "裸文本"

    def test_split_topic(self):
        topic, body = split_topic("注意力机制讨论\n\n- 要点一\n- 要点二")
        assert topic == "注意力机制讨论"
        assert body.startswith("- 要点一")

    def test_split_topic_no_heading(self):
        long_first = "这是一个很长的第一行" * 5
        topic, body = split_topic(f"{long_first}\n第二行")
        assert topic == ""
        assert body


class TestShrinkageVerification:
    def test_rejects_summary_longer_than_shadowed(self):
        """没有这一步会偶尔得到「压缩后上下文变大」。"""
        s = Summarizer(_cfg())
        ok, why = s.verify_shrinkage(framed_tokens=1000, shadowed_tokens=500)
        assert ok is False
        assert "未小于" in why

    def test_rejects_when_ratio_above_threshold(self):
        s = Summarizer(_cfg(summary_max_ratio=0.6))
        ok, why = s.verify_shrinkage(framed_tokens=800, shadowed_tokens=1000)   # 0.8 > 0.6
        assert ok is False
        assert "压缩率" in why

    def test_accepts_good_ratio(self):
        s = Summarizer(_cfg(summary_max_ratio=0.6))
        ok, why = s.verify_shrinkage(framed_tokens=300, shadowed_tokens=1000)
        assert ok is True
        assert "通过" in why

    def test_rejects_zero_shadowed(self):
        s = Summarizer(_cfg())
        ok, _ = s.verify_shrinkage(framed_tokens=10, shadowed_tokens=0)
        assert ok is False


class TestSummarizerFlow:
    def _msgs(self, n=6):
        out = []
        for i in range(n):
            out.append({"seq": i + 1, "ref": make_ref(i + 1), "role": "user", "content": f"第{i}个问题。" * 50})
            out.append({"seq": i + 2, "ref": make_ref(i + 2), "role": "assistant", "content": f"第{i}个回答。" * 50})
        return out

    def test_extractive_backend_produces_valid_summary(self):
        s = Summarizer(_cfg(), backend=ExtractiveSummarizer())
        msgs = self._msgs(8)
        shadowed = sum(Meter(_cfg()).estimate_message(m) for m in msgs)
        r = s.summarize(msgs, shadowed)
        assert r.ok, r.error
        assert r.framed_tokens < shadowed
        assert r.topic

    def test_retries_with_feedback_on_rejection(self):
        """被拒时要把原因反馈给模型重写，而不是直接放弃。"""
        calls: list[str] = []

        def backend(system: str, user: str) -> str:
            calls.append(user)
            # 第一次给超长摘要（触发拒绝），第二次给合格摘要
            return "标题\n\n" + ("废话内容" * 5000 if len(calls) == 1 else "简短摘要")

        s = Summarizer(_cfg(summary_max_ratio=0.6), backend=backend, max_attempts=3)
        r = s.summarize(self._msgs(4), shadowed_tokens=5000)
        assert r.ok, r.error
        assert r.attempts == 2
        assert len(r.rejection_log) == 1
        assert "未通过收缩验证" in calls[1], "第二次调用必须带上重写反馈"

    def test_gives_up_after_max_attempts(self):
        s = Summarizer(_cfg(), backend=lambda sys_, u: "超长" * 5000, max_attempts=2)
        r = s.summarize(self._msgs(2), shadowed_tokens=100)
        assert r.ok is False
        assert r.attempts == 2
        assert "收缩验证" in r.error
        assert len(r.rejection_log) == 2

    def test_backend_exception_is_reported(self):
        def boom(sys_, u):
            raise RuntimeError("网络挂了")

        r = Summarizer(_cfg(), backend=boom).summarize(self._msgs(2), shadowed_tokens=1000)
        assert r.ok is False
        assert "网络挂了" in r.error

    def test_transcript_includes_refs_and_citations(self):
        s = Summarizer(_cfg())
        msgs = [
            {"seq": 1, "ref": "m00001", "role": "user", "content": "问题"},
            {"seq": 2, "ref": "m00002", "role": "assistant", "content": "回答",
             "citations": json.dumps([{"chunk_id": "c_x", "page": 3}])},
        ]
        text = s.render_transcript(msgs)
        assert "m00001" in text and "m00002" in text
        assert "c_x" in text, "引用信息必须进转录文本，否则摘要会丢引用"

    def test_transcript_merges_existing_summaries(self):
        s = Summarizer(_cfg())
        text = s.render_transcript([{"seq": 1, "ref": "m1", "role": "user", "content": "x"}],
                                   existing_summaries=["旧摘要内容"])
        assert "旧摘要内容" in text
        assert "不要丢弃" in text


# ===========================================================================
# Seam（提交事务）
# ===========================================================================
@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.context = _cfg()
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")

    conv = "conv_test"
    kb._con.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at, compact_anchor_seq)"
        " VALUES (?,?,?,?,0)",
        (conv, "测试会话", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    msgs = [{"seq": 1, "role": "system", "content": "系统提示词"}]
    seq = 2
    for i in range(10):
        msgs.append({"seq": seq, "role": "user", "content": f"第{i}个问题。" * 400, "citations": None})
        seq += 1
        cites = json.dumps([{"chunk_id": f"c_{i}", "paper_id": "p_1", "page": i + 1}]) if i < 3 else None
        msgs.append({"seq": seq, "role": "assistant", "content": f"第{i}个回答。" * 400, "citations": cites})
        seq += 1
    for m in msgs:
        kb._con.execute(
            "INSERT INTO messages (id, conversation_id, seq, ref, role, content, citations, kind, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
                (f"m_{m['seq']}", conv, m["seq"], make_ref(m["seq"]), m["role"],
                 m["content"], m.get("citations"), "normal", "2026-01-01T00:00:00"),
        )
    kb._con.commit()
    yield cfg, kb, conv
    kb.close()


def _load_msgs(kb, conv):
    rows = kb._con.execute(
        "SELECT seq, ref, role, content, citations, kind FROM messages"
        " WHERE conversation_id = ? ORDER BY seq",
        (conv,),
    ).fetchall()
    return [dict(r) for r in rows]


class TestSeamBegin:
    def test_begin_writes_start_record(self, env):
        cfg, kb, conv = env
        seam = Seam(kb, cfg)
        msgs = _load_msgs(kb, conv)
        sel = Policy(cfg).select(Meter(cfg).measure_nodes(msgs), messages=msgs)
        assert sel.ok

        tx_id, code = seam.begin(conv, "nudge", sel)
        assert tx_id and code is None
        row = kb._con.execute("SELECT * FROM compaction_tx WHERE id = ?", (tx_id,)).fetchone()
        assert row["status"] == "started"
        assert json.loads(row["shadowed_refs"]) == sel.shadowed_refs

    def test_busy_when_open_tx_exists(self, env):
        """不变量 7：已有未闭合事务时不得再开一个。"""
        cfg, kb, conv = env
        seam = Seam(kb, cfg)
        msgs = _load_msgs(kb, conv)
        sel = Policy(cfg).select(Meter(cfg).measure_nodes(msgs), messages=msgs)

        tx1, _ = seam.begin(conv, "nudge", sel)
        tx2, code = seam.begin(conv, "nudge", sel)
        assert tx1 and tx2 is None
        assert code == "busy"

    def test_rejects_invalid_selection(self, env):
        cfg, kb, conv = env
        from litprism.context.policy import SelectionResult

        tx_id, code = Seam(kb, cfg).begin(conv, "nudge", SelectionResult(ok=False))
        assert tx_id is None and code == "changed"


class TestSeamCommit:
    def _prepare(self, env):
        cfg, kb, conv = env
        seam = Seam(kb, cfg)
        msgs = _load_msgs(kb, conv)
        sel = Policy(cfg).select(Meter(cfg).measure_nodes(msgs), messages=msgs)
        tx_id, _ = seam.begin(conv, "nudge", sel)
        s = Summarizer(cfg, backend=ExtractiveSummarizer())
        in_range = [m for m in msgs if sel.start_seq <= m["seq"] <= sel.end_seq]
        summary = s.summarize(in_range, sel.shadowed_tokens)
        return seam, tx_id, sel, summary

    def test_happy_path_commits_everything(self, env):
        cfg, kb, conv = env
        seam, tx_id, sel, summary = self._prepare(env)
        assert summary.ok, summary.error

        r = seam.commit(tx_id, summary)
        assert r.ok, r.message
        assert r.block_id and r.replacement_ref

        # 事务闭合
        tx = kb._con.execute("SELECT * FROM compaction_tx WHERE id = ?", (tx_id,)).fetchone()
        assert tx["status"] == "committed"

        # 压缩块写入
        blk = kb._con.execute("SELECT * FROM compaction_blocks WHERE id = ?", (r.block_id,)).fetchone()
        assert blk["active"] == 1
        assert json.loads(blk["direct_refs"]) == sel.shadowed_refs

        # 替换消息写入
        rep = kb._con.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND kind = 'compact-checkpoint'", (conv,)
        ).fetchone()
        assert rep is not None and rep["ref"] == r.replacement_ref
        assert SUMMARY_OPEN in rep["content"]

        # 锚点推进
        c = kb._con.execute("SELECT compact_anchor_seq FROM conversations WHERE id = ?", (conv,)).fetchone()
        assert c["compact_anchor_seq"] == int(parse_ref_of(r.replacement_ref))

        # 账本写入
        lg = kb._con.execute("SELECT * FROM compaction_ledger WHERE block_id = ?", (r.block_id,)).fetchone()
        assert lg is not None and lg["shadowed_tokens"] == summary.shadowed_tokens

    def test_citation_handles_preserved(self, env):
        """引用句柄永不折叠 —— 被折掉的引用仍能按 chunk_id 取回原文。"""
        cfg, kb, conv = env
        seam, tx_id, sel, summary = self._prepare(env)
        r = seam.commit(tx_id, summary)
        assert r.ok

        blk = kb._con.execute("SELECT citation_handles FROM compaction_blocks WHERE id = ?", (r.block_id,)).fetchone()
        handles = json.loads(blk["citation_handles"])
        assert handles, "被折区间含引用，必须留下句柄"
        assert all("chunk_id" in h for h in handles)

    def test_commit_after_summary_failure_records_code(self, env):
        cfg, kb, conv = env
        seam, tx_id, sel, _ = self._prepare(env)
        from litprism.context.summarizer import SummaryResult

        r = seam.commit(tx_id, SummaryResult(ok=False, error="摘要未通过收缩验证"))
        assert r.ok is False
        assert r.failure_code == "summary"
        tx = kb._con.execute("SELECT * FROM compaction_tx WHERE id = ?", (tx_id,)).fetchone()
        assert tx["status"] == "failed" and tx["failure_code"] == "summary"

    def test_failed_commit_leaves_no_block(self, env):
        """先立据再干活：失败时不留下「内容消失但没有替代」的半成品。"""
        cfg, kb, conv = env
        seam, tx_id, sel, _ = self._prepare(env)
        from litprism.context.summarizer import SummaryResult

        seam.commit(tx_id, SummaryResult(ok=False, error="x"))
        blocks = kb._con.execute("SELECT COUNT(*) n FROM compaction_blocks").fetchone()["n"]
        repl = kb._con.execute(
            "SELECT COUNT(*) n FROM messages WHERE kind = 'compact-checkpoint'"
        ).fetchone()["n"]
        assert blocks == 0 and repl == 0

    def test_cannot_commit_twice(self, env):
        cfg, kb, conv = env
        seam, tx_id, sel, summary = self._prepare(env)
        assert seam.commit(tx_id, summary).ok
        r2 = seam.commit(tx_id, summary)
        assert r2.ok is False and r2.failure_code == "busy"

    def test_unknown_tx_id(self, env):
        cfg, kb, conv = env
        r = Seam(kb, cfg).commit("tx_nonexistent", None)  # type: ignore[arg-type]
        assert r.ok is False and r.failure_code == "changed"

    def test_cancel_records_code(self, env):
        cfg, kb, conv = env
        seam, tx_id, sel, _ = self._prepare(env)
        seam.cancel(tx_id)
        tx = kb._con.execute("SELECT * FROM compaction_tx WHERE id = ?", (tx_id,)).fetchone()
        assert tx["status"] == "failed" and tx["failure_code"] == "cancelled"

    def test_failure_code_always_in_closed_set(self, env):
        cfg, kb, conv = env
        seam, tx_id, sel, summary = self._prepare(env)
        r = seam.commit(tx_id, summary)
        if r.failure_code:
            assert r.failure_code in FAILURE_CODES

    def test_shadowed_messages_no_longer_visible(self, env):
        """提交后，被遮蔽的消息不再进入发送视图。"""
        cfg, kb, conv = env
        seam, tx_id, sel, summary = self._prepare(env)
        r = seam.commit(tx_id, summary)
        assert r.ok

        msgs = _load_msgs(kb, conv)
        meas = Meter(cfg).measure_nodes(msgs, [
            dict(x) for x in kb._con.execute(
                "SELECT id, summary, direct_refs, active FROM compaction_blocks WHERE conversation_id = ?", (conv,)
            ).fetchall()
        ])
        visible = {n.seq for n in meas.nodes}
        for ref in sel.shadowed_refs:
            assert int(parse_ref_of(ref)) not in visible
        assert meas.total_tokens > meas.surface_tokens


class TestPayback:
    def test_breakeven_calculation(self):
        r = estimate_payback(shadowed_tokens=10000, summary_tokens=1000, repriced_tokens=9000)
        assert r["per_turn_saving"] == 9000
        assert r["breakeven_turns"] == 1

    def test_zero_repricing_is_immediate(self):
        r = estimate_payback(10000, 1000, 0)
        assert r["breakeven_turns"] == 0

    def test_no_saving_never_pays_back(self):
        r = estimate_payback(1000, 1000, 5000)
        assert r["breakeven_turns"] is None
        assert "永不回本" in r["note"]


def parse_ref_of(ref: str) -> int:
    from litprism.refs import parse_ref

    return parse_ref(ref)
