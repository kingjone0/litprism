"""PyInstaller 入口。

打包后以单文件 exe 运行；源码运行时等价于 ``python -m litprism.sidecar``。
"""

import sys
from pathlib import Path

# PyInstaller onedir/onefile 下把 src 加进 sys.path（源码运行时由 PYTHONPATH 提供）
if getattr(sys, "frozen", False):
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    src = base / "src" if (base / "src").exists() else base
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

from litprism.sidecar import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
