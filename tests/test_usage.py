"""用量与缓存命中归集的测试。

关键立场：**"不知道"和"0%"必须区分。**
提供商没报缓存字段时给 None，不能给 0 —— 0% 会被读成"缓存没生效"，
而实际上可能是"这家根本不报"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.usage import UsageTracker, normalize_usage  # noqa: E402


class TestNormalize:
    def test_openai_shape(self):
        n = normalize_usage({
            "prompt_tokens": 1000, "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 800},
        })
        assert n["input"] == 1000 and n["output"] == 200
        assert n["cache_hit"] == 800 and n["cache_miss"] == 200
        assert n["cached_ratio"] == 0.8

    def test_deepseek_shape(self):
        n = normalize_usage({
            "prompt_tokens": 1000, "completion_tokens": 200,
            "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
        })
        assert n["cache_hit"] == 900 and n["cache_miss"] == 100
        assert n["cached_ratio"] == 0.9

    def test_anthropic_shape(self):
        n = normalize_usage({
            "input_tokens": 500, "output_tokens": 100,
            "cache_read_input_tokens": 400, "cache_creation_input_tokens": 100,
        })
        assert n["input"] == 500 and n["output"] == 100
        assert n["cache_hit"] == 400 and n["cache_miss"] == 100
        assert n["cached_ratio"] == 0.8

    def test_no_cache_fields_gives_none(self):
        """⭐ 没报缓存 → None，不是 0。"""
        n = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 200})
        assert n["cached_ratio"] is None
        assert n["cache_hit"] == 0

    def test_empty_usage(self):
        n = normalize_usage(None)
        assert n == {"input": 0, "output": 0, "cache_hit": 0, "cache_miss": 0, "cached_ratio": None}

    def test_malformed_details(self):
        n = normalize_usage({"prompt_tokens": 10, "prompt_tokens_details": "oops"})
        assert n["input"] == 10 and n["cached_ratio"] is None


class TestTracker:
    def test_accumulates(self):
        t = UsageTracker()
        t.add({"prompt_tokens": 100, "completion_tokens": 10,
               "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20})
        t.add({"prompt_tokens": 200, "completion_tokens": 20,
               "prompt_cache_hit_tokens": 150, "prompt_cache_miss_tokens": 50})
        assert t.calls == 2 and t.input == 300 and t.output == 30
        assert t.cache_hit == 230 and t.cache_miss == 70
        assert abs(t.cached_ratio - 230 / 300) < 1e-9

    def test_unknown_cache_counted(self):
        t = UsageTracker()
        t.add({"prompt_tokens": 100, "completion_tokens": 10})
        t.add({"prompt_tokens": 100, "completion_tokens": 10,
               "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10})
        assert t.unknown_cache_calls == 1

    def test_per_task_breakdown(self):
        t = UsageTracker()
        t.add({"prompt_tokens": 100, "completion_tokens": 10}, task="extract")
        t.add({"prompt_tokens": 50, "completion_tokens": 5}, task="concepts")
        d = t.to_dict()["per_task"]
        assert d["extract"]["calls"] == 1 and d["concepts"]["calls"] == 1

    def test_add_from_client(self):
        class Client:
            last_usage = {"prompt_tokens": 10, "completion_tokens": 2,
                          "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2}

        t = UsageTracker()
        t.add_from(Client(), task="extract")
        assert t.calls == 1 and t.cache_hit == 8

    def test_add_from_client_without_usage(self):
        class Client:
            pass

        t = UsageTracker()
        t.add_from(Client())
        assert t.calls == 1 and t.unknown_cache_calls == 1

    def test_note_says_unknown_when_unreported(self):
        t = UsageTracker()
        t.add({"prompt_tokens": 10})
        assert "不可知" in t.to_dict()["note"]

    def test_note_reports_ratio(self):
        t = UsageTracker()
        t.add({"prompt_tokens": 100, "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10})
        assert "90%" in t.to_dict()["note"]
