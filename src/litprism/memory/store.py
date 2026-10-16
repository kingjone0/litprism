"""记忆存储（Store）。

一期只做**显式**记忆 —— 用户主动标记或手动添加。
**不做隐式推断**，理由有三：

1. 隐式记忆容易记错；
2. 用户不知道系统记了什么，破坏透明性；
3. 一旦记错会持续污染后续所有输出，而且很难发现。

隐式推断留到二期，前置条件是「记忆可见、可编辑、可删除」的完整界面已就位。
"""

from __future__ import annotations

import difflib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..refs import safe_ref  # noqa: F401  (保持包内导入一致性)
from .guards import assert_memory_preference_only

MEMORY_KINDS = ("research_interest", "term_preference", "style_preference", "misc")

KIND_LABELS = {
    "research_interest": "研究方向",
    "term_preference": "术语偏好",
    "style_preference": "表达偏好",
    "misc": "其他偏好",
}

# 去重阈值：归一化后相似度超过该值即视为同一条
DUP_THRESHOLD = 0.86


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:10]}"


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


@dataclass
class Memory:
    id: str
    kind: str
    value: str
    key: str | None = None
    source: str = "explicit"
    confidence: float | None = None
    pinned: bool = False
    priority: int = 100
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def render(self) -> str:
        key = f"{self.key}：" if self.key else ""
        return f"[{self.label}] {key}{self.value}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
            "pinned": self.pinned,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
        }


@dataclass
class WriteReceipt:
    """写入回执 —— 说明这条记忆最终去了哪里。"""

    action: str                 # created | updated | merged | rejected
    memory_id: str = ""
    reason: str = ""
    merged_from: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return {
            "created": f"已新增记忆 {self.memory_id}",
            "updated": f"已更新记忆 {self.memory_id}（{self.reason}）",
            "merged": f"已合并进记忆 {self.memory_id}（合并了 {len(self.merged_from)} 条）",
            "rejected": f"未写入：{self.reason}",
        }.get(self.action, self.action)


class MemoryStore:
    """记忆的读写。所有写入都走 :meth:`save` 的去重→蒸馏→归类→回执流程。"""

    def __init__(self, kb: Any, cfg: Any = None):
        self.kb = kb
        self.cfg = cfg.memory if hasattr(cfg, "memory") else cfg

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, memory_id: str) -> Memory | None:
        row = self.kb._con.execute(
            "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL", (memory_id,)
        ).fetchone()
        return _row_to_memory(row) if row else None

    def list(
        self,
        *,
        kind: str | None = None,
        pinned: bool | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE 1=1"
        vals: list[Any] = []
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if kind:
            sql += " AND kind = ?"
            vals.append(kind)
        if pinned is not None:
            sql += " AND pinned = ?"
            vals.append(1 if pinned else 0)
        sql += " ORDER BY priority ASC, updated_at DESC LIMIT ?"
        vals.append(limit)
        return [_row_to_memory(r) for r in self.kb._con.execute(sql, vals).fetchall()]

    def resident(self, limit: int | None = None) -> list[Memory]:
        """常驻层：pinned 的偏好记忆。"""
        out = self.list(pinned=True, limit=limit or 999)
        return out

    def search(self, query: str, *, limit: int = 10) -> list[Memory]:
        """按需层检索（先搜后读）。"""
        q = _norm(query)
        if not q:
            return []
        scored: list[tuple[float, Memory]] = []
        for m in self.list(limit=1000):
            hay = _norm(f"{m.key or ''} {m.value}")
            if q in hay:
                scored.append((1.0, m))
                continue
            r = difflib.SequenceMatcher(None, q, hay).ratio()
            if r >= 0.35:
                scored.append((r, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:limit]]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def save(
        self,
        value: str,
        *,
        kind: str = "misc",
        key: str | None = None,
        pinned: bool = True,
        priority: int = 100,
        source: str = "explicit",
        distill: Any = None,
    ) -> WriteReceipt:
        """显式写入一条记忆。流程：校验 → 蒸馏 → 去重 → 归类 → 落库 → 回执。"""
        if source != "explicit" and not (self.cfg and getattr(self.cfg, "allow_implicit", False)):
            return WriteReceipt(
                action="rejected",
                reason="一期只支持显式记忆（用户主动标记）。隐式推断需先有完整的可见/可编辑界面。",
            )

        try:
            assert_memory_preference_only(kind)
        except ValueError as e:
            return WriteReceipt(action="rejected", reason=str(e))

        text = (value or "").strip()
        if not text:
            return WriteReceipt(action="rejected", reason="内容为空")

        # 蒸馏：可选，把口语化描述压成一句偏好
        if distill is not None:
            try:
                distilled = (distill(text) or "").strip()
                if distilled:
                    text = distilled
            except Exception:  # noqa: BLE001 - 蒸馏失败不该阻断写入
                pass

        # 去重
        dup = self._find_duplicate(kind, key, text)
        if dup is not None:
            if _norm(text) == _norm(dup.value):
                self.touch(dup.id)
                return WriteReceipt(
                    action="updated", memory_id=dup.id, reason="内容相同，仅刷新使用时间"
                )
            merged = self._merge(dup, text)
            return WriteReceipt(
                action="merged", memory_id=dup.id, reason="与已有记忆相似，已合并",
                merged_from=[dup.id],
            )

        mid = _new_id()
        now = _now()
        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO memories (id, kind, key, value, source, confidence, pinned,"
                " priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, kind, key, text, source, 1.0 if source == "explicit" else None,
                 1 if pinned else 0, int(priority), now, now),
            )
        return WriteReceipt(action="created", memory_id=mid)

    def update(self, memory_id: str, **fields: Any) -> bool:
        allowed = {"kind", "key", "value", "pinned", "priority", "confidence"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "kind":
                assert_memory_preference_only(str(v))
            if k == "pinned":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(memory_id)
        with self.kb.tx() as con:
            cur = con.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", vals)
        return cur.rowcount > 0

    def delete(self, memory_id: str, *, hard: bool = False) -> bool:
        with self.kb.tx() as con:
            if hard:
                cur = con.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            else:
                cur = con.execute(
                    "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (_now(), _now(), memory_id),
                )
        return cur.rowcount > 0

    def restore(self, memory_id: str) -> bool:
        with self.kb.tx() as con:
            cur = con.execute(
                "UPDATE memories SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), memory_id),
            )
        return cur.rowcount > 0

    def touch(self, memory_id: str) -> None:
        try:
            with self.kb.tx() as con:
                con.execute("UPDATE memories SET last_used_at = ? WHERE id = ?", (_now(), memory_id))
        except Exception:  # noqa: BLE001
            pass

    def clear(self, *, kind: str | None = None, hard: bool = True) -> int:
        """清空（主要用于测试与用户主动重置）。"""
        with self.kb.tx() as con:
            if kind:
                cur = con.execute("DELETE FROM memories WHERE kind = ?", (kind,))
            else:
                cur = con.execute("DELETE FROM memories")
        return cur.rowcount

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _find_duplicate(self, kind: str, key: str | None, value: str) -> Memory | None:
        nv = _norm(value)
        if not nv:
            return None
        for m in self.list(kind=kind, limit=1000):
            if key and m.key and _norm(m.key) == _norm(key):
                return m
            if _norm(m.value) == nv:
                return m
            if difflib.SequenceMatcher(None, nv, _norm(m.value)).ratio() >= DUP_THRESHOLD:
                return m
        return None

    def _merge(self, existing: Memory, incoming: str) -> Memory:
        """合并：保留更长的表述（信息量更大），除非新的是对旧的修正。"""
        keep = incoming if len(incoming) > len(existing.value) else existing.value
        self.update(existing.id, value=keep)
        updated = self.get(existing.id)
        return updated or existing

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        rows = self.kb._con.execute(
            "SELECT kind, COUNT(*) n FROM memories WHERE deleted_at IS NULL GROUP BY kind"
        ).fetchall()
        by_kind = {r["kind"]: r["n"] for r in rows}
        total = sum(by_kind.values())
        pinned = self.kb._con.execute(
            "SELECT COUNT(*) n FROM memories WHERE deleted_at IS NULL AND pinned = 1"
        ).fetchone()["n"]
        return {
            "total": total,
            "pinned": pinned,
            "by_kind": by_kind,
        }


def _row_to_memory(row: Any) -> Memory:
    d = dict(row)
    return Memory(
        id=d["id"],
        kind=d["kind"],
        value=d["value"],
        key=d.get("key"),
        source=d.get("source") or "explicit",
        confidence=d.get("confidence"),
        pinned=bool(d.get("pinned")),
        priority=int(d.get("priority") or 100),
        created_at=d.get("created_at") or "",
        updated_at=d.get("updated_at") or "",
        last_used_at=d.get("last_used_at"),
    )


def format_memories(items: Iterable[Memory]) -> str:
    items = list(items)
    if not items:
        return "（暂无记忆）"
    return "\n".join(f"  {'★' if m.pinned else '·'} {m.id}  {m.render()}" for m in items)
