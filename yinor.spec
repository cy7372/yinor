# -*- mode: python ; coding: utf-8 -*-
# yinor PyInstaller spec → dist/yinor.exe（onefile，带控制台窗口）
# 构建入口：build.ps1（或 .venv\Scripts\python -m PyInstaller yinor.spec）

import sys
from pathlib import Path

root = Path(SPECPATH).resolve()

datas = [
    # 只读资源：解包后落在 _MEIPASS/yinor/ 下，由 yinor/frozen.py resource_path 定位
    (str(root / "yinor" / "static"), "yinor/static"),
    (str(root / "yinor" / "schema.sql"), "yinor"),
]

# uvicorn 运行时按需 import 的模块，静态分析看不到
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["launcher.py"],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="yinor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 服务型程序：控制台窗口即日志窗，关窗即停
    icon=str(root / "packaging" / "yinor.ico"),
    version=str(root / "packaging" / "version_info.txt"),
)
