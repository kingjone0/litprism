"""提交事务（Seam）—— 压缩的**单一写入口**。

参考实现的教训：压缩必须「先立据再干活」，否则崩溃后会留下
「内容消失了但没有替代」或「有摘要但没有对应区间」的半成品。

流程
----
```
begin:   校验不变量 → 写入 compaction/start（同步，先立据）→ 释放准入
summary: 生成摘要（异步，可能失败）
commit:  重新校验区间是否仍有效 → 写 block → 写替换消息 → 写 end → 写账本
fail:    任何环节失败都写失败码（闭集），不静默吞掉
```

为什么失败码要闭集
------------------
「用 Error 抽象一切」的代价是上层无法分类、用户看不懂、遥测无法聚合。
每种失败都要能被分类、被理解、被统计。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Config, ContextConfig
from ..invariants import (
    FAILURE_CODES,
    CompactContext,
    MessageView,
    check_compaction,
)
from ..refs import make_ref, parse_ref
from .meter import Meter
from .policy import SelectionResult
from .summarizer import SummaryResult, parse_summary


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@dataclass
class CommitResult:
    ok: bool
    compaction_id: str
    status: str                          # committed | failed | busy
    failure_code: str | None = None
    message: str = ""
    block_id: str | None = None
    replacement_ref: str | None = None
    ledger: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def failure_text(self) -> str:
        if not self.failure_code:
            return ""
        return FAILURE_CODES.get(self.failure_code, self.failure_code)


class Seam:
    """压缩事务的单一写入口。"""

    def __init__(self, kb: Any, cfg: Config | ContextConfig | None = None):
        self.kb = kb
        if isinstance(cfg, Config):
            self.cfg = cfg.context
        elif isinstance(cfg, ContextConfig):
            self.cfg = cfg
        else:
            self.cfg = ContextConfig()
        self.meter = Meter(self.cfg)

    # ------------------------------------------------------------------
    # 事务状态查询
    # ------------------------------------------------------------------
    def open_tx(self, conversation_id: str) -> dict | None:
        """当前未闭合的事务（started / summarized）。"""
        row = self.kb._con.execute(
            "SELECT * FROM compaction_tx WHERE conversation_id = ? AND status IN ('started','summarized')"
            " ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None

    def last_failure(self, conversation_id: str, limit: int = 5) -> list[dict]:
        rows = self.kb._con.execute(
            "SELECT * FROM compaction_tx WHERE conversation_id = ? AND status = 'failed'"
            " ORDER BY created_at DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 阶段 1：立据
    # ------------------------------------------------------------------
    def begin(
        self,
        conversation_id: str,
        source_command: str,
        selection: SelectionResult,
    ) -> tuple[str | None, str | None]:
        """写入 start 记录。返回 ``(tx_id, failure_code)``。

        不变量 7 在这里拦截：已有未闭合事务时不得再开一个。
        """
        if not selection.ok or selection.start_seq is None or selection.end_seq is None:
            return None, "changed"

        if self.open_tx(conversation_id):
            return None, "busy"

        tx_id = _new_id("tx_")
        with self.kb.tx() as con:
            con.execute(
                """INSERT INTO compaction_tx
                   (id, conversation_id, source_command, status, start_seq, end_seq,
                    shadowed_refs, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    tx_id, conversation_id, source_command, "started",
                    selection.start_seq, selection.end_seq,
                    json.dumps(selection.shadowed_refs, ensure_ascii=False),
                    _now(), _now(),
                ),
            )
        return tx_id, None

    def mark_summarized(self, tx_id: str) -> None:
        with self.kb.tx() as con:
            con.execute(
                "UPDATE compaction_tx SET status = 'summarized', updated_at = ? WHERE id = ?",
                (_now(), tx_id),
            )

    # ------------------------------------------------------------------
    # 阶段 2：提交
    # ------------------------------------------------------------------
    def commit(
        self,
        tx_id: str,
        summary: SummaryResult,
        *,
        citation_before: dict | None = None,
        citation_after: dict | None = None,
        recheck_citations: Any = None,
    ) -> CommitResult:
        """提交压缩。前置校验不通过则**不写任何数据**。"""
        tx = self._get_tx(tx_id)
        if tx is None:
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="changed", message="事务记录不存在，可能已被清理",
            )
        if tx["status"] not in ("started", "summarized"):
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="busy", message=f"事务状态为 {tx['status']}，不可提交",
            )
        if not summary.ok:
            self.fail(tx_id, "summary", summary.error)
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="summary",
                message=summary.error or "摘要未通过收缩验证",
            )

        conversation_id = tx["conversation_id"]
        start_seq, end_seq = int(tx["start_seq"]), int(tx["end_seq"])
        shadowed_refs = json.loads(tx["shadowed_refs"] or "[]")

        # ---- 重新校验区间是否仍有效（可能已被别的操作改变）----
        messages = self._load_messages(conversation_id, start_seq, end_seq)
        if not messages:
            self.fail(tx_id, "changed", "区间内已无消息")
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="changed", message="区间内已无消息，历史可能已变化",
            )

        # ---- 不变量校验（含 LitPrism 特有第 9 条）----
        ctx = CompactContext(
            compaction_id=tx_id,
            source_command=tx["source_command"],
            conversation_id=conversation_id,
            start_seq=start_seq,
            end_seq=end_seq,
            shadowed_refs=shadowed_refs,
            messages=[
                MessageView(
                    seq=m["seq"], ref=m["ref"], role=m["role"],
                    has_citations=bool(m.get("citations")),
                )
                for m in messages
            ],
            replacement_refs=list(shadowed_refs),
            open_tx_exists=False,          # 已持有事务，此处不再判重
            has_summary=True,
            has_end=True,
            citation_before=citation_before,
            citation_after=citation_after,
            recheck_citations=recheck_citations,
        )
        check = check_compaction(ctx)
        if not check.ok:
            detail = "；".join(str(v) for v in check.errors)
            self.fail(tx_id, "invariant", detail)
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="invariant",
                message="提交前不变量校验未通过，已阻止本次压缩",
                violations=[str(v) for v in check.errors],
            )

        # ---- 写入：block → 替换消息 → end → 账本 ----
        block_id = _new_id("blk_")
        next_seq = self._next_seq(conversation_id)
        replacement_ref = make_ref(next_seq)
        summary_body = parse_summary(summary.framed)
        handles = self._collect_citation_handles(messages)

        try:
            with self.kb.tx() as con:
                con.execute(
                    """INSERT INTO compaction_blocks
                       (id, conversation_id, tier, topic, summary, direct_refs, effective_refs,
                        created_at, survived_count, active, shadowed_token_count,
                        summary_token_count, citation_handles)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        block_id, conversation_id, 1, summary.topic, summary_body,
                        json.dumps(shadowed_refs, ensure_ascii=False),
                        json.dumps(shadowed_refs, ensure_ascii=False),   # 本项目不做多代，血统即直接覆盖
                        _now(), 0, 1,
                        summary.shadowed_tokens, summary.framed_tokens,
                        json.dumps(handles, ensure_ascii=False),
                    ),
                )
                con.execute(
                    """INSERT INTO messages
                       (id, conversation_id, seq, ref, role, content, kind, shadowed_refs, block_id, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _new_id("m_"), conversation_id, next_seq, replacement_ref,
                        "user", summary.framed, "compact-checkpoint",
                        json.dumps(shadowed_refs, ensure_ascii=False), block_id, _now(),
                    ),
                )
                con.execute(
                    "UPDATE compaction_tx SET status = 'committed', updated_at = ? WHERE id = ?",
                    (_now(), tx_id),
                )
                con.execute(
                    "UPDATE conversations SET compact_anchor_seq = ?, updated_at = ? WHERE id = ?",
                    (next_seq, _now(), conversation_id),
                )
                con.execute(
                    """INSERT INTO compaction_ledger
                       (id, conversation_id, block_id, trigger, shadowed_tokens,
                        summary_tokens, repriced_tokens, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        _new_id("lg_"), conversation_id, block_id, tx["source_command"],
                        summary.shadowed_tokens, summary.framed_tokens, 0, _now(),
                    ),
                )
        except Exception as e:  # noqa: BLE001
            self.fail(tx_id, "commit", f"{type(e).__name__}: {e}")
            return CommitResult(
                ok=False, compaction_id=tx_id, status="failed",
                failure_code="commit",
                message=f"写入失败：{type(e).__name__}: {e}。事务已标记失败，重试前请检查状态。",
            )

        return CommitResult(
            ok=True, compaction_id=tx_id, status="committed",
            message=summary.describe(),
            block_id=block_id,
            replacement_ref=replacement_ref,
            ledger={
                "shadowed_tokens": summary.shadowed_tokens,
                "summary_tokens": summary.framed_tokens,
                "trigger": tx["source_command"],
                "saved_per_turn": max(0, summary.shadowed_tokens - summary.framed_tokens),
            },
        )

    # ------------------------------------------------------------------
    def fail(self, tx_id: str, code: str, detail: str = "") -> None:
        """记录失败。失败码必须来自闭集。"""
        if code not in FAILURE_CODES:
            code = "commit"
        try:
            with self.kb.tx() as con:
                con.execute(
                    "UPDATE compaction_tx SET status = 'failed', failure_code = ?,"
                    " failure_detail = ?, updated_at = ? WHERE id = ?",
                    (code, detail[:2000], _now(), tx_id),
                )
        except Exception:  # noqa: BLE001 - 记录失败时不该再抛
            pass

    def cancel(self, tx_id: str) -> None:
        self.fail(tx_id, "cancelled", "用户或上层取消")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _get_tx(self, tx_id: str) -> dict | None:
        row = self.kb._con.execute("SELECT * FROM compaction_tx WHERE id = ?", (tx_id,)).fetchone()
        return dict(row) if row else None

    def _load_messages(self, conversation_id: str, start_seq: int, end_seq: int) -> list[dict]:
        rows = self.kb._con.execute(
            "SELECT seq, ref, role, content, citations, kind FROM messages"
            " WHERE conversation_id = ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (conversation_id, start_seq, end_seq),
        ).fetchall()
        return [dict(r) for r in rows]

    def _next_seq(self, conversation_id: str) -> int:
        row = self.kb._con.execute(
            "SELECT MAX(seq) AS m FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return int((row["m"] if row and row["m"] is not None else 0) + 1)

    @staticmethod
    def _collect_citation_handles(messages: list[dict]) -> list[dict]:
        """收集被折叠区间涉及的引用句柄。

        **正文可折叠，句柄永不折叠** —— 这是可追溯性的最小载体。
        有了它，被折掉的引用仍能按 chunk_id 从 chunks 表取回原文。
        """
        handles: list[dict] = []
        seen: set[str] = set()
        for m in messages:
            raw = m.get("citations")
            if not raw:
                continue
            try:
                items = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            for it in items or []:
                cid = it.get("chunk_id") if isinstance(it, dict) else None
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                handles.append(
                    {
                        "chunk_id": cid,
                        "paper_id": it.get("paper_id"),
                        "page": it.get("page"),
                    }
                )
        return handles


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def estimate_payback(shadowed_tokens: int, summary_tokens: int, repriced_tokens: int = 0) -> dict:
    """单次折叠损益：回答「这次压缩回本了吗」。

    ``n* = T / (S - σ)``，其中 T 是本次折叠造成的重付、S 是被遮蔽的 token、
    σ 是摘要本身。``n*`` 就是回本所需轮数。
    """
    per_turn = max(0, shadowed_tokens - summary_tokens)
    if per_turn <= 0:
        return {"per_turn_saving": 0, "breakeven_turns": None, "note": "每次节省为 0，永不回本"}
    t = max(0, repriced_tokens)
    if t == 0:
        return {"per_turn_saving": per_turn, "breakeven_turns": 0, "note": "无重付，立即回本"}
    import math

    return {
        "per_turn_saving": per_turn,
        "breakeven_turns": int(math.ceil(t / per_turn)),
        "note": f"每次省 {per_turn} token，需 {math.ceil(t / per_turn)} 轮收回 {t} token 重付",
    }
