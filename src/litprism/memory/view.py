"""不可变轮次 View（Turn View）。

借鉴 `dsh-mnemon` 的 Core + View 设计：**每轮检索到的记忆固化为一个不可变 View**，
pin 到该轮。好处有三：

* **一致性** —— 该轮上下文在生成过程中不会漂移
* **可审计** —— `turn memory bar` 展示"本轮到底读写了什么"
* **可复现** —— 同一轮重放得到同样的上下文

界面上每条回复下方一行小字：
``本轮读取：3 条偏好 · 8 个片段 · 2 篇论文解读``，点击展开明细。

**不可变性是硬约束**：一旦冻结，任何修改请求都被拒绝 ——
否则"可复现"就无从谈起。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .strategy import MemoryBundle


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class TurnView:
    id: str
    conversation_id: str
    turn_seq: int
    read_refs: dict = field(default_factory=dict)
    written_refs: list[str] = field(default_factory=list)
    resident_tokens: int = 0
    retrieved_tokens: int = 0
    frozen: bool = True
    created_at: str = ""

    @property
    def total_tokens(self) -> int:
        return self.resident_tokens + self.retrieved_tokens

    def bar_text(self) -> str:
        """回复下方那一行小字。"""
        resident = len(self.read_refs.get("resident") or [])
        retrieved = len(self.read_refs.get("retrieved") or [])
        parts = []
        if resident:
            parts.append(f"{resident} 条偏好")
        if retrieved:
            parts.append(f"{retrieved} 项检索内容")
        if self.written_refs:
            parts.append(f"写入 {len(self.written_refs)} 条")
        if not parts:
            return "本轮未读取记忆"
        return "本轮读取：" + " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "turn_seq": self.turn_seq,
            "read_refs": self.read_refs,
            "written_refs": self.written_refs,
            "resident_tokens": self.resident_tokens,
            "retrieved_tokens": self.retrieved_tokens,
            "total_tokens": self.total_tokens,
            "frozen": self.frozen,
            "created_at": self.created_at,
            "bar": self.bar_text(),
        }


class TurnViewStore:
    """轮次 View 的冻结与读取。"""

    def __init__(self, kb: Any):
        self.kb = kb

    # ------------------------------------------------------------------
    def next_turn_seq(self, conversation_id: str) -> int:
        row = self.kb._con.execute(
            "SELECT COALESCE(MAX(turn_seq), 0) AS m FROM turn_views WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row["m"]) + 1

    def freeze(
        self,
        conversation_id: str,
        bundle: MemoryBundle | None = None,
        *,
        turn_seq: int | None = None,
        read_refs: dict | None = None,
        written_refs: list[str] | None = None,
        resident_tokens: int | None = None,
        retrieved_tokens: int | None = None,
    ) -> TurnView:
        """冻结一轮的 View。

        已存在同一 turn_seq 的 View 时**拒绝覆盖** —— 不可变性是硬约束，
        覆盖会让"可复现"失去意义。
        """
        seq = turn_seq if turn_seq is not None else self.next_turn_seq(conversation_id)
        existing = self.get(conversation_id, seq)
        if existing is not None:
            raise ValueError(
                f"轮次 {seq} 的 View 已冻结（{existing.id}），不可覆盖。"
                "不可变轮次 View 是「一致性 + 可复现」的前提。"
            )

        if bundle is not None:
            read_refs = read_refs if read_refs is not None else bundle.read_refs()
            resident_tokens = bundle.resident_tokens if resident_tokens is None else resident_tokens
            retrieved_tokens = bundle.retrieved_tokens if retrieved_tokens is None else retrieved_tokens

        view = TurnView(
            id=f"tv_{uuid.uuid4().hex[:10]}",
            conversation_id=conversation_id,
            turn_seq=seq,
            read_refs=read_refs or {},
            written_refs=list(written_refs or []),
            resident_tokens=int(resident_tokens or 0),
            retrieved_tokens=int(retrieved_tokens or 0),
            frozen=True,
            created_at=_now(),
        )
        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO turn_views (id, conversation_id, turn_seq, read_refs, written_refs,"
                " resident_tokens, retrieved_tokens, frozen, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    view.id, view.conversation_id, view.turn_seq,
                    json.dumps(view.read_refs, ensure_ascii=False),
                    json.dumps(view.written_refs, ensure_ascii=False),
                    view.resident_tokens, view.retrieved_tokens, 1, view.created_at,
                ),
            )
        return view

    # ------------------------------------------------------------------
    def get(self, conversation_id: str, turn_seq: int) -> TurnView | None:
        row = self.kb._con.execute(
            "SELECT * FROM turn_views WHERE conversation_id = ? AND turn_seq = ?",
            (conversation_id, turn_seq),
        ).fetchone()
        return _row_to_view(row) if row else None

    def latest(self, conversation_id: str) -> TurnView | None:
        row = self.kb._con.execute(
            "SELECT * FROM turn_views WHERE conversation_id = ? ORDER BY turn_seq DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return _row_to_view(row) if row else None

    def list(self, conversation_id: str, limit: int = 20) -> list[TurnView]:
        rows = self.kb._con.execute(
            "SELECT * FROM turn_views WHERE conversation_id = ? ORDER BY turn_seq DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_row_to_view(r) for r in rows]

    def update_written(self, view_id: str, written_refs: list[str]) -> bool:
        """写入是**追加**语义，不修改既有读视图 —— 因此不违反不可变性。"""
        view = self.kb._con.execute("SELECT * FROM turn_views WHERE id = ?", (view_id,)).fetchone()
        if view is None:
            return False
        merged = list(dict.fromkeys(list(_loads(view["written_refs"], [])) + list(written_refs)))
        with self.kb.tx() as con:
            con.execute(
                "UPDATE turn_views SET written_refs = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), view_id),
            )
        return True

    def stats(self, conversation_id: str) -> dict:
        row = self.kb._con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(resident_tokens),0) r, COALESCE(SUM(retrieved_tokens),0) q"
            " FROM turn_views WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return {"turns": int(row["n"]), "resident_tokens": int(row["r"]), "retrieved_tokens": int(row["q"])}


def _row_to_view(row: Any) -> TurnView:
    d = dict(row)
    return TurnView(
        id=d["id"],
        conversation_id=d["conversation_id"],
        turn_seq=int(d["turn_seq"]),
        read_refs=_loads(d.get("read_refs"), {}),
        written_refs=_loads(d.get("written_refs"), []),
        resident_tokens=int(d.get("resident_tokens") or 0),
        retrieved_tokens=int(d.get("retrieved_tokens") or 0),
        frozen=bool(d.get("frozen", 1)),
        created_at=d.get("created_at") or "",
    )


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def format_views(views: list[TurnView]) -> str:
    if not views:
        return "（暂无轮次视图）"
    return "\n".join(
        f"  轮次 {v.turn_seq:>3}  {v.total_tokens:>6} token  {v.bar_text()}" for v in views
    )
