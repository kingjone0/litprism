"""一行重建发布所需产物：``python packaging/build.py``

两步：

1. **PyInstaller 打 sidecar** —— 产物固定落 ``packaging/dist/``。
   为什么不直接调 pyinstaller：默认落点是 **当前工作目录** 的 ``dist/``，
   跑一次会多出一份 140MB 副本，还很容易验证到旧的那一个。
2. **把 sidecar 复制到** ``src-tauri/binaries/`` —— tauri.conf 的
   ``bundle.resources`` 指向它，安装包里才能带上后台服务。

   注：前端不需要单独准备 —— 窗口直接加载 sidecar 提供的页面
   （`src-tauri/ui/` 只是启动期间的 splash）。

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
BINARIES = ROOT / "src-tauri" / "binaries"


def build_sidecar() -> int:
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
    if not exe.exists():
        print("未找到 sidecar 产物，构建可能失败", file=sys.stderr)
        return 1
    print(f"\nsidecar：{exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    shutil.rmtree(WORK, ignore_errors=True)  # 中间产物不留
    return 0


def prepare_ui() -> int:
    """生成 ``src-tauri/ui/``（真实界面 + splash + static 资源）。

    页面引用绝对路径 ``/static/…``，必须保住这一层目录结构。
    """
    return subprocess.run(
        [sys.executable, str(PKG / "prepare_ui.py")], cwd=str(ROOT)
    ).returncode


def stage_binaries() -> int:
    src = DIST / "litprism-sidecar.exe"
    if not src.exists():
        print("先构建 sidecar，再复制到 src-tauri/binaries", file=sys.stderr)
        return 1
    BINARIES.mkdir(parents=True, exist_ok=True)
    dst = BINARIES / src.name
    shutil.copy2(src, dst)
    print(f"已复制到 {dst}（bundle.resources 用）")
    return 0


def main() -> int:
    for step in (build_sidecar, prepare_ui, stage_binaries):
        rc = step()
        if rc != 0:
            return rc
    print("\n全部完成。下一步：cd src-tauri && cargo build --release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
