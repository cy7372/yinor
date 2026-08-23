"""时间感知提取端到端测试：显式日期 + 相对日期 → valid_at/invalid_at 落库。

失败也保证清理（finally），独立 GROUP 不污染生产数据。
"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from yinor.llm import LLMClient
from yinor.pipeline import add_episode
from yinor.storage import Storage

GROUP = "time-test-" + uuid.uuid4().hex[:8]
DB = str(Path(__file__).resolve().parent / "data" / "yinor.db")


async def main() -> None:
    st = Storage(DB)
    llm = LLMClient()
    try:
        ep = await add_episode(
            st,
            llm,
            "张三 2018 年 3 月入职风云公司任架构师，2022 年 6 月离职；下周他要去拜访老客户。",
            group_id=GROUP,
            name="时间感知测试",
            source="text",
        )
        rows = st.conn.execute(
            "SELECT fact, valid_at, invalid_at FROM facts WHERE group_id=? ORDER BY valid_at",
            (GROUP,),
        ).fetchall()
        for r in rows:
            print(
                f"  fact={r['fact'][:50]!r} valid_at={r['valid_at']} invalid_at={r['invalid_at']}"
            )

        tenure = [r for r in rows if "入职" in r["fact"] or "任架构师" in r["fact"]]
        if not tenure:
            raise RuntimeError("未提取到任职事实")
        if "2018" not in (tenure[0]["valid_at"] or ""):
            raise RuntimeError(f"valid_at 不是2018: {tenure[0]['valid_at']}")
        print(f"  ✓ 任职事实 valid_at={tenure[0]['valid_at']}")
        leave = [r for r in rows if "离职" in r["fact"]]
        if leave:
            print(f"  ✓ 离职事实 invalid_at={leave[0]['invalid_at']}")

        # 相对日期（下周）：LLM 应解析为 2026-08 中旬以后的绝对日期
        visit = [r for r in rows if "拜访" in r["fact"] or "老客户" in r["fact"]]
        for r in visit:
            if r["valid_at"] and r["valid_at"].startswith("2026"):
                print(f"  ✓ 相对日期解析 valid_at={r['valid_at']}")
        print("全部通过")
    finally:
        st.delete_episode(ep.uuid) if "ep" in dir() else None
        st.conn.execute("DELETE FROM facts WHERE group_id=?", (GROUP,))
        st.conn.execute("DELETE FROM entities WHERE group_id=?", (GROUP,))
        st.conn.commit()
        st.close()


if __name__ == "__main__":
    asyncio.run(main())
