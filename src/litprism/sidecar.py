"""Tauri sidecar 入口。

外壳（Tauri/Rust）与本进程的握手协议::

    1. 外壳以子进程方式启动本程序（可带 --port 指定端口）；
    2. 本程序完成初始化后向 **stdout** 打一行握手：

           LITPRISM_READY {"port": 8766, "token": "<一次性随机串>", "pid": 1234}

    3. 外壳读取该行，把 token 通过 IPC 注入前端；
    4. 前端所有 /api 请求带 ``X-LitPrism-Token`` 头。

为什么需要 token：端口在 127.0.0.1 上，本机的其他进程也能访问。
一次性 token 保证「只有通过外壳启动的前端」能用这套 API ——
这是本地单机应用的最小访问控制。

握手行之前、之后的 stdout 内容（uvicorn 日志等）外壳都会忽略。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="litprism-sidecar")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 = 自动挑选")
    parser.add_argument("--no-token", action="store_true", help="跳过 token 校验（仅调试）")
    args = parser.parse_args()

    # stdout 在握手前只允许握手行通过 —— uvicorn 的日志强制走 stderr
    from .config import load_config
    from .web.server import create_app

    cfg = load_config()
    app = create_app(cfg)

    token = "" if args.no_token else secrets.token_urlsafe(24)
    if token:
        app.state.api_token = token

        @app.middleware("http")
        async def _token_guard(request, call_next):  # noqa: ANN001
            # 只守 /api —— token 要保护的是**数据**，不是界面外壳。
            # 静态资源与页面骨架（/、/static/*）不设防：它们不含任何知识库内容，
            # 挡住它们既不增加安全性，又让打包版无法在浏览器里排障。
            path = request.url.path
            if not path.startswith("/api") or path == "/api/ping":
                return await call_next(request)
            provided = request.headers.get("x-litprism-token", "")
            if provided != token:
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=401, content={"error": "无效的访问令牌"})
            return await call_next(request)

    host = "127.0.0.1"

    # 先选端口再打握手行，保证握手里的端口可用
    import socket

    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]

    handshake = json.dumps(
        {"port": port, "token": token, "pid": os.getpid()}, ensure_ascii=False
    )
    print(f"LITPRISM_READY {handshake}", flush=True)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
