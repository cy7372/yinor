"""真实场景测试：模拟对话记录（含时序变更），验证中文技术内容提取。"""

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from yinor.llm import LLMClient
from yinor.memory import Memory, fmt_search

logging.basicConfig(level=logging.WARNING)

DB = Path(__file__).resolve().parent / "data" / "real.db"


async def main():
    if DB.exists():
        DB.unlink()
    mem = Memory(db_path=str(DB), llm=LLMClient())

    # 模拟一个项目的演变对话（第 3 条与第 2 条矛盾：存储方案变了）
    episodes = [
        "我们打算搭建自己的记忆系统，参考 Graphiti 项目。计划用 Python 实现，LLM 走本地 cyRouter，存储后端先用 Kuzu。",
        "今天跑通了 Graphiti：用 Kuzu 嵌入式 + cyRouter 的 deepseek-v4-flash 做提取，embedding 用 text-embedding-v3。发现 falkordblite 不支持 Windows。",
        "决定自研精简版 yinor：存储后端改成 SQLite + FTS5，不用 Kuzu 了，因为 Kuzu 已停维护。MVP 保留时序、溯源、混合检索。",
        "用户偏好：cyRouter 的 key 存在 .env 的 CYROUTER_API_KEY。记忆系统最终要集成成 pi 扩展，逐步替换 MindMemOS。",
    ]

    for i, content in enumerate(episodes):
        ep = await mem.add_episode(content=content, name=f"会话{i}")
        print(f"[OK] ep{i}: {content[:40]}...")

    print("\n########## 搜索: 记忆系统的存储方案 ##########")
    resp = await mem.search("我们的记忆系统用什么存储")
    print(fmt_search(resp))

    print("\n########## 搜索: Kuzu 相关 ##########")
    resp = await mem.search("Kuzu 是什么状态")
    print(fmt_search(resp))

    print("\n########## history: yinor ##########")
    for r in await mem.history("yinor"):
        win = f"[{r.valid_at[:10] if r.valid_at else '?'} → {r.invalid_at[:10] if r.invalid_at else '现在'}]"
        print(f"  {'已失效' if r.invalid_at else '有效  '} {win} {r.fact}")

    print("\n########## history: SQLite ##########")
    for r in await mem.history("SQLite"):
        win = f"[{r.valid_at[:10] if r.valid_at else '?'} → {r.invalid_at[:10] if r.invalid_at else '现在'}]"
        print(f"  {'已失效' if r.invalid_at else '有效  '} {win} {r.fact}")

    print("\n########## stats ##########")
    print(mem.stats())
    mem.close()


if __name__ == "__main__":
    asyncio.run(main())
