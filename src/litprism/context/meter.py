"""测量服务（Meter）—— 所有上下文决策的**唯一定价来源**。

为什么必须只有一套
------------------
如果策略算一套、UI 显示另一套，两边就会打架：用户看到"占用 60%"但系统判定"该压缩了"。
所以这里的 :meth:`Meter.measure` 是**唯一**的定价入口，UI 只是它的另一个消费者。

两种占用必须分开
----------------
* ``total_tokens`` —— **会话记账**。含已被压缩的原文，所以它下降得慢。
* ``surface_tokens`` —— **发送视图**。压后实际发给模型的大小。

混用会让用户以为"压缩没生效"。参考实现的经验：某会话记账 150K，而发送视图只有 46K。

关于估算偏差（重要）
--------------------
无 tokenizer 时按字符数启发式估算。**4 字符/token 对中文严重低估（可能低 2~4 倍）**。
本项目是中英混排的学术场景，偏差尤其明显，因此：

1. 中文与拉丁文分别用各自的系数；
2. 测量结果带 ``estimated`` 标记，UI 上必须标注"估算值"；
3. 优先复用**提供方上报的真实用量**，条件是「最新成功调用的请求 envelope
   与待测 envelope 完全匹配」—— 不完全匹配就完全估算，不做部分混用。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..config import Config, ContextConfig
from ..refs import parse_ref, safe_ref

# 每条消息的固定开销（角色标记、分隔符等）
MESSAGE_OVERHEAD = 4
# 每个压缩块（checkpoint 消息）的固定开销
BLOCK_OVERHEAD = 12

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")

CJK_NOTE = "token 数为估算值；中文按 1.5 字符/token 计，与提供方实际计量可能有偏差。"


@dataclass
class NodeCost:
    """一个可见节点的定价。"""

    seq: int
    ref: str
    role: str
    kind: str                 # normal | compact-checkpoint
    tokens: int
    block_id: str | None = None
    protected: bool = False   # 是否落在受保护区
    protected_reason: str = ""

    @property
    def visible(self) -> bool:
        return True


@dataclass
class Measurement:
    total_tokens: int
    surface_tokens: int
    nodes: list[NodeCost] = field(default_factory=list)
    window_tokens: int = 32000
    output_reserve_tokens: int = 4096
    estimated: bool = True
    provider_reported: int | None = None
    note: str = CJK_NOTE

    @property
    def pressure(self) -> float:
        """发送视图相对窗口的压力（0~1+）。触发决策读这个。"""
        if self.window_tokens <= 0:
            return 0.0
        return round(self.surface_tokens / self.window_tokens, 4)

    @property
    def headroom_tokens(self) -> int:
        """还能塞多少（扣掉输出预留）。"""
        return self.window_tokens - self.output_reserve_tokens - self.surface_tokens

    def visible_nodes(self) -> list[NodeCost]:
        return list(self.nodes)

    def breakdown(self) -> dict[str, int]:
        """上下文构成分解 —— UI 的「上下文由什么组成」。

        注意判断顺序：**先判 kind 再判 role**。摘要块的 role 是 system，
        若先判 role 会把摘要计入 system，导致用户看到"系统提示词占了 40%"的假象。
        """
        out = {"system": 0, "对话": 0, "摘要": 0, "引用片段": 0}
        for n in self.nodes:
            if n.kind == "compact-checkpoint":
                out["摘要"] += n.tokens
            elif n.role == "system":
                out["system"] += n.tokens
            else:
                out["对话"] += n.tokens
        return out


class Meter:
    """测量服务。"""

    def __init__(self, cfg: Config | ContextConfig | None = None):
        if isinstance(cfg, Config):
            self.cfg = cfg.context
        elif isinstance(cfg, ContextConfig):
            self.cfg = cfg
        else:
            self.cfg = ContextConfig()
        # provider 上报的真实用量缓存：envelope_hash -> tokens
        self._provider_usage: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 估算
    # ------------------------------------------------------------------
    def estimate_text(self, text: str | None) -> int:
        """混排文本的 token 估算。中文与拉丁文用各自系数。"""
        if not text:
            return 0
        cjk = len(_CJK_RE.findall(text))
        other = max(0, len(text) - cjk)
        cjk_ratio = max(0.5, self.cfg.cjk_chars_per_token)
        latin_ratio = max(1.0, self.cfg.latin_chars_per_token)
        tokens = cjk / cjk_ratio + other / latin_ratio
        return max(1, int(tokens + 0.5))

    def estimate_message(self, msg: Mapping[str, Any]) -> int:
        """单条消息的定价（含固定开销与结构化附件）。"""
        tokens = MESSAGE_OVERHEAD
        tokens += self.estimate_text(msg.get("content"))
        # 引用附件也要计价 —— 它同样占上下文
        cites = msg.get("citations")
        if cites:
            tokens += self.estimate_text(cites if isinstance(cites, str) else json.dumps(cites, ensure_ascii=False))
        return tokens

    def estimate_block(self, block: Mapping[str, Any]) -> int:
        """压缩块（checkpoint 消息）的定价。"""
        return BLOCK_OVERHEAD + self.estimate_text(block.get("summary"))

    # ------------------------------------------------------------------
    # 测量
    # ------------------------------------------------------------------
    def measure_nodes(
        self,
        messages: Sequence[Mapping[str, Any]],
        blocks: Sequence[Mapping[str, Any]] = (),
        *,
        protected_seqs: set[int] | None = None,
        protected_reasons: Mapping[int, str] | None = None,
    ) -> Measurement:
        """核心测量。``messages`` 与 ``blocks`` 为数据库行的映射。

        ``total_tokens`` 计全部消息（含已被遮蔽的原文）；
        ``surface_tokens`` 只计当前可见节点（未遮蔽消息 + 活跃摘要块）。
        """
        protected_seqs = protected_seqs or set()
        protected_reasons = protected_reasons or {}

        active_blocks = [b for b in blocks if int(b.get("active", 1)) == 1]
        shadowed: set[int] = set()
        for b in active_blocks:
            for r in _load_refs(b.get("direct_refs")):
                shadowed.add(r)

        total = 0
        nodes: list[NodeCost] = []
        # 已被可见检查点消息代表的压缩块 —— 避免同一份摘要被计价两次
        represented_blocks: set[str] = set()

        for m in messages:
            seq = m.get("seq")
            if not isinstance(seq, int):
                continue
            cost = self.estimate_message(m)
            total += cost
            if seq in shadowed:
                continue
            if str(m.get("kind") or "") == "compact-checkpoint" and m.get("block_id"):
                represented_blocks.add(str(m["block_id"]))
            nodes.append(
                NodeCost(
                    seq=seq,
                    ref=str(m.get("ref") or safe_ref(seq)),
                    role=str(m.get("role") or "user"),
                    kind=str(m.get("kind") or "normal"),
                    tokens=cost,
                    block_id=str(m["block_id"]) if m.get("block_id") else None,
                    protected=seq in protected_seqs,
                    protected_reason=protected_reasons.get(seq, ""),
                )
            )

        for b in active_blocks:
            bid = str(b.get("id") or "")
            # 压缩块与它的替换消息是同一份摘要的两种表示。
            # 若替换消息已可见，就只算消息那一份 —— 否则摘要会被重复计价，
            # 用户会看到"摘要占了 13%"而账本说"压到 6%"的矛盾数字。
            if bid and bid in represented_blocks:
                continue
            cost = self.estimate_block(b)
            total += cost
            nodes.append(
                NodeCost(
                    seq=-1,
                    ref=bid or "block",
                    role="system",
                    kind="compact-checkpoint",
                    tokens=cost,
                    block_id=bid,
                )
            )

        nodes.sort(key=lambda n: (n.kind == "compact-checkpoint", n.seq))
        surface = sum(n.tokens for n in nodes)

        return Measurement(
            total_tokens=total,
            surface_tokens=surface,
            nodes=nodes,
            window_tokens=self.cfg.window_tokens,
            output_reserve_tokens=self.cfg.output_reserve_tokens,
        )

    def measure(self, kb: Any, conversation_id: str) -> Measurement:
        """从知识库读取并测量。"""
        rows = kb._con.execute(
            "SELECT seq, ref, role, content, citations, kind, block_id FROM messages"
            " WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        blocks = kb._con.execute(
            "SELECT id, summary, direct_refs, effective_refs, active FROM compaction_blocks"
            " WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        messages = [dict(r) for r in rows]
        block_dicts = [dict(b) for b in blocks]

        # 受保护区（详见 policy.py；这里只做定价标记，不参与裁剪决策）
        protected, reasons = self._default_protected(messages)
        m = self.measure_nodes(messages, block_dicts, protected_seqs=protected, protected_reasons=reasons)

        # 若提供方上报用量且 envelope 匹配，用真实值覆盖 surface
        env_hash = self.envelope_hash(messages, block_dicts)
        reported = self._provider_usage.get(env_hash)
        if reported is not None:
            m.provider_reported = reported
            m.surface_tokens = reported
            m.estimated = False
            m.note = "使用提供方上报的真实用量。"
        return m

    # ------------------------------------------------------------------
    # 提供方上报用量的采纳
    # ------------------------------------------------------------------
    @staticmethod
    def envelope_hash(messages: Sequence[Mapping[str, Any]], blocks: Sequence[Mapping[str, Any]] = ()) -> str:
        """请求 envelope 的指纹。

        只有在「最新成功调用的 envelope 与待测 envelope 完全匹配」时才采信上报值 ——
        否则会出现"用上一次的用量描述这一次的上下文"的错配。
        """
        h = hashlib.sha1()
        for m in messages:
            h.update(str(m.get("seq", "")).encode())
            h.update(str(m.get("role", "")).encode())
            h.update(str(m.get("content") or "").encode())
        for b in blocks:
            h.update(str(b.get("id", "")).encode())
            h.update(str(b.get("summary") or "").encode())
            h.update(str(b.get("active", 1)).encode())
        return h.hexdigest()[:16]

    def record_provider_usage(self, envelope_hash: str, tokens: int) -> None:
        if tokens > 0:
            self._provider_usage[envelope_hash] = int(tokens)

    def clear_provider_usage(self) -> None:
        self._provider_usage.clear()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _default_protected(self, messages: Sequence[Mapping[str, Any]]) -> tuple[set[int], dict[int, str]]:
        """最小受保护区：system + 第一条用户消息 + 最近 N 条。

        完整受保护区（含引用片段、交付物源范围）由 policy.py 组装后传入；
        这里只提供与策略无关的默认值，供 UI 展示用。
        """
        protected: set[int] = set()
        reasons: dict[int, str] = {}
        seqs = [m["seq"] for m in messages if isinstance(m.get("seq"), int)]
        if not seqs:
            return protected, reasons

        for m in messages:
            if m.get("role") == "system" and isinstance(m.get("seq"), int):
                protected.add(m["seq"])
                reasons[m["seq"]] = "系统提示词（前缀稳定 + 防幻觉约束载体）"

        users = [m["seq"] for m in messages if m.get("role") == "user" and isinstance(m.get("seq"), int)]
        if users:
            protected.add(users[0])
            reasons[users[0]] = "第一条用户消息（研究意图）"
            protected.add(users[-1])
            reasons[users[-1]] = "最后一条用户消息（当前指令）"

        for s in sorted(seqs)[-self.cfg.recent_turns_protected :]:
            protected.add(s)
            reasons.setdefault(s, "最近消息（活跃工作集）")
        return protected, reasons


def _load_refs(raw: Any) -> list[int]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[int] = []
    for x in raw or []:
        try:
            out.append(parse_ref(x))
        except Exception:  # noqa: BLE001 - 脏数据不应让测量崩掉
            continue
    return out
