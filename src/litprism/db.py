"""SQLite 本地知识库。

表结构
------
papers        论文元数据 + 核验状态 + 全文
chunks        正文分块（含页码、章节），供检索与引用定位
chunks_fts    FTS5 全文索引（对 CJK 逐字分词，支持中文 BM25 检索）
analyses      结构化解读结果（研究问题/方法/实验/结论/创新点/局限）
concepts      专业术语及其在文中的解释
relations     跨论文关联（相似度、关系类型）
verify_log    核验审计日志（每一次核验都可追溯，防幻觉可审计）
meta          键值杂项
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Config
from . import schema as _schema
from .refs import make_ref
from .textutil import build_fts_query, cjk_segment, paper_fingerprint, truncate
from .whitelist import badge

# Schema 定义与迁移已抽到 schema.py —— 单一事实来源
SCHEMA = _schema.SCHEMA_V1          # 兼容旧引用；新代码请直接用 schema.py


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _j(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


class KnowledgeBase:
    """本地论文知识库。线程内复用同一个连接。"""

    def __init__(self, cfg: Config | None = None, db_path: str | Path | None = None):
        self.cfg = cfg or Config()
        self.cfg.ensure_dirs()
        self.db_path = Path(db_path) if db_path else self.cfg.db_path
        self._con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self.migrations_applied = _schema.init_schema(self._con)

    # ------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._con
            self._con.commit()
        except Exception:
            self._con.rollback()
            raise

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 论文
    # ------------------------------------------------------------------
    def add_paper(
        self,
        *,
        title: str,
        authors: Iterable[str] | None = None,
        year: int | None = None,
        venue: str | None = None,
        abstract: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        url: str | None = None,
        source: str = "local_pdf",
        origin: str | None = None,
        full_text: str | None = None,
        tags: Iterable[str] | None = None,
        verify_status: str = "unverified",
        verify_level: str = "unknown",
        verify_confidence: float = 0.0,
        verify_evidence: dict | None = None,
        paper_id: str | None = None,
    ) -> str:
        """新增论文；若 DOI 或 标题+年份 指纹已存在则返回已有 id（去重）。"""
        existing = self.find_duplicate(doi=doi, title=title, year=year)
        if existing:
            self.update_paper(
                existing,
                verify_status=verify_status,
                verify_level=verify_level,
                verify_confidence=verify_confidence,
                verify_evidence=verify_evidence,
                abstract=abstract,
                venue=venue,
                url=url,
                arxiv_id=arxiv_id,
            )
            return existing

        pid = paper_id or _new_id("p_")
        now = _now()
        with self.tx() as con:
            con.execute(
                """INSERT INTO papers (id, doi, arxiv_id, title, authors, year, venue, abstract, url,
                       source, origin, verify_status, verify_level, verify_confidence, verify_evidence,
                       verified_at, tags, full_text, fingerprint, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, doi, arxiv_id, title.strip(),
                    json.dumps(list(authors or []), ensure_ascii=False),
                    year, venue, abstract, url, source, origin,
                    verify_status, verify_level, float(verify_confidence),
                    json.dumps(verify_evidence or {}, ensure_ascii=False),
                    now if verify_status == "verified" else None,
                    json.dumps(list(tags or []), ensure_ascii=False),
                    full_text, paper_fingerprint(title, year), now, now,
                ),
            )
        return pid

    def find_duplicate(self, *, doi: str | None, title: str, year: int | None) -> str | None:
        if doi:
            row = self._con.execute("SELECT id FROM papers WHERE doi = ?", (doi,)).fetchone()
            if row:
                return row["id"]
        fp = paper_fingerprint(title, year)
        row = self._con.execute("SELECT id FROM papers WHERE fingerprint = ?", (fp,)).fetchone()
        if row:
            return row["id"]
        if year is None:
            row = self._con.execute(
                "SELECT id FROM papers WHERE fingerprint = ?", (paper_fingerprint(title, None),)
            ).fetchone()
            if row:
                return row["id"]
        return None

    def update_paper(self, paper_id: str, **fields: Any) -> None:
        allowed = {
            "title", "authors", "year", "venue", "abstract", "doi", "arxiv_id", "url",
            "source", "origin", "verify_status", "verify_level", "verify_confidence",
            "verify_evidence", "tags", "note", "full_text", "fingerprint",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k in ("authors", "tags", "verify_evidence"):
                v = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            if k == "verify_status" and v == "verified":
                sets.append("verified_at = ?")
                vals.append(_now())
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        sets.append("updated_at = ?")
        vals.append(_now())
        vals.append(paper_id)
        with self.tx() as con:
            con.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", vals)

    def get_paper(self, paper_id: str) -> dict | None:
        row = self._con.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return self._row_to_paper(row) if row else None

    def resolve_paper(self, ref: str) -> dict | None:
        """按 id / id 前缀 / DOI / 标题模糊匹配定位一篇论文。"""
        ref = (ref or "").strip()
        if not ref:
            return None
        p = self.get_paper(ref)
        if p:
            return p
        rows = self._con.execute(
            "SELECT * FROM papers WHERE id LIKE ? LIMIT 5", (f"{ref}%",)
        ).fetchall()
        if len(rows) == 1:
            return self._row_to_paper(rows[0])
        row = self._con.execute(
            "SELECT * FROM papers WHERE lower(doi) = lower(?) LIMIT 1", (ref,)
        ).fetchone()
        if row:
            return self._row_to_paper(row)
        row = self._con.execute(
            "SELECT * FROM papers WHERE title LIKE ? LIMIT 1", (f"%{ref}%",)
        ).fetchone()
        return self._row_to_paper(row) if row else None

    def list_papers(
        self, *, status: str | None = None, query: str | None = None, limit: int = 200
    ) -> list[dict]:
        sql = "SELECT * FROM papers"
        conds, vals = [], []
        if status:
            conds.append("verify_status = ?")
            vals.append(status)
        if query:
            conds.append("(title LIKE ? OR abstract LIKE ? OR tags LIKE ?)")
            vals += [f"%{query}%"] * 3
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        vals.append(limit)
        return [self._row_to_paper(r) for r in self._con.execute(sql, vals).fetchall()]

    def rename_paper(self, paper_id: str, display_title: str) -> bool:
        """改**显示名**。

        ⚠️ 只改 ``display_title``，绝不动 ``title``。原始题录标题用于
        核验比对、生成引用、导出 BibTeX —— 改了它会让真实论文被判成
        ``not_found``，还会让导出的 BibTeX 带上随手起的名字。
        """
        dt = (display_title or "").strip()
        row = self._con.execute(
            "SELECT title FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        if row is None:
            return False
        if not dt or dt == row["title"]:
            dt = None   # 与原名相同或为空 → 恢复用原始标题
        with self.tx() as con:
            con.execute(
                "UPDATE papers SET display_title = ?, updated_at = ? WHERE id = ?",
                (dt, _now(), paper_id),
            )
        return True

    def delete_paper(self, paper_id: str) -> bool:
        rows = self._con.execute("SELECT rowid FROM chunks WHERE paper_id = ?", (paper_id,)).fetchall()
        with self.tx() as con:
            for r in rows:
                con.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["rowid"],))
            con.execute("DELETE FROM chunks WHERE paper_id = ?", (paper_id,))
            con.execute("DELETE FROM analyses WHERE paper_id = ?", (paper_id,))
            con.execute("DELETE FROM concepts WHERE paper_id = ?", (paper_id,))
            con.execute("DELETE FROM relations WHERE paper_a = ? OR paper_b = ?", (paper_id, paper_id))
            cur = con.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        return cur.rowcount > 0

    @staticmethod
    def _row_to_paper(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["authors"] = _j(d.get("authors"), [])
        d["tags"] = _j(d.get("tags"), [])
        d["verify_evidence"] = _j(d.get("verify_evidence"), {})
        return d

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------
    def add_chunks(self, paper_id: str, chunks: list[dict]) -> int:
        """chunks: [{text, page, section}]"""
        with self.tx() as con:
            for c in chunks:
                text = (c.get("text") or "").strip()
                if not text:
                    continue
                cid = c.get("id") or _new_id("c_")
                cur = con.execute(
                    "INSERT OR IGNORE INTO chunks (id, paper_id, page, section, text) VALUES (?,?,?,?,?)",
                    (cid, paper_id, c.get("page"), c.get("section"), text),
                )
                if cur.rowcount:
                    con.execute(
                        "INSERT INTO chunks_fts (rowid, seg_text) VALUES (?, ?)",
                        (cur.lastrowid, cjk_segment(text)),
                    )
        return self.count_chunks(paper_id)

    def count_chunks(self, paper_id: str | None = None) -> int:
        if paper_id:
            row = self._con.execute("SELECT COUNT(*) n FROM chunks WHERE paper_id = ?", (paper_id,)).fetchone()
        else:
            row = self._con.execute("SELECT COUNT(*) n FROM chunks").fetchone()
        return int(row["n"])

    def get_chunks(self, paper_id: str, limit: int | None = None) -> list[dict]:
        sql = "SELECT id, paper_id, page, section, text FROM chunks WHERE paper_id = ? ORDER BY rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._con.execute(sql, (paper_id,)).fetchall()]

    def search_chunks(
        self, query: str, *, paper_ids: list[str] | None = None, top_k: int = 8
    ) -> list[dict]:
        """单条查询的 BM25 检索。"""
        match = build_fts_query(query)
        if not match:
            return []
        return self.search_match(match, paper_ids=paper_ids, top_k=top_k, fallback_terms=[query])

    def search_match(
        self,
        match: str,
        *,
        paper_ids: list[str] | None = None,
        top_k: int = 8,
        fallback_terms: list[str] | None = None,
    ) -> list[dict]:
        """执行 FTS5 MATCH 表达式。返回 [{chunk..., score, paper_title}]，score 越大越相关。"""
        if not match:
            return []
        sql = [
            "SELECT c.id, c.paper_id, c.page, c.section, c.text,",
            "       p.title AS paper_title, p.year AS paper_year,",
            "       bm25(chunks_fts) AS raw_score",
            "FROM chunks_fts f JOIN chunks c ON c.rowid = f.rowid",
            "JOIN papers p ON p.id = c.paper_id",
            "WHERE chunks_fts MATCH ?",
        ]
        vals: list[Any] = [match]
        if paper_ids:
            sql.append(f"AND c.paper_id IN ({','.join('?' * len(paper_ids))})")
            vals += paper_ids
        sql.append("ORDER BY raw_score LIMIT ?")
        vals.append(max(top_k * 6, top_k))
        try:
            rows = self._con.execute(" ".join(sql), vals).fetchall()
        except sqlite3.OperationalError:
            return self._search_like(fallback_terms or [match], paper_ids, top_k)

        out = []
        for r in rows:
            d = dict(r)
            d["score"] = round(-float(d.pop("raw_score")), 4)   # bm25 越小越相关 → 取负
            out.append(d)
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]

    def _search_like(self, terms: list[str], paper_ids: list[str] | None, top_k: int) -> list[dict]:
        """FTS 不可用时的降级检索。"""
        terms = [t for t in terms if t and len(t) >= 2][:6] or ["the"]
        conds = " OR ".join(["c.text LIKE ?"] * len(terms))
        vals: list[Any] = [f"%{t}%" for t in terms]
        sql = (
            "SELECT c.id, c.paper_id, c.page, c.section, c.text, p.title AS paper_title,"
            " p.year AS paper_year FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE " + conds
        )
        if paper_ids:
            sql += f" AND c.paper_id IN ({','.join('?' * len(paper_ids))})"
            vals += paper_ids
        sql += " LIMIT ?"
        vals.append(top_k)
        return [dict(r) | {"score": 1.0} for r in self._con.execute(sql, vals).fetchall()]

    # ------------------------------------------------------------------
    # 结构化解读
    # ------------------------------------------------------------------
    def save_analysis(self, paper_id: str, data: dict, model: str = "") -> None:
        with self.tx() as con:
            con.execute(
                """INSERT INTO analyses (paper_id, research_question, methodology, experiment_design,
                        key_conclusions, innovations, limitations, field, keywords, payload, model, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(paper_id) DO UPDATE SET
                        research_question=excluded.research_question,
                        methodology=excluded.methodology,
                        experiment_design=excluded.experiment_design,
                        key_conclusions=excluded.key_conclusions,
                        innovations=excluded.innovations,
                        limitations=excluded.limitations,
                        field=excluded.field,
                        keywords=excluded.keywords,
                        payload=excluded.payload,
                        model=excluded.model,
                        created_at=excluded.created_at""",
                (
                    paper_id,
                    data.get("research_question"), data.get("methodology"),
                    data.get("experiment_design"), data.get("key_conclusions"),
                    data.get("innovations"), data.get("limitations"),
                    data.get("field"),
                    json.dumps(data.get("keywords", []), ensure_ascii=False),
                    json.dumps(data, ensure_ascii=False), model, _now(),
                ),
            )

    def get_analysis(self, paper_id: str) -> dict | None:
        row = self._con.execute("SELECT * FROM analyses WHERE paper_id = ?", (paper_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["keywords"] = _j(d.get("keywords"), [])
        d["payload"] = _j(d.get("payload"), {})
        return d

    # ------------------------------------------------------------------
    # 术语
    # ------------------------------------------------------------------
    def add_concepts(self, paper_id: str, concepts: list[dict]) -> int:
        n = 0
        with self.tx() as con:
            for c in concepts:
                term = (c.get("term") or "").strip()
                if not term:
                    continue
                con.execute(
                    "INSERT INTO concepts (id, paper_id, term, definition, category, created_at) VALUES (?,?,?,?,?,?)",
                    (_new_id("t_"), paper_id, term, c.get("definition"), c.get("category"), _now()),
                )
                n += 1
        return n

    def list_concepts(self, paper_id: str | None = None, term: str | None = None, limit: int = 200) -> list[dict]:
        sql, vals = "SELECT * FROM concepts", []
        conds = []
        if paper_id:
            conds.append("paper_id = ?")
            vals.append(paper_id)
        if term:
            conds.append("term LIKE ?")
            vals.append(f"%{term}%")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY term LIMIT ?"
        vals.append(limit)
        return [dict(r) for r in self._con.execute(sql, vals).fetchall()]

    # ------------------------------------------------------------------
    # 关联
    # ------------------------------------------------------------------
    def upsert_relation(
        self, paper_a: str, paper_b: str, rel_type: str, score: float, evidence: str = ""
    ) -> None:
        a, b = sorted([paper_a, paper_b])
        with self.tx() as con:
            con.execute(
                """INSERT INTO relations (id, paper_a, paper_b, rel_type, score, evidence, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(paper_a, paper_b, rel_type) DO UPDATE SET
                       score=excluded.score, evidence=excluded.evidence, created_at=excluded.created_at""",
                (_new_id("r_"), a, b, rel_type, float(score), evidence, _now()),
            )

    def list_relations(self, paper_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT r.*, pa.title AS title_a, pb.title AS title_b"
            " FROM relations r"
            " JOIN papers pa ON pa.id = r.paper_a"
            " JOIN papers pb ON pb.id = r.paper_b"
        )
        vals: list[Any] = []
        if paper_id:
            sql += " WHERE r.paper_a = ? OR r.paper_b = ?"
            vals += [paper_id, paper_id]
        sql += " ORDER BY r.score DESC LIMIT ?"
        vals.append(limit)
        return [dict(r) for r in self._con.execute(sql, vals).fetchall()]

    # ------------------------------------------------------------------
    # 核验日志
    # ------------------------------------------------------------------
    def log_verification(self, query: str, result: dict) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO verify_log (id, query, doi, title, result, level, confidence, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    _new_id("v_"), query, result.get("doi"), result.get("query_title"),
                    json.dumps(result, ensure_ascii=False), result.get("level"),
                    float(result.get("confidence") or 0.0), _now(),
                ),
            )

    # ------------------------------------------------------------------
    # 会话与消息（append-only）
    # ------------------------------------------------------------------
    def add_conversation(
        self,
        title: str = "新会话",
        *,
        scope: Any = None,
        model: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        cid = conversation_id or _new_id("conv_")
        now = _now()
        with self.tx() as con:
            con.execute(
                "INSERT INTO conversations (id, title, scope, model, pinned, compact_anchor_seq,"
                " created_at, updated_at) VALUES (?,?,?,?,0,0,?,?)",
                (cid, title, json.dumps(scope, ensure_ascii=False) if scope else None, model, now, now),
            )
        return cid

    def list_conversations(self, limit: int = 100) -> list[dict]:
        rows = self._con.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count"
            " FROM conversations c ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: str) -> bool:
        """删除会话及全部关联数据。

        ``messages`` / ``compaction_*`` / ``turn_views`` 都对 ``conversations``
        设了 ``ON DELETE CASCADE``，且连接开启了 ``foreign_keys = ON``，
        所以删主表一条即可级联清干净。
        """
        with self.tx() as con:
            cur = con.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cur.rowcount > 0

    def resolve_conversation(self, ref: str) -> dict | None:
        """按 id / id 前缀 / 标题片段定位会话；``ref`` 为空时取最近一个。"""
        ref = (ref or "").strip()
        if not ref:
            rows = self.list_conversations(limit=1)
            return rows[0] if rows else None
        row = self._con.execute("SELECT * FROM conversations WHERE id = ?", (ref,)).fetchone()
        if row:
            return dict(row)
        rows = self._con.execute(
            "SELECT * FROM conversations WHERE id LIKE ? ORDER BY updated_at DESC LIMIT 2", (f"{ref}%",)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        row = self._con.execute(
            "SELECT * FROM conversations WHERE title LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"%{ref}%",),
        ).fetchone()
        return dict(row) if row else None

    def ensure_default_conversation(self) -> str:
        rows = self.list_conversations(limit=1)
        if rows:
            return rows[0]["id"]
        return self.add_conversation("默认会话")

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        citations: Any = None,
        citation_check: Any = None,
        kind: str = "normal",
        meta: Any = None,
    ) -> dict:
        """追加一条消息。seq 与 ref 自动分配（append-only，永不改写既有消息）。"""
        row = self._con.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        seq = int(row["m"]) + 1
        ref = make_ref(seq)
        mid = _new_id("msg_")
        now = _now()
        with self.tx() as con:
            con.execute(
                "INSERT INTO messages (id, conversation_id, seq, ref, role, content, citations,"
                " citation_check, kind, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid, conversation_id, seq, ref, role, content,
                    json.dumps(citations, ensure_ascii=False) if citations is not None else None,
                    json.dumps(citation_check, ensure_ascii=False) if citation_check is not None else None,
                    kind,
                    json.dumps(meta, ensure_ascii=False) if meta is not None else None,
                    now,
                ),
            )
            con.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
        return {"id": mid, "seq": seq, "ref": ref, "role": role, "content": content, "kind": kind}

    def get_messages(self, conversation_id: str, *, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT seq, ref, role, content, citations, citation_check, kind, shadowed_refs,"
            " block_id, created_at FROM messages WHERE conversation_id = ? ORDER BY seq"
        )
        vals: list[Any] = [conversation_id]
        if limit:
            sql += " LIMIT ?"
            vals.append(limit)
        return [dict(r) for r in self._con.execute(sql, vals).fetchall()]

    def touch_anchor(self, conversation_id: str, seq: int) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE conversations SET compact_anchor_seq = ?, updated_at = ? WHERE id = ?",
                (int(seq), _now(), conversation_id),
            )

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        con = self._con
        total = con.execute("SELECT COUNT(*) n FROM papers").fetchone()["n"]
        by_status = {
            r["verify_status"]: r["n"]
            for r in con.execute("SELECT verify_status, COUNT(*) n FROM papers GROUP BY verify_status")
        }
        by_level = {
            r["verify_level"]: r["n"]
            for r in con.execute("SELECT verify_level, COUNT(*) n FROM papers GROUP BY verify_level")
        }
        return {
            "papers": total,
            "chunks": con.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"],
            "analyses": con.execute("SELECT COUNT(*) n FROM analyses").fetchone()["n"],
            "concepts": con.execute("SELECT COUNT(*) n FROM concepts").fetchone()["n"],
            "relations": con.execute("SELECT COUNT(*) n FROM relations").fetchone()["n"],
            "verifications": con.execute("SELECT COUNT(*) n FROM verify_log").fetchone()["n"],
            "by_verify_status": by_status,
            "by_verify_level": by_level,
            "db_path": str(self.db_path),
        }

    def paper_brief(self, paper: dict, width: int = 72) -> str:
        authors = paper.get("authors") or []
        a = ", ".join(authors[:3]) + (" 等" if len(authors) > 3 else "")
        # 徽章统一由 whitelist.badge() 产出 —— 自动核验与人工确认必须视觉可区分
        mark = badge(paper.get("verify_level") or paper.get("verify_status"))
        return f"[{paper['id']}] {truncate(paper['title'], width)}\n    {a or '作者未知'} · {paper.get('year') or '年份未知'} · {mark}"
