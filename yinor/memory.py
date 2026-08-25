"""Memory 门面：对外统一入口（摄入 / 搜索 / 历史 / 统计）。"""

from __future__ import annotations

import logging

from .frozen import app_dir
from .llm import LLMClient
from .models import Episode, SearchResponse
from .pipeline import add_episode as _add_episode
from .search import Searcher
from .storage import Storage

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(app_dir() / "data" / "yinor.db")


class Memory:
    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        llm: LLMClient | None = None,
        default_group: str = "default",
        storage: Storage | None = None,
    ):
        # 传入共享 storage 时不拥有它（close 不关闭）；否则自建并拥有
        self._owns_storage = storage is None
        self.storage = storage if storage is not None else Storage(db_path)
        self.llm = llm or LLMClient()
        self.searcher = Searcher(self.storage, self.llm)
        self.default_group = default_group

    # ---------- 写入 ----------

    async def add_episode(
        self,
        content: str,
        group_id: str | None = None,
        name: str | None = None,
        source: str = "text",
        source_description: str = "",
        valid_at: str | None = None,
        extract: bool = True,
        update_summary: bool = True,
    ) -> Episode:
        """摄入一条信息（对话/笔记/消息）到记忆图。"""
        return await _add_episode(
            storage=self.storage,
            llm=self.llm,
            content=content,
            group_id=group_id or self.default_group,
            name=name,
            source=source,
            source_description=source_description,
            valid_at=valid_at,
            extract=extract,
            update_summary=update_summary,
        )

    async def forget(self, episode_uuid: str) -> None:
        """删除一条 episode 及其独占的事实。"""
        self.storage.delete_episode(episode_uuid)

    # ---------- 读取 ----------

    async def search(
        self,
        query: str,
        group_id: str | None = None,
        limit: int = 20,
        as_of: str | None = None,
        include_graph: bool = True,
        rerank: bool | None = None,
    ) -> SearchResponse:
        """混合检索。as_of 查询历史时刻。"""
        return await self.searcher.search(
            query,
            group_id=group_id or self.default_group,
            limit=limit,
            as_of=as_of,
            include_graph=include_graph,
            rerank=rerank,
        )

    async def history(
        self, entity_name: str, group_id: str | None = None, limit: int = 20
    ):
        """实体的事实演变史。"""
        return await self.searcher.history(
            entity_name, group_id=group_id or self.default_group, limit=limit
        )

    def stats(self, group_id: str | None = None) -> dict[str, int]:
        return self.storage.stats(group_id)

    def get_episode(self, uuid: str) -> Episode | None:
        return self.storage.get_episode(uuid)

    def close(self) -> None:
        if self._owns_storage:
            self.storage.close()


def fmt_search(resp: SearchResponse) -> str:
    """搜索结果的文本渲染（CLI / pi 工具用）。"""
    lines = [f"查询: {resp.query} ({resp.elapsed_ms:.0f}ms)"]
    if resp.entities:
        lines.append("\n[实体]")
        for e in resp.entities:
            labels = ", ".join(e["labels"]) if e["labels"] else "Entity"
            lines.append(f"  • {e['name']} ({labels}) — {(e['summary'] or '')[:80]}")
    lines.append("\n[事实]")
    for f in resp.facts:
        window = ""
        if f.valid_at:
            window = " [" + f.valid_at[:10]
            if f.invalid_at:
                window += " → " + f.invalid_at[:10] + "]"
            else:
                window += " → 现在]"
        lines.append(f"  • {f.fact}{window}")
    if resp.episodes:
        lines.append("\n[原文]")
        for ep in resp.episodes:
            lines.append(f"  • {ep['content'][:120]}")
    if not resp.facts and not resp.entities and not resp.episodes:
        lines.append("  （无结果）")
    return "\n".join(lines)


# (no-op: 触发 pyright 重分析，SearchResponse.episodes 已在 models.py:142 定义)


__all__ = [
    "Memory",
    "LLMClient",
    "fmt_search",
    "SearchResponse",
    "Episode",
    "DEFAULT_DB_PATH",
]
