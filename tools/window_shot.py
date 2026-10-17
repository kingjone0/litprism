"""抓取一个窗口的截图（纯 ctypes，无第三方依赖）。

为什么需要它：桌面外壳的验证不能只看「进程起来了」——
窗口是否渲染出内容、渲染成什么样，只有看到才算实测。
本机 PowerShell 的 ``Add-Type`` 被安全策略拦，Pillow 也没装，所以直接调 user32/gdi32。

用法：
    python tools/window_shot.py "文献棱镜 LitPrism" _window_probe.png
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
import zlib
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SW_RESTORE = 9
SRCCOPY = 0x00CC0020
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def find_window(title: str) -> int:
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        raise SystemExit(f"找不到标题为「{title}」的窗口")
    return hwnd


def png(rgba: bytes, w: int, h: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + rgba[y * w * 4 : (y + 1) * w * 4] for y in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001


def _bring_to_front(hwnd: int) -> None:
    """把窗口真正放到最前。

    ``SetForegroundWindow`` 会被系统的「前台锁定」拒绝（除非调用方就是前台进程），
    结果是截到压在它上面的别的窗口 —— 第一版就栽在这里。
    先把这个超时置 0 解除限制，用完再还原。
    """
    prev = wt.DWORD()
    user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(prev), 0)
    user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, None, 0)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.0)


def _bitmap_bytes(hdc_mem, bmp, w: int, h: int) -> bytes:
    bi = BITMAPINFOHEADER(
        ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, BI_RGB, 0, 0, 0, 0, 0
    )
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    src = bytearray(buf.raw)
    src[0::4], src[2::4] = src[2::4], src[0::4]   # BGRA -> RGBA
    return bytes(src)


def _is_blank(rgba: bytes) -> bool:
    """整幅只有一种颜色 = 没抓到内容（全黑或全白）。"""
    first = rgba[:4]
    return all(rgba[i : i + 4] == first for i in range(0, min(len(rgba), 4 * 40000), 4))


def capture(hwnd: int) -> tuple[int, int, bytes]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise SystemExit("GetWindowRect 失败")
    x, y = rect.left, rect.top
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise SystemExit(f"窗口尺寸异常：{w}x{h}")

    user32.ShowWindow(hwnd, SW_RESTORE)

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, bmp)

    # 顺序有讲究：
    # ① 屏幕拷贝才拍到 WebView2 的真实内容，但要求窗口在最上层 ——
    #    先解除前台锁定再置前（见 _bring_to_front 的注释）；
    # ② PrintWindow 不怕遮挡，但抓不到 WebView2 的合成层（一片白），只作退路。
    _bring_to_front(hwnd)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
    rgba = _bitmap_bytes(hdc_mem, bmp, w, h)

    if _is_blank(rgba) and user32.PrintWindow(hwnd, hdc_mem, 2):
        rgba = _bitmap_bytes(hdc_mem, bmp, w, h)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return w, h, rgba


def main(argv: list[str]) -> int:
    title = argv[1] if len(argv) > 1 else "文献棱镜 LitPrism"
    out = Path(argv[2] if len(argv) > 2 else "_window_probe.png")
    hwnd = find_window(title)
    w, h, rgba = capture(hwnd)
    out.write_bytes(png(rgba, w, h))
    print(f"已保存 {out}（{w}x{h}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
