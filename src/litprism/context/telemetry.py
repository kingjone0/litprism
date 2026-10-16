"""可观测性（Telemetry）—— 让"上下文发生了什么"可见。

三层投影（最低配）
------------------
* ``tokenUsage``      —— uncachedInput / output / cacheRead / cacheWrite，用于计费与趋势
* ``contextPressure`` —— 提供方上报的真实 prompt 规模 / 预计规模 / 窗口
* ``contextBreakdown``—— 上下文由什么组成

LitPrism 的构成分解要**特别标注"引用片段"占比** —— 这是本项目最大的一块上下文消耗，
用户需要看到它。

简化 miss 归因
--------------
参考文档把缓存 miss 分三类（新增 / 压缩重付 / TTL 过期）。但那需要知道
"最早待结算折叠起点"。本项目是单机单用户、调用间隔长、缓存收益本就不高，
**简化为一类并如实标注"含无法归因部分"** —— 不要假装能精确归因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config, ContextConfig
from .meter import Meter, Measurement


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------
@dataclass
class TokenUsage:
    uncached_input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def input_total(self) -> int:
        return self.uncached_input + self.cache_read

    @property
    def cache_hit_ratio(self) -> float:
        if self.input_total <= 0:
            return 0.0
        return round(self.cache_read / self.input_total, 4)

    def to_dict(self) -> dict:
        return {
            "uncached_input": self.uncached_input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "input_total": self.input_total,
            "cache_hit_ratio": self.cache_hit_ratio,
        }


@dataclass
class ContextPressure:
    estimated: int = 0
    reported: int | None = None
    window: int = 32000
    output_reserve: int = 4096

    @property
    def effective(self) -> int:
        """优先用上报值 —— 估算对中文有偏差。"""
        return self.reported if self.reported is not None else self.estimated

    @property
    def ratio(self) -> float:
        if self.window <= 0:
            return 0.0
        return round(self.effective / self.window, 4)

    def to_dict(self) -> dict:
        return {
            "estimated": self.estimated,
            "reported": self.reported,
            "effective": self.effective,
            "window": self.window,
            "ratio": self.ratio,
            "source": "提供方上报" if self.reported is not None else "估算值",
        }


@dataclass
class ContextBreakdown:
    system: int = 0
    dialogue: int = 0
    summary: int = 0
    citations: int = 0
    total: int = 0

    def to_dict(self) -> dict:
        d = {
            "system": self.system,
            "dialogue": self.dialogue,
            "summary": self.summary,
            "citations": self.citations,
            "total": self.total,
        }
        if self.total > 0:
            d["percent"] = {
                k: round(v / self.total * 100, 1)
                for k, v in d.items()
                if k != "total"
            }
        return d


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------
@dataclass
class MissAttribution:
    """缓存 miss 的简化归因。

    ``comp_repay`` 是"压缩造成的重付"，**含无法归因部分** —— 如实标注，不假装精确。
    """

    missed: int = 0
    new_content: int = 0
    comp_repay: int = 0
    note: str = (
        "本项目为单机单用户、调用间隔长，缓存收益有限；"
        "comp_repay 含无法归因部分（如 TTL 过期），不做进一步拆分。"
    )

    def to_dict(self) -> dict:
        return {
            "missed": self.missed,
            "new_content": self.new_content,
            "comp_repay": self.comp_repay,
            "note": self.note,
        }

    def describe(self) -> str:
        if self.missed <= 0:
            return "本轮无缓存 miss"
        return (
            f"miss {self.missed} token：正常新增 {self.new_content}、"
            f"压缩重付 {self.comp_repay}（含无法归因部分）"
        )


def attribute_miss(prev: TokenUsage | None, cur: TokenUsage) -> MissAttribution:
    """简化归因：把 miss 拆成「正常新增」与「压缩重付」两类。

    恒等式：``new_content + comp_repay == missed``
    """
    missed = max(0, cur.input_total - cur.cache_read)
    if prev is None:
        return MissAttribution(missed=missed, new_content=missed, comp_repay=0)
    growth = max(0, cur.input_total - prev.input_total)
    new_content = min(missed, growth)
    return MissAttribution(missed=missed, new_content=new_content, comp_repay=missed - new_content)


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------
class Telemetry:
    """三层投影 + 压缩账本。"""

    def __init__(self, kb: Any, cfg: Config | ContextConfig | None = None):
        self.kb = kb
        if isinstance(cfg, Config):
            self.cfg = cfg.context
        elif isinstance(cfg, ContextConfig):
            self.cfg = cfg
        else:
            self.cfg = ContextConfig()
        self.meter = Meter(self.cfg)
        self._last_usage: dict[str, TokenUsage] = {}

    # ------------------------------------------------------------------
    def record_usage(self, conversation_id: str, usage: TokenUsage, envelope_hash: str | None = None) -> None:
        """记录一次调用的用量。同时把上报值喂给 Meter 用于 envelope 匹配。"""
        self._last_usage[conversation_id] = usage
        if envelope_hash and usage.input_total > 0:
            self.meter.record_provider_usage(envelope_hash, usage.input_total)

    def last_usage(self, conversation_id: str) -> TokenUsage | None:
        return self._last_usage.get(conversation_id)

    # ------------------------------------------------------------------
    def pressure(self, measurement: Measurement) -> ContextPressure:
        return ContextPressure(
            estimated=measurement.surface_tokens,
            reported=measurement.provider_reported,
            window=measurement.window_tokens,
            output_reserve=measurement.output_reserve_tokens,
        )

    def breakdown(self, measurement: Measurement) -> ContextBreakdown:
        raw = measurement.breakdown()
        return ContextBreakdown(
            system=raw.get("system", 0),
            dialogue=raw.get("对话", 0),
            summary=raw.get("摘要", 0),
            citations=raw.get("引用片段", 0),
            total=measurement.surface_tokens,
        )

    # ------------------------------------------------------------------
    def snapshot(self, conversation_id: str) -> dict:
        """状态面板所需的一次性快照。"""
        meas = self.meter.measure(self.kb, conversation_id)
        conv = self.kb._con.execute(
            "SELECT compact_anchor_seq FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        anchor = int(conv["compact_anchor_seq"]) if conv and conv["compact_anchor_seq"] is not None else 0

        blocks = self.kb._con.execute(
            "SELECT id, topic, summary_token_count, shadowed_token_count, survived_count, active,"
            " citation_handles, direct_refs FROM compaction_blocks WHERE conversation_id = ?"
            " ORDER BY created_at",
            (conversation_id,),
        ).fetchall()

        block_items = []
        for b in blocks:
            handles = _loads(b["citation_handles"], [])
            refs = _loads(b["direct_refs"], [])
            block_items.append(
                {
                    "id": b["id"],
                    "topic": b["topic"] or "",
                    "range": f"{refs[0]}-{refs[-1]}" if refs else "—",
                    "shadowed_tokens": int(b["shadowed_token_count"] or 0),
                    "summary_tokens": int(b["summary_token_count"] or 0),
                    "age": int(b["survived_count"] or 0),
                    "active": bool(int(b["active"] or 0)),
                    "handles": len(handles),
                }
            )

        last = self._last_usage.get(conversation_id)
        return {
            "conversation_id": conversation_id,
            "anchor_seq": anchor,
            "token_usage": (last or TokenUsage()).to_dict(),
            "context_pressure": self.pressure(meas).to_dict(),
            "context_breakdown": self.breakdown(meas).to_dict(),
            "two_kinds_of_occupancy": {
                "total_tokens": meas.total_tokens,
                "surface_tokens": meas.surface_tokens,
                "note": "total 是会话记账（含已压缩原文），surface 是实际发送视图 —— 两者必须分开看",
            },
            "blocks": block_items,
            "ledger": self.ledger(conversation_id),
            "estimated": meas.estimated,
            "note": meas.note,
        }

    # ------------------------------------------------------------------
    def ledger(self, conversation_id: str, limit: int = 20) -> list[dict]:
        rows = self.kb._con.execute(
            "SELECT * FROM compaction_ledger WHERE conversation_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            saved = max(0, int(d["shadowed_tokens"]) - int(d["summary_tokens"]))
            d["saved_per_turn"] = saved
            d["breakeven_turns"] = (
                None if saved <= 0 else _ceil(int(d["repriced_tokens"] or 0) / saved)
            )
            out.append(d)
        return out

    def totals(self, conversation_id: str) -> dict:
        row = self.kb._con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(shadowed_tokens),0) s,"
            " COALESCE(SUM(summary_tokens),0) m, COALESCE(SUM(repriced_tokens),0) r"
            " FROM compaction_ledger WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        saved = max(0, int(row["s"]) - int(row["m"]))
        return {
            "compactions": int(row["n"]),
            "shadowed_tokens_total": int(row["s"]),
            "summary_tokens_total": int(row["m"]),
            "repriced_tokens_total": int(row["r"]),
            "net_saving_per_turn": saved,
            "compression_ratio": round(int(row["m"]) / int(row["s"]), 4) if int(row["s"]) else None,
        }


# ---------------------------------------------------------------------------
# 呈现
# ---------------------------------------------------------------------------
def format_snapshot(snap: dict) -> str:
    """把快照渲染成可读的状态面板。"""
    cp = snap["context_pressure"]
    bd = snap["context_breakdown"]
    occ = snap["two_kinds_of_occupancy"]
    lines = [
        "上下文状态",
        "─" * 46,
        f"  会话记账      {occ['total_tokens']:>8} token",
        f"  发送视图      {occ['surface_tokens']:>8} token",
        f"  窗口压力      {cp['ratio']:>8.1%}  （窗口 {cp['window']}，来源：{cp['source']}）",
        f"  压缩锚点      m{snap['anchor_seq']:05d}",
        "",
        "  构成分解",
    ]
    pct = bd.get("percent") or {}
    for key, label in (("system", "系统提示词"), ("dialogue", "对话"), ("summary", "摘要"), ("citations", "引用片段")):
        v = bd.get(key, 0)
        p = pct.get(key, 0.0)
        lines.append(f"    {label:<10} {v:>8} token  {p:>5.1f}%")

    if snap.get("blocks"):
        lines.append("")
        lines.append("  压缩块")
        for b in snap["blocks"]:
            mark = "●" if b["active"] else "○"
            lines.append(
                f"    {mark} {b['id']}  {b['range']}  "
                f"{b['shadowed_tokens']}→{b['summary_tokens']} token  "
                f"句柄 {b['handles']}  {b['topic']}"
            )
    if snap.get("ledger"):
        lines.append("")
        lines.append("  压缩账本（最近）")
        for e in snap["ledger"][:5]:
            be = e.get("breakeven_turns")
            be_text = "立即" if be == 0 else (f"{be} 轮" if be else "不回本")
            lines.append(
                f"    {e['trigger']:<9} 省 {e['saved_per_turn']:>6}/轮  回本 {be_text}"
            )
    if snap.get("note"):
        lines.append("")
        lines.append(f"  注：{snap['note']}")
    return "\n".join(lines)


def _loads(raw: Any, default: Any) -> Any:
    import json

    if not raw:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def _ceil(x: float) -> int:
    import math

    return int(math.ceil(x))
