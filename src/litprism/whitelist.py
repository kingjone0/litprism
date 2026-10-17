"""核验判定等级与人工确认放行（白名单）。

五态判定
--------
| 判定 | 含义 | 允许引用 |
|---|---|---|
| ``verified`` | 自动核验通过（DOI 精确命中或相似度 ≥ 阈值 + 佐证） | ✅ |
| ``manual_confirmed`` | 自动核验未通过，但**用户人工确认**并留痕 | ✅（徽章与 verified 区分） |
| ``likely`` | 疑似匹配，待用户处理 | ⏸ 挂起 |
| ``not_found`` | 数据库正常响应但无匹配 | ❌（可申请人工确认） |
| ``unchecked`` | 数据源全部失败 | ❌（建议重试，而非直接人工确认） |

关键设计：白名单**并存**于自动核验结论，而不是覆盖它
------------------------------------------------
``whitelist.level_at_confirm`` 与 ``verify_evidence`` 记录"确认当时自动核验判了什么"。
这样将来复核时能分清**哪些是机器判的、哪些是人拍的板**，
不会把人的判断伪装成机器的结论。

这是本模块最重要的一条：人工放行是**记录在案的例外**，不是**抹掉证据的重写**。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# 判定等级
# ---------------------------------------------------------------------------
LEVELS = ("verified", "manual_confirmed", "likely", "not_found", "unchecked")

LEVEL_LABELS = {
    "verified": "已核验",
    "manual_confirmed": "人工确认",
    "likely": "疑似匹配",
    "not_found": "未能核到",
    "unchecked": "无法判定",
}

# 徽章：自动核验与人工确认**必须用不同颜色/符号**，避免混淆
LEVEL_BADGES = {
    "verified": "✅已核验",
    "manual_confirmed": "🔵人工确认",
    "likely": "⚠️疑似",
    "not_found": "❌未核到",
    "unchecked": "❔无法判定",
}

# 允许被引用的等级
CITABLE_LEVELS = ("verified", "manual_confirmed")

# 可申请人工确认的等级（unchecked 不建议 —— 应先重试网络）
CONFIRMABLE_LEVELS = ("likely", "not_found", "unchecked")

LEVEL_MESSAGES = {
    "verified": "自动核验通过",
    "manual_confirmed": "自动核验未通过，已由用户人工确认放行",
    "likely": "疑似匹配，需人工确认后才能引用",
    "not_found": "权威数据库中无匹配记录，引用前必须人工复核",
    "unchecked": "数据源不可用，无法判定（不等于该文献不存在）",
}


def is_citable(level: str | None) -> bool:
    return (level or "") in CITABLE_LEVELS


def badge(level: str | None) -> str:
    return LEVEL_BADGES.get(level or "", "❔未知")


def label(level: str | None) -> str:
    return LEVEL_LABELS.get(level or "", level or "未知")


def normalize_level(level: str | None) -> str:
    """把历史/兼容取值映射到五态。"""
    lv = (level or "").strip().lower()
    if lv in LEVELS:
        return lv
    # 兼容旧值
    if lv in ("unverified", "unknown", ""):
        return "unchecked"
    return "unchecked"


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class WhitelistEntry:
    id: str
    title: str
    reason: str
    doi: str | None = None
    paper_id: str | None = None
    level_at_confirm: str = "not_found"
    verify_evidence: dict = field(default_factory=dict)
    confirmed_at: str = ""
    confirmed_by: str = "local_user"

    @property
    def original_verdict(self) -> str:
        """确认当时的自动核验判定 —— 这个事实必须保留。"""
        return self.level_at_confirm

    def render(self) -> str:
        doi = f"  DOI: {self.doi}" if self.doi else ""
        return (
            f"[{self.id}] {self.title}\n"
            f"      确认时自动判定：{label(self.level_at_confirm)}"
            f"（{LEVEL_MESSAGES.get(self.level_at_confirm, '')}）\n"
            f"      依据：{self.reason}\n"
            f"      确认人：{self.confirmed_by}  时间：{self.confirmed_at}{doi}"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "doi": self.doi,
            "paper_id": self.paper_id,
            "level_at_confirm": self.level_at_confirm,
            "verify_evidence": self.verify_evidence,
            "reason": self.reason,
            "confirmed_at": self.confirmed_at,
            "confirmed_by": self.confirmed_by,
        }


class WhitelistError(ValueError):
    """白名单操作被拒绝。"""


class Whitelist:
    """人工确认放行的白名单。"""

    def __init__(self, kb: Any):
        self.kb = kb

    # ------------------------------------------------------------------
    def add(
        self,
        *,
        title: str,
        reason: str,
        doi: str | None = None,
        paper_id: str | None = None,
        level_at_confirm: str = "not_found",
        verify_evidence: dict | None = None,
        confirmed_by: str = "local_user",
    ) -> WhitelistEntry:
        """加入白名单。

        **确认依据必填** —— 这是防止白名单被滥用成"万能放行"的第一道闸门。
        """
        if not (title or "").strip():
            raise WhitelistError("标题不能为空")
        if not (reason or "").strip():
            raise WhitelistError(
                "确认依据必填。请说明为什么认为这篇文献真实存在"
                "（例如：在会议论文集里见过 / DOI 在出版社页面可访问 / 导师推荐的必读文献）。"
                "没有依据的放行会让白名单退化成万能通行证。"
            )
        if len(reason.strip()) < 4:
            raise WhitelistError(f"确认依据过短（{len(reason.strip())} 字）。请写清具体理由。")

        lv = normalize_level(level_at_confirm)
        if lv not in CONFIRMABLE_LEVELS:
            raise WhitelistError(
                f"判定 {lv!r} 不需要人工确认。只有 {CONFIRMABLE_LEVELS} 才需要 —— "
                "已核验的不必再确认。"
            )

        existing = self.find(doi=doi, title=title)
        if existing:
            return existing

        entry = WhitelistEntry(
            id=f"wl_{uuid.uuid4().hex[:10]}",
            title=title.strip(),
            reason=reason.strip(),
            doi=doi,
            paper_id=paper_id,
            level_at_confirm=lv,
            verify_evidence=verify_evidence or {},
            confirmed_at=_now(),
            confirmed_by=confirmed_by,
        )
        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO whitelist (id, paper_id, doi, title, level_at_confirm, verify_evidence,"
                " reason, confirmed_at, confirmed_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry.id, entry.paper_id, entry.doi, entry.title, entry.level_at_confirm,
                    json.dumps(entry.verify_evidence, ensure_ascii=False),
                    entry.reason, entry.confirmed_at, entry.confirmed_by,
                ),
            )
        # 旧库中已存在的待核验资料也必须与确认状态同步，原自动判定保存在白名单。
        for paper in self.kb.list_papers(limit=100000):
            matched = self.find(doi=paper.get('doi'), title=paper.get('title'))
            if matched and matched.id == entry.id and paper.get('verify_level') != 'verified':
                self.kb.update_paper(paper['id'], verify_level='manual_confirmed', verify_status='manual_confirmed',
                                     verify_evidence={**entry.verify_evidence, 'original_level': lv, 'whitelist_id': entry.id})
        return entry

    # ------------------------------------------------------------------
    def find(self, *, doi: str | None = None, title: str | None = None) -> WhitelistEntry | None:
        """按 DOI 或标题查白名单。"""
        if doi:
            row = self.kb._con.execute(
                "SELECT * FROM whitelist WHERE lower(doi) = lower(?) LIMIT 1", (doi,)
            ).fetchone()
            if row:
                return _row_to_entry(row)

        if title:
            from .textutil import normalize_title, title_similarity

            # 先试精确归一化匹配（快路径）
            nt = normalize_title(title)
            if nt:
                for row in self.kb._con.execute("SELECT * FROM whitelist").fetchall():
                    if normalize_title(row["title"]) == nt:
                        return _row_to_entry(row)
                # 再试相似度（慢路径，白名单通常很小所以可接受）
                best, best_sim = None, 0.0
                for row in self.kb._con.execute("SELECT * FROM whitelist").fetchall():
                    s = title_similarity(title, row["title"])
                    if s > best_sim:
                        best, best_sim = row, s
                if best is not None and best_sim >= 0.92:
                    return _row_to_entry(best)
        return None

    def get(self, entry_id: str) -> WhitelistEntry | None:
        row = self.kb._con.execute("SELECT * FROM whitelist WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def list(self, limit: int = 200) -> list[WhitelistEntry]:
        rows = self.kb._con.execute(
            "SELECT * FROM whitelist ORDER BY confirmed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def remove(self, entry_id: str) -> bool:
        entry = self.get(entry_id)
        if entry:
            for paper in self.kb.list_papers(limit=100000):
                if (paper.get('verify_evidence') or {}).get('whitelist_id') == entry_id:
                    self.kb.update_paper(paper['id'], verify_level=entry.level_at_confirm,
                                         verify_status=entry.level_at_confirm, verify_evidence=entry.verify_evidence)
        with self.kb.tx() as con:
            cur = con.execute("DELETE FROM whitelist WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    def stats(self) -> dict:
        row = self.kb._con.execute("SELECT COUNT(*) n FROM whitelist").fetchone()
        by_level = {
            r["level_at_confirm"]: r["n"]
            for r in self.kb._con.execute(
                "SELECT level_at_confirm, COUNT(*) n FROM whitelist GROUP BY level_at_confirm"
            ).fetchall()
        }
        return {"total": int(row["n"]), "by_original_verdict": by_level}


def _row_to_entry(row: Any) -> WhitelistEntry:
    d = dict(row)
    evidence = d.get("verify_evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = {}
    return WhitelistEntry(
        id=d["id"],
        title=d["title"],
        reason=d.get("reason") or "",
        doi=d.get("doi"),
        paper_id=d.get("paper_id"),
        level_at_confirm=normalize_level(d.get("level_at_confirm")),
        verify_evidence=evidence or {},
        confirmed_at=d.get("confirmed_at") or "",
        confirmed_by=d.get("confirmed_by") or "local_user",
    )


def format_entries(entries: list[WhitelistEntry]) -> str:
    if not entries:
        return "（白名单为空）"
    return "\n".join("  " + e.render().replace("\n", "\n  ") for e in entries)
