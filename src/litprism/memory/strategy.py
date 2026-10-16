"""分层驻留策略（Layered Strategy）。

借鉴 `dsh-mnemon` 的 Layered strategy 与 Projection Budget：

* **常驻层（eager）**：偏好记忆，每轮注入。**有硬上限** ——
  偏好记忆无上限增长会持续挤压检索片段的预算，而检索片段才是回答质量的关键。
* **按需层（on demand）**：解读笔记、交付物 → 先搜后读。
* **事实层（on demand）**：知识库本身 → 按需召回。

准入标准：只有"几乎每轮都用得上"的才常驻。拿不准的一律放按需层 ——
宁可多一次检索，不要每轮白付 token。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config, MemoryConfig
from .guards import render_memory_block
from .store import Memory, MemoryStore


@dataclass
class MemoryBundle:
    """一轮对话要注入/检索到的记忆集合。"""

    resident: list[Memory] = field(default_factory=list)
    resident_tokens: int = 0
    retrieved: list[dict] = field(default_factory=list)
    retrieved_tokens: int = 0
    truncated: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    query: str = ""

    @property
    def total_tokens(self) -> int:
        return self.resident_tokens + self.retrieved_tokens

    def prompt_block(self) -> str:
        """渲染成可直接拼进提示词的文本（含强制约束说明）。"""
        return render_memory_block([m.to_dict() for m in self.resident])

    def read_refs(self) -> dict:
        """本轮读取了什么的清单（供不可变轮次 View 记录）。"""
        return {
            "resident": [m.id for m in self.resident],
            "retrieved": [r.get("id") or r.get("chunk_id") for r in self.retrieved],
            "truncated": [t.get("id") or t.get("label") for t in self.truncated],
        }

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "resident": [m.to_dict() for m in self.resident],
            "resident_tokens": self.resident_tokens,
            "retrieved": self.retrieved,
            "retrieved_tokens": self.retrieved_tokens,
            "truncated": self.truncated,
            "total_tokens": self.total_tokens,
            "notes": self.notes,
        }


class LayeredStrategy:
    """分层驻留 + 投影预算。"""

    def __init__(self, cfg: Config | MemoryConfig | None = None, *, kb: Any = None):
        if isinstance(cfg, Config):
            self.cfg = cfg.memory
        elif isinstance(cfg, MemoryConfig):
            self.cfg = cfg
        else:
            self.cfg = MemoryConfig()
        self.kb = kb
        self._meter = None

    # ------------------------------------------------------------------
    def _estimate(self, text: str) -> int:
        if self._meter is None:
            from ..context.meter import Meter

            self._meter = Meter()
        return self._meter.estimate_text(text)

    def assemble(
        self,
        store: MemoryStore,
        *,
        query: str = "",
        paper_ids: list[str] | None = None,
        include_resident: bool = True,
        include_retrieved: bool = True,
        retrieval_fn: Any = None,
    ) -> MemoryBundle:
        """组装一轮的记忆。

        ``retrieval_fn(query, limit) -> list[dict]`` 用于注入按需层检索
        （通常是知识库检索或交付物检索）。不提供则跳过按需层。
        """
        bundle = MemoryBundle(query=query)

        if include_resident:
            self._fill_resident(store, bundle)

        if include_retrieved and query and retrieval_fn is not None:
            self._fill_retrieved(bundle, retrieval_fn)

        return bundle

    # ------------------------------------------------------------------
    def _fill_resident(self, store: MemoryStore, bundle: MemoryBundle) -> None:
        """常驻层：按 priority 排序装入，**超预算即裁减**。"""
        items = store.resident()
        if not items:
            return

        budget = max(0, int(self.cfg.resident_token_budget))
        max_items = max(0, int(self.cfg.resident_max_items))
        items = sorted(items, key=lambda m: (m.priority, -len(m.value)))

        kept: list[Memory] = []
        used = 0
        for m in items:
            if len(kept) >= max_items:
                bundle.truncated.append({"id": m.id, "label": m.render(), "reason": "超出条数上限"})
                continue
            cost = self._estimate(m.render())
            if used + cost > budget:
                bundle.truncated.append({"id": m.id, "label": m.render(), "reason": "超出 token 预算"})
                continue
            kept.append(m)
            used += cost

        bundle.resident = kept
        bundle.resident_tokens = used
        if bundle.truncated:
            bundle.notes.append(
                f"常驻记忆有 {len(bundle.truncated)} 条因预算被裁减"
                f"（上限 {budget} token / {max_items} 条）。"
                "常驻层必须有硬上限，否则会持续挤压检索片段的预算。"
            )

    def _fill_retrieved(self, bundle: MemoryBundle, retrieval_fn: Any) -> None:
        """按需层：先搜后读，共享剩余预算。"""
        remaining = max(0, self.cfg.resident_token_budget) - bundle.resident_tokens
        # 按需层的预算是独立的 —— 它不该被常驻层吃掉。
        # 这里用一个显式的倍数关系，避免"常驻占满就没有按需"的退化。
        retrieve_budget = max(remaining, self.cfg.resident_token_budget * 2)

        try:
            items = retrieval_fn(bundle.query, self.cfg.retrieval_top_k) or []
        except Exception as e:  # noqa: BLE001 - 检索失败不该阻断本轮
            bundle.notes.append(f"按需层检索失败：{type(e).__name__}: {e}")
            return

        used = 0
        for it in items:
            text = it.get("text") or it.get("summary") or it.get("title") or ""
            cost = self._estimate(str(text))
            if used + cost > retrieve_budget:
                bundle.truncated.append({"label": str(text)[:40], "reason": "超出按需层预算"})
                continue
            bundle.retrieved.append(it)
            used += cost
        bundle.retrieved_tokens = used

    # ------------------------------------------------------------------
    def budget_report(self, bundle: MemoryBundle) -> str:
        cfg = self.cfg
        lines = [
            "记忆预算",
            f"  常驻层  {bundle.resident_tokens:>6} / {cfg.resident_token_budget} token"
            f"  （{len(bundle.resident)} / {cfg.resident_max_items} 条）",
            f"  按需层  {bundle.retrieved_tokens:>6} token  （{len(bundle.retrieved)} 项）",
            f"  合计    {bundle.total_tokens:>6} token",
        ]
        for n in bundle.notes:
            lines.append(f"  注：{n}")
        return "\n".join(lines)
