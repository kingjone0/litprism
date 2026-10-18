"""模型窗口解析测试。

背景：`context.window_tokens` 原本是全局 32000，换 128k/1M 的模型不会自动放大，
长论文永远进不了「全文驻留」模式 —— 缓存前缀根本建立不起来。

解析顺序必须是：**模型级 → 提供商级 → 全局配置**，
且"没配"与"配了非法值"都不能被悄悄当成默认窗口。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.providers import (  # noqa: E402
    ProviderStore,
    _as_int,
    apply_active_window,
    guess_window,
)


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    yield cfg, kb
    kb.close()


class TestGuess:
    def test_known_models(self):
        assert guess_window("gpt-4o") == 128_000
        assert guess_window("gpt-4.1-preview") == 1_000_000
        assert guess_window("deepseek-chat") == 128_000
        assert guess_window("claude-sonnet-4-5") == 200_000
        assert guess_window("qwen-long") == 1_000_000

    def test_unknown_returns_none(self):
        assert guess_window("my-private-model") is None
        assert guess_window("") is None


class TestAsInt:
    def test_valid(self):
        assert _as_int(128000) == 128000
        assert _as_int("128000") == 128000

    def test_blank_is_none(self):
        assert _as_int(None) is None
        assert _as_int("") is None

    def test_invalid_is_none(self):
        """⭐ 非法值不能被当成 0 或默认窗口 —— 那会静默改变行为。"""
        assert _as_int("abc") is None
        assert _as_int(0) is None
        assert _as_int(-5) is None


class TestResolution:
    def test_model_level_wins(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [
            {"model_id": "a", "enabled": True, "window_tokens": 128000},
            {"model_id": "b", "enabled": True, "window_tokens": 1_000_000},
        ])
        store.set_active_model(p.id, "b")
        assert store.active_window() == 1_000_000

    def test_provider_level_fallback(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "a", "enabled": True}])
        with kb.tx() as con:
            con.execute("UPDATE providers SET window_tokens = ? WHERE id = ?", (64000, p.id))
        store.set_active_model(p.id, "a")
        assert store.active_window() == 64000

    def test_none_when_unconfigured(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "a", "enabled": True}])
        store.set_active_model(p.id, "a")
        assert store.active_window() is None

    def test_no_active_provider(self, env):
        cfg, kb = env
        assert ProviderStore(kb, cfg.data_path).active_window() is None


class TestApply:
    def test_applies_model_window(self, env):
        cfg, kb = env
        cfg.context.window_tokens = 32000
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "a", "enabled": True, "window_tokens": 128000}])
        store.set_active_model(p.id, "a")

        info = apply_active_window(cfg, kb)
        assert cfg.context.window_tokens == 128000
        assert info["source"] == "model"
        assert info["configured"] == 32000
        assert info["available"] == 128000 - cfg.context.output_reserve_tokens

    def test_falls_back_to_config(self, env):
        cfg, kb = env
        cfg.context.window_tokens = 32000
        info = apply_active_window(cfg, kb)
        assert cfg.context.window_tokens == 32000
        assert info["source"] == "config"

    def test_survives_broken_provider_table(self, env):
        """⭐ 读不到窗口不能变成"分析失败"。"""
        cfg, kb = env
        cfg.context.window_tokens = 32000
        kb._con.executescript("DROP TABLE provider_models;")
        info = apply_active_window(cfg, kb)
        assert info["window"] == 32000

    def test_idempotent(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "a", "enabled": True, "window_tokens": 200000}])
        store.set_active_model(p.id, "a")
        a = apply_active_window(cfg, kb)
        b = apply_active_window(cfg, kb)
        assert a["window"] == b["window"] == 200000


class TestMigration:
    def test_columns_exist(self, env):
        cfg, kb = env
        cols = {r[1] for r in kb._con.execute("PRAGMA table_info(provider_models)").fetchall()}
        assert "window_tokens" in cols
        cols = {r[1] for r in kb._con.execute("PRAGMA table_info(providers)").fetchall()}
        assert "window_tokens" in cols

    def test_persisted_through_set_models(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "a", "enabled": True, "window_tokens": 300000}])
        assert store.get(p.id).models[0].window_tokens == 300000
