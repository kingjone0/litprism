"""模型用量与缓存命中归集。

为什么要单独归一
----------------
三家提供商的 usage 字段名完全不同：

| 提供商 | 输入 | 输出 | 缓存命中 |
|---|---|---|---|
| OpenAI | `prompt_tokens` | `completion_tokens` | `prompt_tokens_details.cached_tokens` |
| DeepSeek | `prompt_tokens` | `completion_tokens` | `prompt_cache_hit_tokens` |
| Anthropic | `input_tokens` | `output_tokens` | `cache_read_input_tokens` |

直接把原始 dict 透出去，界面上就得写三套判断，而且很容易某家漏看。

**缓存命中率是"上下文是否真的固定"的体检指标。** 我们为了命中前缀缓存
做了布局反转（原文在前、指令在后），如果实测命中率接近 0，说明这条设计没生效 ——
这不是成本问题，是设计验证问题。所以必须能测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _pick(d: dict, *keys: str) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(v)
    return 0


def normalize_usage(usage: dict | None) -> dict:
    """把任意提供商的 usage 归一成统一结构。

    缺失的字段填 0，不猜 —— 猜出来的数字比没有更危险。
    """
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "cache_hit": 0, "cache_miss": 0, "cached_ratio": None}

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}

    total_in = _pick(usage, "prompt_tokens", "input_tokens")
    out = _pick(usage, "completion_tokens", "output_tokens")

    hit = _pick(usage, "prompt_cache_hit_tokens", "cache_read_input_tokens")
    if not hit:
        hit = _pick(details, "cached_tokens", "cache_hit_tokens")

    # ⚠️ 必须先判断"有没有报缓存字段"，再决定要不要推算 miss。
    # 反过来写的话，没报缓存的提供商会把 miss 推成整个输入量，
    # 于是算出"命中 0%" —— 把"不知道"说成了"没命中"。
    reported = bool(
        hit
        or "prompt_cache_miss_tokens" in usage
        or "cache_creation_input_tokens" in usage
        or "prompt_cache_hit_tokens" in usage
        or "cache_read_input_tokens" in usage
        or details.get("cached_tokens") is not None
        or details.get("cache_hit_tokens") is not None
    )

    miss = _pick(usage, "prompt_cache_miss_tokens", "cache_creation_input_tokens")
    if not miss and reported and total_in:
        miss = max(0, total_in - hit)

    ratio = (hit / (hit + miss)) if reported and (hit + miss) > 0 else None

    return {
        "input": total_in,
        "output": out,
        "cache_hit": hit,
        "cache_miss": miss,
        "cached_ratio": ratio,
    }


@dataclass
class UsageTracker:
    """累计一次解读里所有调用的用量。"""

    calls: int = 0
    input: int = 0
    output: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    unknown_cache_calls: int = 0
    unknown_usage_calls: int = 0
    failed_calls: int = 0
    per_task: dict[str, dict] = field(default_factory=dict)

    def add(self, usage: dict | None, *, task: str = "unknown", failed: bool = False) -> None:
        n = normalize_usage(usage)
        self.calls += 1
        self.failed_calls += int(failed)
        known = isinstance(usage, dict) and any(k in usage for k in ("prompt_tokens", "input_tokens")) and any(k in usage for k in ("completion_tokens", "output_tokens"))
        self.unknown_usage_calls += int(not known)
        self.input += n["input"]
        self.output += n["output"]
        self.cache_hit += n["cache_hit"]
        self.cache_miss += n["cache_miss"]
        if n["cached_ratio"] is None:
            self.unknown_cache_calls += 1

        slot = self.per_task.setdefault(task, {"calls": 0, "input": 0, "output": 0,
                                               "cache_hit": 0, "cache_miss": 0, "failed_calls": 0, "unknown_usage_calls": 0})
        slot["calls"] += 1
        slot["failed_calls"] += int(failed)
        slot["unknown_usage_calls"] += int(not known)
        slot["input"] += n["input"]
        slot["output"] += n["output"]
        slot["cache_hit"] += n["cache_hit"]
        slot["cache_miss"] += n["cache_miss"]

    def add_from(self, client: Any, *, task: str = "unknown") -> None:
        """从客户端读 ``last_usage`` 并累计。客户端没有该属性时按未知处理。"""
        self.add(getattr(client, "last_usage", None), task=task)

    def merge(self, other):
        for key in ('calls', 'input', 'output', 'cache_hit', 'cache_miss',
                    'unknown_cache_calls', 'unknown_usage_calls', 'failed_calls'):
            setattr(self, key, getattr(self, key)+getattr(other, key))
        for task, values in other.per_task.items():
            slot = self.per_task.setdefault(task, {key: 0 for key in values})
            for key, value in values.items():
                slot[key] = slot.get(key, 0)+value

    @property
    def cached_ratio(self) -> float | None:
        total = self.cache_hit + self.cache_miss
        return (self.cache_hit / total) if total > 0 else None

    def to_dict(self) -> dict:
        ratio = self.cached_ratio
        return {
            "calls": self.calls,
            "input": self.input,
            "output": self.output,
            "cache_hit": self.cache_hit,
            "cache_miss": self.cache_miss,
            "cached_ratio": ratio,
            "unknown_cache_calls": self.unknown_cache_calls,
            "unknown_usage_calls": self.unknown_usage_calls,
            "failed_calls": self.failed_calls,
            "accounting_scope": "logical_calls_reported_usage",
            "complete": not self.unknown_usage_calls,
            "per_task": dict(self.per_task),
            "note": (f"{self.unknown_usage_calls} 次调用未返回用量；以下为已报告用量，非完整计费。 " if self.unknown_usage_calls else "") + (
                "提供商未报告缓存字段，命中率不可知" if ratio is None
                else f"缓存命中 {ratio*100:.0f}%（{self.cache_hit:,} / {self.cache_hit + self.cache_miss:,}）"
                + (f"；仅含有缓存上报的调用，另有 {self.unknown_cache_calls} 次未知" if self.unknown_cache_calls else "")
            ),
        }


def tracked_json(client, messages, *, task, tracker=None):
    """Record each logical call once, including truncation and failed calls.

    Reset last_usage to avoid charging a previous response to a failed request.
    Transport attempts without provider usage remain unknown, never guessed free.
    """
    if tracker is None:
        return client.chat_json(messages, task=task)
    previous = getattr(client, 'last_usage', None)
    reset = False
    try:
        client.last_usage = None
        reset = True
    except (AttributeError, TypeError):
        pass
    failed = True
    try:
        client.last_call_replayed = False
    except (AttributeError, TypeError):
        pass
    try:
        result = client.chat_json(messages, task=task)
        failed = False
        return result
    finally:
        usage = getattr(client, 'last_usage', None)
        if not reset and usage is previous:
            usage = None
        if not getattr(client, 'last_call_replayed', False) and not getattr(client, 'last_call_not_sent', False):
            tracker.add(usage, task=task, failed=failed)


class TrackedClient:
    """Count query expansion and answer attempts through the same logical ledger."""

    def __init__(self, client, tracker):
        self.client, self.tracker = client, tracker

    def __getattr__(self, name):
        return getattr(self.client, name)

    def chat_json(self, messages, *, task='unknown', **kwargs):
        # Production query/QA calls currently use this signature; do not invent
        # transport-attempt counts from a client's internal retry loop.
        if kwargs:
            raise TypeError('TrackedClient only accepts logical task calls')
        return tracked_json(self.client, messages, task=task, tracker=self.tracker)
