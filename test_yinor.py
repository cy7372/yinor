"""yinor 端到端验证：摄入 → 消歧 → 时序 → 检索。"""

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from yinor.llm import LLMClient
from yinor.memory import Memory, fmt_search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB = Path(__file__).resolve().parent / "data" / "test.db"


async def main():
    # 用测试库，避免污染主库
    if DB.exists():
        DB.unlink()
    mem = Memory(db_path=str(DB), llm=LLMClient())

    episodes = [
        "Kamala Harris is the Attorney General of California. She was previously the district attorney for San Francisco.",
        "As AG, Harris was in office from January 3, 2011 to January 3, 2017.",
        "Gavin Newsom is the Governor of California. He was previously the Lieutenant Governor.",
        "In 2021, Kamala Harris became the Vice President of the United States.",
    ]

    for i, content in enumerate(episodes):
        ep = await mem.add_episode(content=content, name=f"ep{i}")
        logger.info("== added episode %d: %s", i, content[:50])
        print("\n" + "=" * 70)
        print("添加:", content)
        print("=" * 70)

    print("\n\n########## 搜索: Who was the California Attorney General? ##########")
    resp = await mem.search("Who was the California Attorney General?")
    print(fmt_search(resp))

    print("\n\n########## 搜索: 2024 年 Harris 的职位 (as_of 历史查询) ##########")
    resp = await mem.search(
        "What is Kamala Harris's job?", as_of="2024-01-01T00:00:00+00:00"
    )
    print(fmt_search(resp))

    print("\n\n########## history: Kamala Harris ##########")
    results = await mem.history("Kamala Harris")
    for r in results:
        win = f"[{r.valid_at[:10] if r.valid_at else '?'} → {r.invalid_at[:10] if r.invalid_at else '现在'}]"
        print(f"  {'已失效' if r.invalid_at else '有效  '} {win} {r.fact}")

    print("\n\n########## stats ##########")
    print(mem.stats())

    mem.close()


if __name__ == "__main__":
    asyncio.run(main())
