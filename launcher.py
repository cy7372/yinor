"""yinor exe 入口（PyInstaller 打包用，源码运行等价 run_server.py）。

双击/命令行启动本地记忆服务，日志直接打到控制台窗口——关窗口即停服务。
配置读取 exe 旁的 .env（LLM_API_KEY / YINOR_LLM_BASE_URL / YINOR_PORT 等），
数据落在 exe 旁的 data/ 目录；Web 控制台 http://127.0.0.1:20102/。
"""

from __future__ import annotations

import os

# 提前 import 让 PyInstaller 静态分析收集 yinor.server 及其依赖
from yinor.server import app  # noqa: F401
import uvicorn


def _port() -> int:
    try:
        return int(os.environ.get("YINOR_PORT", "20102"))
    except ValueError:
        return 20102


def main() -> None:
    port = _port()
    print(f"印·Yinor 记忆服务启动中… http://127.0.0.1:{port}/")
    print("  配置: .env（同目录）  数据: data/（同目录）  停止: 关闭本窗口或 Ctrl+C")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
