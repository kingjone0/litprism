#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文献棱镜 LitPrism 便捷启动器（免安装 PYTHONPATH）。

用法::

    python run.py init
    python run.py add paper.pdf --verify
    python run.py ask "这篇论文的创新点是什么？"
    python run.py serve

等价于 ``python -m litprism <命令>``，只是自动把 src/ 加入模块搜索路径。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Windows 控制台 UTF-8，避免中文与特殊符号乱码
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from litprism.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
