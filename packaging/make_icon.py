"""生成应用图标（纯标准库，无 Pillow 依赖）。

图案：深色底 + 白色入射光 → 棱镜三角 → 三色出射光谱，对应「文献棱镜」。

用法：``python packaging/make_icon.py``
产物：``src-tauri/icons/icon.ico``（多尺寸，ICO 内嵌 PNG，Vista+ 支持）
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src-tauri" / "icons" / "icon.ico"

BG = (11, 17, 32, 255)          # #0b1120
INK = (226, 232, 240, 255)      # 入射白光
SPECTRUM = [
    (248, 113, 113, 255),       # red
    (250, 204, 21, 255),        # amber
    (56, 189, 248, 255),        # sky
]


def _mix(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(4))


def _inside_triangle(x: float, y: float, pts) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = pts
    d1 = (x - x2) * (y1 - y2) - (x1 - x2) * (y - y2)
    d2 = (x - x3) * (y2 - y3) - (x2 - x3) * (y - y3)
    d3 = (x - x1) * (y3 - y1) - (x3 - x1) * (y - y1)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def render(size: int) -> bytes:
    """返回 RGBA 原始像素（自上而下）。"""
    n = float(size)
    # 棱镜：正立的等边三角形，略偏左
    tri = [(0.42 * n, 0.18 * n), (0.20 * n, 0.80 * n), (0.64 * n, 0.80 * n)]
    apex_y = 0.18 * n
    rows = []
    for py in range(size):
        row = bytearray()
        y = py + 0.5
        for px in range(size):
            x = px + 0.5
            c = BG

            # 圆角底：离角太远就透明
            r = 0.18 * n
            dx = max(r - x, 0.0) + max(x - (n - r), 0.0)
            dy = max(r - y, 0.0) + max(y - (n - r), 0.0)
            if (dx * dx + dy * dy) ** 0.5 > r:
                row.extend((0, 0, 0, 0))
                continue

            # 入射光：左侧水平光束打到棱镜左面（终点贴住棱镜，不留缺口）
            if 0.24 * n < y < 0.34 * n and 0.06 * n < x < 0.40 * n:
                c = _mix(c, INK, 1.0)

            # 棱镜本体（深色填充）
            if _inside_triangle(x, y, tri):
                c = _mix(c, (30, 41, 59, 255), 1.0)

            # 出射光谱：从棱镜右面起向右上方散开
            for i, col in enumerate(SPECTRUM):
                slope = 0.42 - i * 0.10
                y_line = apex_y + (x - 0.42 * n) * slope * -1 + 0.22 * n
                thick = max(1.2, 0.028 * n)
                if x > 0.46 * n and abs(y - y_line) < thick:
                    fade = 1.0 - (x - 0.46 * n) / (0.54 * n)
                    c = _mix(c, col, max(0.15, fade))

            row.extend(c)
        rows.append(bytes(row))
    return b"".join(rows)


def png(rgba: bytes, size: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + rgba[i * size * 4 : (i + 1) * size * 4] for i in range(size)
    )
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    sizes = [16, 32, 48, 64, 128, 256]
    images = [(s, png(render(s), s)) for s in sizes]

    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in images:
        dim = 0 if s >= 256 else s           # 256 记作 0
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(out)
    print(f"已生成 {OUT}  ({len(out)} 字节, {len(images)} 个尺寸)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
