"""Electron sidecar 入口。

外壳与本进程的握手协议::

    1. 外壳以子进程方式启动本程序（可带 --port 指定端口）；
    2. 本程序完成初始化后向 **stdout** 打一行握手：

           LITPRISM_READY {"port": 8766, "token": "<一次性随机串>", "pid": 1234}

    3. 外壳读取该行，加载同源页面 ``http://127.0.0.1:<port>/#t=<token>``；
    4. 前端所有 /api 请求带 ``X-LitPrism-Token`` 头。

为什么需要 token：端口在 127.0.0.1 上，本机的其他进程也能访问。
持有本次 token 的请求才可访问数据，这是本地单机应用的最小访问控制。
Electron 直接加载后台提供的同源页面，不需要跨源放行。

握手行之前、之后的 stdout 内容（uvicorn 日志等）外壳都会忽略。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys


def harden(app, token: str | None) -> None:  # noqa: ANN001
    """给同源应用装上 token 守卫，不授予跨源读取权限。

    ``token`` 为空表示不设防（等价于裸 serve 模式）。
    """
    if not token:
        return

    from fastapi.responses import JSONResponse

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
            return JSONResponse(status_code=401, content={"error": "无效的访问令牌"})
        return await call_next(request)

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
    harden(app, token)

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

    # 日志必须全部走 stderr：stdout 是握手管道，外壳只挑 LITPRISM_READY 那一行，其余丢弃。
    # uvicorn 默认把**访问日志写到 stdout** —— 那样排障时会把「前端其实请求了」
    # 误判成「前端从没请求过后台」（本项目踩过这个坑）。
    import copy

    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    for handler in log_config["handlers"].values():
        handler["stream"] = "ext://sys.stderr"

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=os.environ.get("LITPRISM_LOG_LEVEL", "warning"),
            # 默认关访问日志：外壳模式下它只会污染 stderr。
            # 排查「前端到底有没有请求到后台」时置 1 —— 这是唯一能证明链路通了的证据。
            access_log=bool(os.environ.get("LITPRISM_ACCESS_LOG")),
            log_config=log_config,
        )
    )
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
