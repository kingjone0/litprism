"""MCP 调用审计。

每次工具调用都记一条 —— 谁在什么时候调了什么、成功与否。
参数**不存明文**，只存一个既可读又不泄露内容的摘要（键名 + 截断值 + 指纹）。

为什么审计是必需的
------------------
MCP 让外部客户端能读写知识库。没有审计，出了问题是查不出来的 ——
"这条未核验的文献是谁导进来的"必须能回答。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def digest_args(args: dict | None, *, max_value_len: int = 24) -> str:
    """把参数压成可读摘要：``query=注意力…,limit=10 #a3f2b1``。

    保留键名与截断值便于排查，同时附指纹保证可比对但不泄露全文。
    """
    if not args:
        return "-"
    parts = []
    for k in sorted(args):
        v = args[k]
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        if len(s) > max_value_len:
            s = s[:max_value_len] + "…"
        parts.append(f"{k}={s}")
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return f"{','.join(parts)} #{fp}"


class AuditLog:
    """MCP 调用审计。"""

    def __init__(self, kb: Any, server_id: str = "local"):
        self.kb = kb
        self.server_id = server_id

    def record(
        self,
        tool: str,
        args: dict | None,
        *,
        ok: bool,
        error: str | None = None,
    ) -> str:
        rid = f"ma_{uuid.uuid4().hex[:10]}"
        try:
            with self.kb.tx() as con:
                con.execute(
                    "INSERT INTO mcp_audit (id, server_id, tool, args_digest, ok, error, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rid, self.server_id, tool, digest_args(args), 1 if ok else 0,
                     (error or "")[:1000] or None, _now()),
                )
        except Exception:  # noqa: BLE001 - 审计失败不该阻断调用
            pass
        return rid

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.kb._con.execute(
            "SELECT * FROM mcp_audit ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        row = self.kb._con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok FROM mcp_audit"
        ).fetchone()
        by_tool = {
            r["tool"]: r["n"]
            for r in self.kb._con.execute(
                "SELECT tool, COUNT(*) n FROM mcp_audit GROUP BY tool ORDER BY n DESC"
            ).fetchall()
        }
        return {
            "total": int(row["n"]),
            "ok": int(row["ok"]),
            "failed": int(row["n"]) - int(row["ok"]),
            "by_tool": by_tool,
        }

    def clear(self) -> int:
        with self.kb.tx() as con:
            cur = con.execute("DELETE FROM mcp_audit")
        return cur.rowcount


def format_audit(entries: list[dict]) -> str:
    if not entries:
        return "（暂无调用记录）"
    lines = []
    for e in entries:
        mark = "✓" if e.get("ok") else "✗"
        lines.append(
            f"  {mark} {e.get('created_at', '')[:19]}  {e.get('tool', ''):<18} {e.get('args_digest') or '-'}"
        )
        if e.get("error"):
            lines.append(f"      {str(e['error'])[:120]}")
    return "\n".join(lines)
