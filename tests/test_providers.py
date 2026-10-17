"""模型提供商与密钥存储测试。

重点：
1. **API Key 不落配置文件** —— 只存 ref，明文进加密库；
2. **模型目录是可选项清单** —— enabled 控制是否暴露给对话；
3. **拉取失败的消息要可操作** —— 认证失败 / 无该接口 / 网络不通，三种原因分开说。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.providers import (  # noqa: E402
    PRESETS,
    PROTOCOLS,
    ProviderError,
    ProviderStore,
    format_providers,
)
from litprism.secrets import SecretStore  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    yield cfg, kb
    kb.close()


# ===========================================================================
# 密钥存储
# ===========================================================================
class TestSecretStore:
    def test_set_get_roundtrip(self, tmp_path):
        s = SecretStore(tmp_path)
        ref = s.set("test", "sk-abcdef1234567890")
        assert s.get(ref) == "sk-abcdef1234567890"

    def test_ref_does_not_contain_plaintext(self, tmp_path):
        """⭐ ref 会被写进数据库 —— 绝不能带明文。"""
        s = SecretStore(tmp_path)
        ref = s.set("test", "sk-super-secret-value")
        assert "sk-super-secret-value" not in ref

    def test_file_does_not_contain_plaintext(self, tmp_path):
        """⭐ 落盘文件里也不能出现明文。"""
        s = SecretStore(tmp_path)
        s.set("test", "sk-super-secret-value")
        raw = s.path.read_text(encoding="utf-8")
        assert "sk-super-secret-value" not in raw

    def test_mask(self, tmp_path):
        s = SecretStore(tmp_path)
        ref = s.set("test", "sk-abcdef1234567890")
        mask = s.mask(ref)
        assert "abcdef1234567890" not in mask
        assert "sk-a" in mask and "7890" in mask

    def test_mask_unconfigured(self, tmp_path):
        s = SecretStore(tmp_path)
        assert s.mask(None) == "未配置"
        assert s.mask("nope") == "未配置"

    def test_delete(self, tmp_path):
        s = SecretStore(tmp_path)
        ref = s.set("test", "value")
        assert s.delete(ref)
        assert s.get(ref) is None
        assert not s.delete(ref)

    def test_backend_reported(self, tmp_path):
        s = SecretStore(tmp_path)
        info = s.describe()
        assert info["backend"] in ("dpapi", "aesgcm", "weak")
        assert info["note"]

    def test_persistence_across_instances(self, tmp_path):
        ref = SecretStore(tmp_path).set("test", "value123")
        assert SecretStore(tmp_path).get(ref) == "value123"

    def test_empty_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            SecretStore(tmp_path).set("test", "")


# ===========================================================================
# 提供商
# ===========================================================================
class TestProviderCRUD:
    def test_create_with_preset_seeds_models(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="OpenAI", base_url="https://api.openai.com/v1", preset="openai")
        assert p.name == "OpenAI"
        assert p.protocol == "openai"
        assert [m.model_id for m in p.models] == ["gpt-4o", "gpt-4o-mini"]

    def test_create_without_preset_has_no_models(self, env):
        cfg, kb = env
        p = ProviderStore(kb, cfg.data_path).create(name="自定义", base_url="https://x/v1")
        assert p.models == []
        assert p.is_custom is True

    def test_key_stored_as_ref_not_plaintext(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="sk-secret-123456")
        row = kb._con.execute("SELECT api_key_ref FROM providers WHERE id = ?", (p.id,)).fetchone()
        assert row["api_key_ref"], "应存 ref"
        assert "sk-secret-123456" not in row["api_key_ref"]
        assert store.secrets.get(row["api_key_ref"]) == "sk-secret-123456"
        assert p.api_key_mask != "未配置"

    def test_empty_name_rejected(self, env):
        cfg, kb = env
        with pytest.raises(ProviderError, match="显示名称"):
            ProviderStore(kb, cfg.data_path).create(name="", base_url="https://x/v1")

    def test_empty_url_rejected(self, env):
        cfg, kb = env
        with pytest.raises(ProviderError, match="API 地址"):
            ProviderStore(kb, cfg.data_path).create(name="X", base_url="")

    def test_bad_protocol_rejected(self, env):
        cfg, kb = env
        with pytest.raises(ProviderError, match="协议"):
            ProviderStore(kb, cfg.data_path).create(name="X", base_url="https://x/v1", protocol="grpc")

    def test_update_keeps_key_when_blank(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="sk-abc123456789")
        before = store.secrets.get(p.api_key_ref)
        store.update(p.id, api_key="")
        after = store.get(p.id)
        assert store.secrets.get(after.api_key_ref) == before

    def test_update_replaces_key(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="old-key-123456")
        store.update(p.id, api_key="new-key-654321")
        q = store.get(p.id)
        assert store.secrets.get(q.api_key_ref) == "new-key-654321"

    def test_delete_removes_key_and_models(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="k-12345678", preset="openai")
        ref = p.api_key_ref
        assert store.delete(p.id)
        assert store.secrets.get(ref) is None
        assert kb._con.execute(
            "SELECT COUNT(*) n FROM provider_models WHERE provider_id = ?", (p.id,)
        ).fetchone()["n"] == 0


class TestModelDirectory:
    def test_set_models_replaces(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [
            {"model_id": "a", "display_name": "A", "enabled": True},
            {"model_id": "b", "display_name": "B", "enabled": False},
        ])
        q = store.get(p.id)
        assert [m.model_id for m in q.models] == ["a", "b"]
        assert [m.model_id for m in q.models if m.enabled] == ["a"]

    def test_duplicates_and_blanks_dropped(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        n = store.set_models(p.id, [
            {"model_id": "a"}, {"model_id": "a"}, {"model_id": ""}, {"model_id": "b"},
        ])
        assert n == 2

    def test_chat_options_only_enabled(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [
            {"model_id": "a", "display_name": "A", "enabled": True},
            {"model_id": "b", "display_name": "B", "enabled": False},
        ])
        opts = store.chat_options()
        assert [o["model_id"] for o in opts] == ["a"]
        assert opts[0]["label"] == "X · A"


class TestActiveModel:
    def test_set_and_get(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", preset="openai")
        assert store.set_active_model(p.id, "gpt-4o-mini")
        active, model = store.get_active()
        assert active.id == p.id and model == "gpt-4o-mini"

    def test_get_active_without_config(self, env):
        cfg, kb = env
        active, model = ProviderStore(kb, cfg.data_path).get_active()
        assert active is None and model is None

    def test_fallback_to_first_enabled(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")
        store.set_models(p.id, [{"model_id": "m1", "enabled": True}])
        with kb.tx() as con:
            con.execute("UPDATE providers SET chat_model = NULL WHERE id = ?", (p.id,))
        store.set_active_model(p.id, "m1")
        with kb.tx() as con:
            con.execute("UPDATE providers SET chat_model = NULL WHERE id = ?", (p.id,))
        active, model = store.get_active()
        assert model == "m1"


class TestPresets:
    def test_presets_shape(self):
        assert len(PRESETS) >= 5
        for p in PRESETS:
            assert p["key"] and p["name"] and p["base_url"]
            assert p["protocol"] in PROTOCOLS

    def test_protocols(self):
        assert set(PROTOCOLS) == {"openai", "anthropic"}
        assert "OpenAI" in PROTOCOLS["openai"]
        assert "Anthropic" in PROTOCOLS["anthropic"]

    def test_format(self, env):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        assert format_providers([]) == "（未配置任何提供商）"
        store.create(name="X", base_url="https://x/v1", preset="openai")
        text = format_providers(store.list())
        assert "X" in text and "openai" in text


class TestFetchModels:
    def test_unknown_provider(self, env):
        cfg, kb = env
        with pytest.raises(ProviderError, match="不存在"):
            ProviderStore(kb, cfg.data_path).fetch_models("pv_nope")

    def test_connection_error_message_is_actionable(self, env, monkeypatch):
        """⭐ 连不上要说清「检查地址/网络」，而不是抛原始异常。"""
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="http://127.0.0.1:1/v1")

        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(ProviderError) as ei:
            store.fetch_models(p.id)
        msg = str(ei.value)
        assert "无法连接" in msg
        assert "检查" in msg

    def test_auth_error_message(self, env, monkeypatch):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="bad-key-12345")

        import requests

        class R:
            status_code = 401
            text = "unauthorized"

        monkeypatch.setattr(requests, "get", lambda *a, **k: R())
        with pytest.raises(ProviderError) as ei:
            store.fetch_models(p.id)
        assert "认证失败" in str(ei.value)

    def test_404_message_suggests_manual(self, env, monkeypatch):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")

        import requests

        class R:
            status_code = 404
            text = "not found"

        monkeypatch.setattr(requests, "get", lambda *a, **k: R())
        with pytest.raises(ProviderError) as ei:
            store.fetch_models(p.id)
        assert "手动添加" in str(ei.value)

    def test_success_parses_openai_shape(self, env, monkeypatch):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1", api_key="k-12345678")

        import requests

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"id": "gpt-4o"}, {"id": "o3-mini"}]}

        monkeypatch.setattr(requests, "get", lambda *a, **k: R())
        items = store.fetch_models(p.id)
        assert [i["model_id"] for i in items] == ["gpt-4o", "o3-mini"]
        assert store.get(p.id).status == "ok"

    def test_test_endpoint_reports_failure(self, env, monkeypatch):
        cfg, kb = env
        store = ProviderStore(kb, cfg.data_path)
        p = store.create(name="X", base_url="https://x/v1")

        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        r = store.test(p.id)
        assert r["ok"] is False and r["message"]
        assert store.get(p.id).status == "failed"
