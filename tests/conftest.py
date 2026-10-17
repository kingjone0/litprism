"""pytest 全局引导：把 ``src`` 放进 import 路径。

各测试文件原本各自 ``sys.path.insert(... / "src")``。后果是**单独跑某个文件**
时能否 import 取决于它自己有没有那三行 —— 靠别的测试文件先跑一遍把路径插好，
是隐式依赖，换个执行顺序就红。这里统一处理，个别文件里的重复插入无害。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
