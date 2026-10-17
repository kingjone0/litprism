"""把 Web 前端整理成 Tauri 可内嵌的目录结构。

为什么需要这一步：页面里引用的是绝对路径 ``/static/app.js``。
FastAPI 把 static 挂在 ``/static``；而 Tauri 的 ``frontendDist`` 根目录就是页面根，
若直接指向 ``src/litprism/web/static``，``/static/app.js`` 会解析成
``<根>/static/app.js`` —— 文件其实在根下，必然 404。

所以生成一个镜像目录，保持 ``/static`` 这一层：

    src-tauri/ui/
    ├── index.html          ← 真实界面（页面骨架，从 web/static 拷）
    └── static/*.css|js     ← 资源，与 FastAPI 的挂载路径一致

``ui/`` 是生成物（gitignore）。
用法：``python packaging/prepare_ui.py``（``packaging/build.py`` 会自动调用）
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "litprism" / "web" / "static"
OUT = ROOT / "src-tauri" / "ui"

REQUIRED = ("index.html", "app.css", "app.js", "lit-ui.css", "lit-ui.js")


def main() -> int:
    missing = [n for n in REQUIRED if not (SRC / n).exists()]
    if missing:
        print(f"缺少前端文件：{', '.join(missing)}")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True)

    shutil.copy2(SRC / "index.html", OUT / "index.html")
    for f in sorted(SRC.iterdir()):
        # 资源全量拷进 static/，含将来新增的字体、图标
        if f.is_file() and f.name != "index.html" and f.suffix in {".css", ".js"}:
            shutil.copy2(f, OUT / "static" / f.name)

    files = sorted(p.relative_to(OUT).as_posix() for p in OUT.rglob("*") if p.is_file())
    print(f"已生成 {OUT}")
    for f in files:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
