"""交付物管理。

交付物是「一次研究的产出物」—— 综述草稿、对比报告、阅读笔记、审稿意见。
与普通对话回答的区别：它有**引用清单**、有**状态**、可**导出**。

关键约束
--------
交付物的 ``citations`` 记录每条引用对应的 ``paper_id`` / ``page`` / ``chunk_id``，
导出时一并带出。**没有引用清单的交付物是不可追溯的** ——
读者看到一句结论却不知道它来自哪一页，这比不写更糟。

状态流转
--------
``draft`` → ``final`` → ``exported``
一期不做强制的状态机（允许任意跳转），但会在列表里显示状态，
便于用户区分"还在改"和"已经定稿"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TYPE_LABELS = {
    "review": "文献综述",
    "comparison": "对比报告",
    "notes": "阅读笔记",
    "peer_review": "审稿意见",
}

STATUS_LABELS = {
    "draft": "草稿",
    "final": "定稿",
    "exported": "已导出",
}

DELIVERABLE_TYPES = tuple(TYPE_LABELS)
STATUSES = tuple(STATUS_LABELS)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Deliverable:
    id: str
    type: str
    title: str
    content: str = ""
    skill_id: str | None = None
    source_scope: dict = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    status: str = "draft"
    model: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def citation_count(self) -> int:
        return len(self.citations)

    @property
    def word_count(self) -> int:
        return len(self.content or "")

    def citation_summary(self) -> str:
        """把引用清单压成一行可读摘要，用于导出时的"来源"段。"""
        if not self.citations:
            return "（无引用）"
        lines = []
        for i, c in enumerate(self.citations, 1):
            bits = []
            if c.get("paper_title"):
                bits.append(str(c["paper_title"]))
            elif c.get("paper_id"):
                bits.append(str(c["paper_id"]))
            if c.get("page") is not None:
                bits.append(f"p.{c['page']}")
            if c.get("tag"):
                bits.append(f"[{c['tag']}]")
            if c.get("chunk_id"):
                bits.append(f"chunk:{c['chunk_id']}")
            lines.append(f"{i}. " + " · ".join(bits))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "type_label": self.type_label,
            "title": self.title,
            "status": self.status,
            "status_label": self.status_label,
            "skill_id": self.skill_id,
            "source_scope": self.source_scope,
            "citations": self.citations,
            "citation_count": self.citation_count,
            "word_count": self.word_count,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DeliverableStore:
    """交付物的读写与导出。"""

    def __init__(self, kb: Any):
        self.kb = kb

    # ------------------------------------------------------------------
    def add(
        self,
        *,
        type: str,
        title: str,
        content: str,
        skill_id: str | None = None,
        source_scope: dict | None = None,
        citations: list[dict] | None = None,
        status: str = "draft",
        model: str | None = None,
        deliverable_id: str | None = None,
    ) -> str:
        if type not in DELIVERABLE_TYPES:
            raise ValueError(f"未知交付物类型 {type!r}。可用：{list(DELIVERABLE_TYPES)}")
        if status not in STATUSES:
            raise ValueError(f"未知状态 {status!r}。可用：{list(STATUSES)}")
        if not (title or "").strip():
            raise ValueError("标题不能为空")

        did = deliverable_id or f"del_{uuid.uuid4().hex[:10]}"
        now = _now()
        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO deliverables (id, type, title, skill_id, source_scope, content,"
                " citations, status, model, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did, type, title.strip(), skill_id,
                    json.dumps(source_scope or {}, ensure_ascii=False),
                    content or "",
                    json.dumps(citations or [], ensure_ascii=False),
                    status, model, now, now,
                ),
            )
        return did

    # ------------------------------------------------------------------
    def get(self, did: str) -> Deliverable | None:
        row = self.kb._con.execute(
            "SELECT * FROM deliverables WHERE id = ? AND deleted_at IS NULL", (did,)
        ).fetchone()
        return _row_to_deliverable(row) if row else None

    def resolve(self, ref: str) -> Deliverable | None:
        """按 id / id 前缀 / 标题片段定位；为空时取最近一个。"""
        ref = (ref or "").strip()
        if not ref:
            items = self.list(limit=1)
            return items[0] if items else None
        d = self.get(ref)
        if d:
            return d
        rows = self.kb._con.execute(
            "SELECT * FROM deliverables WHERE id LIKE ? AND deleted_at IS NULL"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 2",
            (f"{ref}%",),
        ).fetchall()
        if len(rows) == 1:
            return _row_to_deliverable(rows[0])
        row = self.kb._con.execute(
            "SELECT * FROM deliverables WHERE title LIKE ? AND deleted_at IS NULL"
            " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (f"%{ref}%",),
        ).fetchone()
        return _row_to_deliverable(row) if row else None

    def list(
        self,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Deliverable]:
        sql = "SELECT * FROM deliverables WHERE deleted_at IS NULL"
        vals: list[Any] = []
        if type:
            sql += " AND type = ?"
            vals.append(type)
        if status:
            sql += " AND status = ?"
            vals.append(status)
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        vals.append(limit)
        return [_row_to_deliverable(r) for r in self.kb._con.execute(sql, vals).fetchall()]

    # ------------------------------------------------------------------
    def update(self, did: str, **fields: Any) -> bool:
        allowed = {"title", "content", "status", "type", "citations", "source_scope"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "status" and v not in STATUSES:
                raise ValueError(f"未知状态 {v!r}")
            if k == "type" and v not in DELIVERABLE_TYPES:
                raise ValueError(f"未知类型 {v!r}")
            if k in ("citations", "source_scope"):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        vals.extend([_now(), did])
        with self.kb.tx() as con:
            cur = con.execute(f"UPDATE deliverables SET {', '.join(sets)} WHERE id = ?", vals)
        return cur.rowcount > 0

    def delete(self, did: str, *, hard: bool = False) -> bool:
        with self.kb.tx() as con:
            if hard:
                cur = con.execute("DELETE FROM deliverables WHERE id = ?", (did,))
            else:
                cur = con.execute(
                    "UPDATE deliverables SET deleted_at = ?, updated_at = ?"
                    " WHERE id = ? AND deleted_at IS NULL",
                    (_now(), _now(), did),
                )
        return cur.rowcount > 0

    def restore(self, did: str) -> bool:
        with self.kb.tx() as con:
            cur = con.execute(
                "UPDATE deliverables SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), did),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def render_markdown(self, d: Deliverable) -> str:
        front = [
            "---",
            f"title: {json.dumps(d.title, ensure_ascii=False)}",
            f"type: {d.type}",
            f"status: {d.status}",
        ]
        if d.skill_id:
            front.append(f"skill: {d.skill_id}")
        if d.model:
            front.append(f"model: {d.model}")
        front.append(f"created: {d.created_at}")
        front.append(f"updated: {d.updated_at}")
        front.append(f"citations: {d.citation_count}")
        front.append("---")

        body = [f"# {d.title}", "", d.content.strip() or "（空）"]
        if d.citations:
            body += ["", "## 引用来源", "", d.citation_summary()]
        return "\n".join(front + [""] + body) + "\n"

    def export(self, did: str, path: str | Path, *, fmt: str = "md") -> Path:
        d = self.get(did)
        if d is None:
            raise ValueError(f"交付物 {did} 不存在")
        self.validate_export(d)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            p.write_text(
                json.dumps(d.to_dict() | {"content": d.content}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            p.write_text(self.render_markdown(d), encoding="utf-8")
        # 导出后把状态推到 exported（除非已是 final —— 定稿状态更"重"，不降级）
        if d.status == "draft":
            self.update(did, status="exported")
        return p

    def validate_export(self, d: Deliverable) -> None:
        from .evidence import citation_issues, stored_excerpts
        if not isinstance(d.source_scope, dict):
            raise ValueError('引用校验记录格式错误。')
        if not isinstance(d.citations, list) or not d.citations:
            raise ValueError("没有引用来源，禁止导出学术交付物。")
        excerpts = stored_excerpts(self.kb, d.citations)
        issues = citation_issues(d.content, excerpts, required=False)
        if (d.source_scope.get('citation_check') or {}).get('passed') is False:
            issues.append('生成时的引用校验未通过。')
        if issues:
            raise ValueError('引用校验未通过：' + '；'.join(issues))
        d.citations = [{'tag': e['tag'], 'chunk_id': e['id'], 'paper_id': e['paper_id'],
                        'paper_title': e['paper_title'], 'page': e['page']} for e in excerpts]

    def export_all(self, out_dir: str | Path, *, fmt: str = "md") -> list[Path]:
        for d in self.list(limit=1000):
            self.validate_export(d)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for d in self.list(limit=1000):
            safe = _safe_filename(d.title)
            p = out / f"{safe}.{fmt if fmt != 'md' else 'md'}"
            paths.append(self.export(d.id, p, fmt=fmt))
        return paths

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        rows = self.kb._con.execute(
            "SELECT type, status, COUNT(*) n FROM deliverables WHERE deleted_at IS NULL"
            " GROUP BY type, status"
        ).fetchall()
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for r in rows:
            by_type[r["type"]] = by_type.get(r["type"], 0) + r["n"]
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        total = sum(by_type.values())
        cites = self.kb._con.execute(
            "SELECT COALESCE(SUM(LENGTH(citations) - LENGTH(REPLACE(citations, 'chunk_id', ''))), 0) n"
            " FROM deliverables WHERE deleted_at IS NULL"
        ).fetchone()
        return {
            "total": total,
            "by_type": by_type,
            "by_status": by_status,
            "citation_mentions": int(cites["n"]) // len("chunk_id") if cites else 0,
        }


def _row_to_deliverable(row: Any) -> Deliverable:
    d = dict(row)
    return Deliverable(
        id=d["id"],
        type=d["type"],
        title=d["title"],
        content=d.get("content") or "",
        skill_id=d.get("skill_id"),
        source_scope=_loads(d.get("source_scope"), {}),
        citations=_loads(d.get("citations"), []),
        status=d.get("status") or "draft",
        model=d.get("model"),
        created_at=d.get("created_at") or "",
        updated_at=d.get("updated_at") or "",
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


def _safe_filename(title: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in title).strip()
    return (out or "untitled")[:80]


def format_deliverables(items: list[Deliverable]) -> str:
    if not items:
        return "（暂无交付物）"
    lines = []
    for d in items:
        lines.append(
            f"  [{d.id}] {d.status_label:<4} {d.type_label:<6} {d.title[:48]}"
        )
        meta = f"{d.word_count} 字"
        if d.citation_count:
            meta += f" · {d.citation_count} 条引用"
        if d.skill_id:
            meta += f" · Skill {d.skill_id}"
        lines.append(f"      {meta} · {d.updated_at[:19]}")
    return "\n".join(lines)
