"""同源 sidecar 的 token 守卫与不授予跨源读取权限。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from litprism.sidecar import harden


def make_app(token: str | None) -> FastAPI:
    app = FastAPI()

    @app.get("/api/data")
    def data():
        return {"ok": True}

    @app.post("/api/data")
    def data_post():
        return {"created": True}

    @app.get("/api/ping")
    def ping():
        return {"pong": True}

    @app.get("/")
    def index():
        return {"page": "shell"}

    harden(app, token)
    return app


# ---------------------------------------------------------------- token 守卫
def test_api_without_token_is_rejected():
    c = TestClient(make_app("tk"))
    assert c.get("/api/data").status_code == 401


def test_api_with_token_passes():
    c = TestClient(make_app("tk"))
    r = c.get("/api/data", headers={"X-LitPrism-Token": "tk"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_wrong_token_is_rejected():
    c = TestClient(make_app("tk"))
    assert c.get("/api/data", headers={"X-LitPrism-Token": "nope"}).status_code == 401


def test_ping_is_open():
    # 外壳用它做存活探测，此时还没有 token 可用
    assert TestClient(make_app("tk")).get("/api/ping").status_code == 200


def test_page_shell_is_not_guarded():
    # token 保护数据不保护外壳：挡住 / 既不增加安全，又妨碍排障
    assert TestClient(make_app("tk")).get("/").status_code == 200


# ---------------------------------------------------------------- CORS
def test_cross_origin_preflight_without_token_is_rejected():
    """Electron 无需跨源预检，旧来源不再豁免 token 守卫。"""
    c = TestClient(make_app("tk"))
    r = c.options(
        "/api/data",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-litprism-token,content-type",
        },
    )
    assert r.status_code == 401
    assert "access-control-allow-origin" not in r.headers


def test_retired_origins_are_not_granted():
    c = TestClient(make_app("tk"))
    for origin in ('http://tauri.localhost', 'https://tauri.localhost', 'tauri://localhost'):
        r = c.options(
            "/api/data",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET", "X-LitPrism-Token": "tk"},
        )
        assert r.status_code == 405
        assert "access-control-allow-origin" not in r.headers, origin


def test_foreign_origin_is_not_granted():
    c = TestClient(make_app("tk"))
    r = c.get(
        "/api/data",
        headers={"Origin": "https://evil.example", "X-LitPrism-Token": "tk"},
    )
    # 请求本身照常处理（CORS 是浏览器侧的约束），但不发 allow-origin
    assert "access-control-allow-origin" not in r.headers


def test_no_token_mode_has_no_cors():
    """裸 serve 模式绝不能带 CORS —— 否则任意网页可直读知识库。"""
    c = TestClient(make_app(None))
    r = c.get("/api/data", headers={"Origin": "http://tauri.localhost"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
