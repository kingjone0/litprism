"""一行重建 sidecar：``python packaging/build.py``

为什么需要这个脚本：``pyinstaller packaging/litprism-sidecar.spec`` 的产物
默认落在 **当前工作目录** 的 ``dist/``，而不是 ``packaging/dist/``。
不带参数跑一次就会多出一份 145MB 的副本，还容易验证到旧的那一个。
这里把 distpath / workpath 固定下来，谁跑都一样。
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


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller：python -m pip install pyinstaller", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
    ]
    print("$", " ".join(cmd), flush=True)
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    if rc != 0:
        return rc

    exe = DIST / "litprism-sidecar.exe"
    if exe.exists():
        print(f"\n产物：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
        shutil.rmtree(WORK, ignore_errors=True)   # 中间产物不留
    else:
        print("未找到产物，构建可能失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
