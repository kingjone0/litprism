"""一行重建发布所需产物：``python packaging/build.py``

只剩一步：**PyInstaller 打 sidecar**，产物固定落 ``packaging/dist/litprism-sidecar.exe``。

为什么不再顺带准备 UI 或复制到别处：
- 桌面壳是 Electron（``desktop-electron/main.js``），窗口直接加载 sidecar 提供的页面，
  不需要单独的 splash 目录；

为什么不直接调 pyinstaller：默认落点是**当前工作目录**的 ``dist/``，
跑一次会多出一份 140 MB 副本，还很容易验证到旧的那一个。

用法：``python packaging/build.py``
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
SPEC = PKG / "litprism-sidecar.spec"
DIST = PKG / "dist"
WORK = PKG / "build"


def build_sidecar() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller：python -m pip install pyinstaller", file=sys.stderr)
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller", str(SPEC),
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
    ]
    print("$", " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc != 0:
        return rc

    exe = DIST / "litprism-sidecar.exe"
    if not exe.exists():
        print("未找到 sidecar 产物，构建可能失败", file=sys.stderr)
        return 1
    print(f"\nsidecar：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    shutil.rmtree(WORK, ignore_errors=True)  # 中间产物不留
    return 0


def main() -> int:
    rc = build_sidecar()
    if rc != 0:
        return rc
    print("\n完成。桌面壳从源码运行：node_modules/electron/dist/electron.exe desktop-electron/main.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
