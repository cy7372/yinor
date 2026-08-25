"""Frozen（PyInstaller exe）与源码运行的双模式路径解析。

打包成单文件 exe 后，模块文件落在解包临时目录（sys._MEIPASS，每次运行都变），
而用户数据（.env、data/yinor.db）必须放在 exe 旁边的固定目录——两类路径不能混：

- resource_path(...)：随包只读资源（schema.sql、static/ 控制台），frozen 时取
  _MEIPASS/yinor/...，源码时取包目录（与 Path(__file__) 同级，行为不变）。
- app_dir()：可写位置（.env、数据目录），frozen 时取 exe 所在目录（安装目录），
  源码时取项目根（与历史行为一致）。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*rel: str) -> Path:
    """随包只读资源路径（schema.sql / static/...）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base).joinpath("yinor", *rel)
    return Path(__file__).resolve().parent.joinpath(*rel)


def app_dir() -> Path:
    """可写目录（.env、data/）：exe 旁或项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
