"""反思机制（reflection）：把近期记忆综合成高层洞察，让方法层从知识层生长出来。

参考 Generative Agents (Park et al. 2023)：当近期记忆积累到一定程度，
agent 停下来综合更高层的"反思"写回记忆流。本实现为手动/定期触发版：
近期 episodes + 现有事实 → LLM 提炼 0-N 条洞察 → 调用方把每条洞察作为
source='reflection' 的 episode 走完整提取管线入库（复用去重/失效/检索全套机制）。

设计约束：
- 输入排除 migration 与 reflection 自身（避免"对反思再反思"的反馈循环）
- 宁缺毋滥：无洞察时返回空列表，一条不写
- 本模块只负责"取窗口 + 提炼"，写库由调用方（server._run_reflect）按
  补偿提取同款模式执行（全局锁串行、每条独立连接、节流）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .llm import LLMClient
from .storage import Storage

logger = logging.getLogger(__name__)

REFLECT_SYSTEM = """你是记忆系统的反思者。阅读近期记忆（事件片段 + 已提取的事实），提炼更高层、可复用的洞察。

值得提炼的洞察：
- 跨多个片段重复出现的模式（"又一次因为 X 踩坑 → 以后 Y"）
- 从具体经验升华、可迁移到未来任务的方法论或原则
- 对已有认知的修正或深化

要求：
1. 不复述单个事实；不产出事实列表里已有的内容（重复无价值）
2. 每条洞察是一句完整、独立、可执行的陈述，说明适用条件
3. 没有值得提炼的就输出空列表——宁缺毋滥
4. 最多 {max_insights} 条
以 JSON 输出：{{"insights": ["...", "..."]}}"""

REFLECT_USER = """当前时间：{now}

【近期记忆片段】（{n_episodes} 条，近 {days} 天）
{episodes}

【已有事实】（节选，用于避免重复提炼）
{facts}

请提炼洞察。"""

MIN_EPISODES = 3  # 窗口内少于此数不值得反思
MAX_EPISODES = 30  # 输入片段上限（控制 prompt 体积）
MAX_FACTS = 60  # 去重参考事实上限
EPISODE_SNIPPET = 300  # 每片段截断字数


class ReflectionResult(BaseModel):
    insights: list[str] = Field(default_factory=list)


def gather_window(
    storage: Storage,
    group_id: str,
    days: int,
    max_episodes: int = MAX_EPISODES,
    max_facts: int = MAX_FACTS,
) -> tuple[list, list]:
    """取反思输入：窗口内非迁移/非反思的 episode + 组内现有事实（去重参考）。"""
    ep_rows = storage.conn.execute(
        """SELECT name, content, created_at FROM episodes
           WHERE group_id=? AND source NOT IN ('migration', 'migration-local', 'reflection')
             AND datetime(created_at) > datetime('now', ?)
           ORDER BY created_at DESC LIMIT ?""",
        (group_id, f"-{days} day", max_episodes),
    ).fetchall()
    fact_rows = storage.conn.execute(
        """SELECT fact FROM facts
           WHERE group_id=? AND expired_at IS NULL
           ORDER BY rowid DESC LIMIT ?""",
        (group_id, max_facts),
    ).fetchall()
    return ep_rows, fact_rows


async def reflect(
    storage: Storage,
    llm: LLMClient,
    group_id: str,
    days: int = 7,
    max_insights: int = 3,
) -> list[str]:
    """执行一次反思，返回提炼出的洞察列表（不写库）。

    窗口内记忆不足 MIN_EPISODES 条时直接返回空——样本太少谈不上模式。
    """
    ep_rows, fact_rows = gather_window(storage, group_id, days)
    if len(ep_rows) < MIN_EPISODES:
        logger.info("反思跳过：窗口内记忆不足（%d 条）", len(ep_rows))
        return []

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    ep_text = "\n".join(
        f"- [{r['created_at'][:10]}] {r['name']}：{r['content'][:EPISODE_SNIPPET]}"
        for r in ep_rows
    )
    fact_text = "\n".join(f"- {r['fact']}" for r in fact_rows)
    user = REFLECT_USER.format(
        now=now,
        n_episodes=len(ep_rows),
        days=days,
        episodes=ep_text,
        facts=fact_text,
    )
    resp = await llm.chat(
        [
            {
                "role": "system",
                "content": REFLECT_SYSTEM.format(max_insights=max_insights),
            },
            {"role": "user", "content": user},
        ],
        response_model=ReflectionResult,
    )
    insights = ReflectionResult(**resp).insights

    # 清洗：去空白、保序去重、截断数量
    seen: set[str] = set()
    out: list[str] = []
    for s in insights:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    out = out[:max_insights]
    logger.info(
        "反思完成: group=%s 输入 %d 片段 → %d 洞察", group_id, len(ep_rows), len(out)
    )
    return out
