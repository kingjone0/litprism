# -*- mode: python ; coding: utf-8 -*-
# LitPrism sidecar 打包配置
#
# 用法：pyinstaller packaging/litprism-sidecar.spec --noconfirm
#
# spike 验证目标：
#   1. pymupdf 的原生 DLL 能否进单文件并正常运行
#   2. 静态资源（web/static）能否打进包
#   3. 打包后的 exe 能否完整跑通：握手 → API → PDF 解析

import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent          # packaging/ 的上级 = 项目根
SRC = PROJECT_ROOT / "src"
STATIC_DIR = SRC / "litprism" / "web" / "static"
CONFIG_EXAMPLE = PROJECT_ROOT / "config" / "config.example.yaml"

block_cipher = None

a = Analysis(
    [str(Path(SPECPATH) / "sidecar_entry.py")],          # 入口脚本
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # 静态资源打进包内 litprism/web/static/
        (str(STATIC_DIR / "*"), "litprism/web/static"),
        # 配置模板必须进包 —— 否则打包版首次运行生成不出用户配置，
        # 设置界面改了也没地方落盘（frozen 下 APP_ROOT 才是可写根）
        (str(CONFIG_EXAMPLE), "config"),
    ],
    hiddenimports=[
        # PyInstaller 静态分析发现不了的动态导入
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "uvicorn.error",
    ],
    hookspath=[],
    hooks_config={},
    runtime_hooks=[],
    excludes=[
        # 可选依赖不打包：缺了应降级而不是报错
        "tkinter",
        "matplotlib",
        "numpy.testing",
        "pytest",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="litprism-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 对 pymupdf 的 DLL 常出问题，宁大勿坏
    runtime_tmpdir=None,
    console=True,       # sidecar 必须保留控制台：握手行走 stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
