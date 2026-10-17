"""路径根解析测试（打包 frozen 模式的核心约束）。

背景：PyInstaller onefile 下 ``__file__`` 位于 ``sys._MEIPASS``（临时解包目录），
若把它当项目根，知识库会建在 ``%TEMP%\\data`` 里、用户配置永远生成不出来。
这组测试锁死「读资源走 BUNDLE_ROOT，写数据走 APP_ROOT」这条规则。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litprism import config as cfgmod
from litprism.config import ensure_user_config, patch_config, resolve_roots


# ---------------------------------------------------------------------------
# resolve_roots：纯函数，不依赖真实 frozen 环境
# ---------------------------------------------------------------------------
def test_source_mode_uses_project_root_for_both():
    src = Path("/proj")
    bundle, app = resolve_roots(frozen=False, source_root=src, localappdata="/ignored")
    assert bundle == src and app == src


def test_frozen_splits_readonly_and_writable():
    bundle, app = resolve_roots(
        frozen=True, meipass="/tmp/_MEI123", localappdata="C:/Users/u/AppData/Local"
    )
    assert bundle == Path("/tmp/_MEI123")
    assert app == Path("C:/Users/u/AppData/Local") / "LitPrism"


def test_frozen_without_localappdata_falls_back_to_home():
    bundle, app = resolve_roots(frozen=True, meipass="/tmp/_MEI", home="/home/u")
    assert bundle == Path("/tmp/_MEI")
    assert app == Path("/home/u") / ".litprism"


def test_frozen_without_meipass_uses_exe_dir(monkeypatch):
    # onedir 模式下没有 _MEIPASS，资源就在 exe 旁边
    bundle, _ = resolve_roots(frozen=True, meipass=None, localappdata="C:/L")
    assert bundle == Path(cfgmod.sys.executable).parent


def test_litprism_home_overrides_writable_root(monkeypatch):
    monkeypatch.setenv("LITPRISM_HOME", "/portable/home")
    _, app = resolve_roots(frozen=True, meipass="/tmp/_MEI", localappdata="C:/L")
    assert app == Path("/portable/home")


def test_litprism_home_overrides_in_source_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LITPRISM_HOME", str(tmp_path))
    bundle, app = resolve_roots(frozen=False, source_root=Path("/proj"))
    assert bundle == Path("/proj") and app == tmp_path


# ---------------------------------------------------------------------------
# 用户配置必须落在可写根，模板必须从只读根读
# ---------------------------------------------------------------------------
@pytest.fixture()
def fake_roots(monkeypatch, tmp_path):
    """把两种根指向两个独立目录：bundle 只读、app 可写。"""
    bundle = tmp_path / "bundle"
    app = tmp_path / "app"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config" / "config.example.yaml").write_text(
        "app:\n  name: 模板\nllm:\n  provider: mock\n", encoding="utf-8"
    )
    monkeypatch.setattr(cfgmod, "BUNDLE_ROOT", bundle)
    monkeypatch.setattr(cfgmod, "APP_ROOT", app)
    return bundle, app


def test_user_config_copied_into_writable_root(fake_roots):
    bundle, app = fake_roots
    user = ensure_user_config()
    assert user == app / "config" / "config.yaml"
    assert user.exists()
    # 模板本身不被改写（只读根保持干净）
    assert "模板" in (bundle / "config" / "config.example.yaml").read_text(encoding="utf-8")


def test_user_config_is_idempotent(fake_roots):
    _, app = fake_roots
    first = ensure_user_config()
    first.write_text("app:\n  name: 用户改过的\n", encoding="utf-8")
    second = ensure_user_config()
    assert second == first
    # 已存在的用户配置绝不能被模板覆盖回去
    assert "用户改过的" in second.read_text(encoding="utf-8")


def test_missing_template_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "BUNDLE_ROOT", tmp_path / "empty")
    monkeypatch.setattr(cfgmod, "APP_ROOT", tmp_path / "app2")
    assert ensure_user_config() is None


def test_patch_config_never_writes_bundle_template(fake_roots):
    bundle, app = fake_roots
    example = bundle / "config" / "config.example.yaml"
    before = example.read_bytes()

    patch_config(example, {"app": {"name": "被改的名字"}})

    assert example.read_bytes() == before, "模板绝不能被设置界面改写"
    user = app / "config" / "config.yaml"
    assert user.exists() and "被改的名字" in user.read_text(encoding="utf-8")
