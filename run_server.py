"""yinor 服务启动入口（Servy 调用）。

Windows 下强制使用 SelectorEventLoop：默认的 ProactorEventLoop（IOCP）
在并发短连接 burst 时 accept 会抛 WinError 64（'指定的网络名不再可用'），
uvicorn 停止 accept 但进程存活，导致 Servy 健康检查反复失败、重启循环。
Selector 模型不经过 IOCP，可根治；localhost 低并发服务性能完全够用。
"""

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn


def _port() -> int:
    try:
        return int(os.environ.get("YINOR_PORT", "20102"))
    except ValueError:
        return 20102


if __name__ == "__main__":
    uvicorn.run(
        "yinor.server:app",
        host="127.0.0.1",
        port=_port(),
        log_level="info",
    )
