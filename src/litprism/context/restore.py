"""可逆性（Restore）—— 压缩不能是单向门。

LitPrism 的结构性优势
---------------------
参考文档把可逆性分三级：

* L0 不可逆 —— 摘要是终态
* L1 可寻址还原 —— 按块/按区间还原，可还原到文件
* L2 内容寻址 —— 超大内容换占位符，按 id 取回全文

通用 agent 要额外建一套内容寻址存储才能拿到 L2。
**我们免费就有** —— 论文原文本来就在 SQLite 的 ``chunks`` 表里，而且有稳定 ``chunk_id``。
所以压缩时**根本不需要把原文塞进摘要**，直接记句柄即可，要看时一条 SQL 取回。

**原文从未真正离开系统。** 这是 LitPrism 相对通用 agent 的结构性优势。

别指望模型自发恢复
------------------
参考文档实测：模型不会自己想到调用恢复工具。所以要么在指令里强制
（如"综述定稿前先还原核对"），要么不承诺。**一期不承诺自动恢复**，
只提供人工入口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config, ContextConfig
from ..refs import parse_ref, safe_ref
from .meter import Meter
from .summarizer import parse_summary


@dataclass
class RestoreResult:
    ok: bool
    block_id: str = ""
    topic: str = ""
    summary: str = ""
    messages: list[dict] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    missing_chunks: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def restored_tokens(self) -> int:
        m = Meter()
        return sum(m.estimate_message(x) for x in self.messages) + sum(
            m.estimate_text(c.get("text")) for c in self.citations
        )

    def describe(self) -> str:
        if not self.ok:
            return f"还原失败：{self.error}"
        parts = [f"还原 {len(self.messages)} 条消息"]
        if self.citations:
            parts.append(f"{len(self.citations)} 段原文")
        if self.missing_chunks:
            parts.append(f"⚠️ {len(self.missing_chunks)} 个句柄已失效")
        return "，".join(parts)


class Restorer:
    """按块还原。"""

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
    def list_blocks(self, conversation_id: str, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM compaction_blocks WHERE conversation_id = ?"
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY created_at"
        rows = self.kb._con.execute(sql, (conversation_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["direct_refs"] = _loads(d.get("direct_refs"), [])
            d["citation_handles"] = _loads(d.get("citation_handles"), [])
            d["summary"] = parse_summary(d.get("summary") or "")
            out.append(d)
        return out

    def restore_block(self, block_id: str, *, with_citations: bool = True) -> RestoreResult:
        """按块还原：取回被折叠区间的消息，以及引用句柄对应的原文。"""
        row = self.kb._con.execute("SELECT * FROM compaction_blocks WHERE id = ?", (block_id,)).fetchone()
        if row is None:
            return RestoreResult(ok=False, block_id=block_id, error=f"压缩块 {block_id} 不存在")

        block = dict(row)
        refs: list[str] = _loads(block.get("direct_refs"), [])
        seqs = sorted(parse_ref(r) for r in refs) if refs else []

        messages: list[dict] = []
        if seqs:
            marks = ",".join("?" * len(seqs))
            rows = self.kb._con.execute(
                f"SELECT seq, ref, role, content, citations, kind, created_at FROM messages"
                f" WHERE conversation_id = ? AND seq IN ({marks}) ORDER BY seq",
                [block["conversation_id"], *seqs],
            ).fetchall()
            messages = [dict(r) for r in rows]

        citations: list[dict] = []
        missing: list[str] = []
        if with_citations:
            handles = _loads(block.get("citation_handles"), [])
            for h in handles:
                cid = h.get("chunk_id") if isinstance(h, dict) else None
                if not cid:
                    continue
                chunk = self.fetch_chunk(cid)
                if chunk is None:
                    missing.append(cid)
                else:
                    citations.append(chunk)

        return RestoreResult(
            ok=True,
            block_id=block_id,
            topic=block.get("topic") or "",
            summary=block.get("summary") or "",
            messages=messages,
            citations=citations,
            missing_chunks=missing,
        )

    # ------------------------------------------------------------------
    def fetch_chunk(self, chunk_id: str) -> dict | None:
        """**内容寻址取回** —— 我们的 ``chunks`` 表就是内容寻址存储。"""
        row = self.kb._con.execute(
            "SELECT c.id AS chunk_id, c.paper_id, c.page, c.section, c.text, p.title AS paper_title"
            " FROM chunks c LEFT JOIN papers p ON p.id = c.paper_id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row else None

    def fetch_chunks(self, chunk_ids: list[str]) -> list[dict]:
        out = []
        for cid in chunk_ids:
            c = self.fetch_chunk(cid)
            if c:
                out.append(c)
        return out

    # ------------------------------------------------------------------
    def render(self, result: RestoreResult, *, include_citations: bool = True) -> str:
        """把还原结果渲染成可读文本。"""
        if not result.ok:
            return f"[还原失败] {result.error}"
        lines = [f"# 压缩块还原 · {result.block_id}"]
        if result.topic:
            lines.append(f"> {result.topic}")
        lines.append("")
        lines.append("## 摘要")
        lines.append(result.summary or "（无）")
        lines.append("")
        lines.append(f"## 被折叠的原始消息（{len(result.messages)} 条）")
        for m in result.messages:
            role = {"user": "用户", "assistant": "助手", "system": "系统"}.get(m.get("role", ""), "其他")
            lines.append(f"### [{m.get('ref')}] {role}")
            lines.append(m.get("content") or "")
            lines.append("")
        if include_citations and result.citations:
            lines.append(f"## 引用的原文（{len(result.citations)} 段）")
            for c in result.citations:
                head = f"### {c.get('paper_title') or c.get('paper_id')} · p.{c.get('page')}"
                if c.get("section"):
                    head += f" · {c['section']}"
                lines.append(head)
                lines.append(f"`chunk_id: {c.get('chunk_id')}`")
                lines.append("")
                lines.append(c.get("text") or "")
                lines.append("")
        if result.missing_chunks:
            lines.append("## ⚠️ 失效的句柄")
            lines.append(
                "以下 chunk_id 在知识库中已不存在（论文可能被删除）："
                + "、".join(f"`{x}`" for x in result.missing_chunks)
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def restore_to_file(
        self, block_id: str, path: str | Path, *, include_citations: bool = True
    ) -> Path:
        """**还原到文件** —— 把全保真原文写盘，而不是灌回上下文。

        参考文档里最巧的一招：用户要看细节时，写文件比回填上下文更实用 ——
        既不炸上下文窗口，也不作废前缀缓存。而 LitPrism 本来就有导出能力，天然契合。
        """
        result = self.restore_block(block_id, with_citations=include_citations)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.render(result, include_citations=include_citations), encoding="utf-8")
        return p

    # ------------------------------------------------------------------
    def stats(self, conversation_id: str) -> dict:
        """压缩块统计，供状态面板展示。"""
        blocks = self.list_blocks(conversation_id)
        active = [b for b in blocks if int(b.get("active", 1)) == 1]
        return {
            "blocks": len(blocks),
            "active_blocks": len(active),
            "shadowed_tokens": sum(int(b.get("shadowed_token_count") or 0) for b in active),
            "summary_tokens": sum(int(b.get("summary_token_count") or 0) for b in active),
            "citation_handles": sum(len(b.get("citation_handles") or []) for b in blocks),
            "items": [
                {
                    "id": b["id"],
                    "topic": b.get("topic") or "",
                    "range": _range_text(b.get("direct_refs") or []),
                    "shadowed": int(b.get("shadowed_token_count") or 0),
                    "summary": int(b.get("summary_token_count") or 0),
                    "age": b.get("survived_count") or 0,
                    "active": bool(int(b.get("active", 1))),
                    "handles": len(b.get("citation_handles") or []),
                }
                for b in blocks
            ],
        }


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def _range_text(refs: list[str]) -> str:
    if not refs:
        return "—"
    try:
        return f"{refs[0]}-{refs[-1]}"
    except Exception:  # noqa: BLE001
        return "—"


def format_restore(result: RestoreResult) -> str:
    lines = [result.describe()]
    if result.missing_chunks:
        lines.append(f"  失效句柄：{', '.join(result.missing_chunks)}")
    return "\n".join(lines)


__all__ = ["Restorer", "RestoreResult", "format_restore", "safe_ref"]
