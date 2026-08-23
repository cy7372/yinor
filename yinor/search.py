"""混合检索：FTS5(BM25) + 向量余弦 → RRF 融合 → 图遍历扩展。

参考 Graphiti 的搜索设计（四层 scope + 混合检索），精简为：
- 事实检索：FTS + 向量 → RRF 融合（默认只看当前有效事实，支持 as_of 历史查询）
- 实体检索：FTS + 向量 → 实体候选
- 图遍历：命中实体的一跳邻域事实（"如果改了 X，还关联什么"）
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np

from .llm import LLMClient
from .models import SearchResponse, SearchResult
from .storage import Storage

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 常数
TOP_K = 20
RECENCY_HALF_LIFE_DAYS = 60.0  # recency 衰减半衰期（天）
RECENCY_FLOOR = 0.5  # 旧记忆最低权重——只降权不埋没


def _rrf(ranked_lists: list[list[str]]) -> dict[str, float]:
    """Reciprocal Rank Fusion：合并多个按序排列的 uuid 列表。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, uid in enumerate(lst):
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def _parse_iso(ts: str | None) -> datetime | None:
    """宽容解析 ISO8601（含 Z 后缀 / 无时区 / 日期-only）；失败返回 None。"""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t


def _recency_factor(ts: str | None, now: datetime) -> float:
    """recency 衰减因子：1.0(刚写入) → 0.5(远古)，半衰期 60 天。

    参考 Generative Agents 的 recency 检索因子与 ACT-R 的 activation 衰减。
    设下限而非纯指数衰减——旧知识仍有效，只降权不埋没。
    时间戳无法解析时不惩罚（返回 1.0）。
    """
    t = _parse_iso(ts)
    if t is None:
        return 1.0
    age_days = max(0.0, (now - t).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * decay


def _vector_top_k(
    uuids: list[str], mat: np.ndarray, query_vec: np.ndarray, k: int
) -> list[str]:
    if not uuids or mat.size == 0:
        return []
    norms = np.linalg.norm(mat, axis=1)
    vnorm = np.linalg.norm(query_vec)
    if vnorm == 0:
        return []
    sims = (mat @ query_vec) / (norms * vnorm + 1e-9)
    order = np.argsort(-sims)
    return [uuids[i] for i in order[:k]]


class Searcher:
    def __init__(self, storage: Storage, llm: LLMClient):
        self.storage = storage
        self.llm = llm

    async def search(
        self,
        query: str,
        group_id: str = "default",
        limit: int = TOP_K,
        as_of: str | None = None,
        include_graph: bool = True,
    ) -> SearchResponse:
        """主搜索入口。

        as_of: 查询历史时刻（ISO 时间），此时只返回该时刻仍有效的事实。
        include_graph: 是否对命中的实体做一跳邻域图遍历扩展。
        """
        start = time.perf_counter()
        query_vec = await self.llm.embed(query)

        # ---- 事实检索（FTS + 向量）----
        fts_hits = [
            u for u, _ in self.storage.fts_search_facts(query, group_id, limit=limit)
        ]
        fact_uuids, fact_mat = self.storage.load_embeddings("fact", group_id)
        vec_hits = _vector_top_k(
            fact_uuids, fact_mat, np.asarray(query_vec, dtype=np.float32), limit
        )

        fused = _rrf([fts_hits, vec_hits])
        # recency 衰减：近期写入的记忆加权（Generative Agents recency 因子）。
        # facts 用 reference_time（何时学到，fallback valid_at）；
        # as_of 历史查询以该时刻为"现在"基准。
        now = _parse_iso(as_of) or datetime.now(UTC)
        cand_facts = {u: self.storage.get_fact(u) for u in fused}
        for u, f in cand_facts.items():
            if f is not None:
                fused[u] *= _recency_factor(f.reference_time or f.valid_at, now)
        top_fact_uuids = sorted(fused, key=fused.__getitem__, reverse=True)[:limit]

        facts = [cand_facts[u] for u in top_fact_uuids]
        facts = [f for f in facts if f is not None]

        # 时序过滤
        if as_of:
            facts = [f for f in facts if _valid_at_time(f, as_of)]
        else:
            facts = [f for f in facts if f.expired_at is None]

        # ---- 实体检索（用于图遍历 + 结果展示）----
        ent_fts = [
            u for u, _ in self.storage.fts_search_entities(query, group_id, limit=limit)
        ]
        ent_uuids, ent_mat = self.storage.load_embeddings("entity", group_id)
        ent_vec = _vector_top_k(
            ent_uuids, ent_mat, np.asarray(query_vec, dtype=np.float32), limit
        )
        fused_ents = _rrf([ent_fts, ent_vec])
        top_ent_uuids = sorted(fused_ents, key=fused_ents.__getitem__, reverse=True)[
            :limit
        ]
        entities = [self.storage.get_entity(u) for u in top_ent_uuids]
        entities = [e for e in entities if e is not None]

        # ---- 图遍历：命中实体的一跳邻域事实（含 same_as 跨组汇聚）----
        if include_graph and entities:
            seen_fact_ids = {f.uuid for f in facts}
            for ent in entities[:5]:
                neighborhood = list(
                    self.storage.get_facts_mentioning(ent.uuid, group_id)
                )
                # same_as：跨分区链接实体的 facts 也汇聚（保留各自 group_id，
                # 跨组互认不合并——在 Suey 搜 cyRouter 也能看到 yinor 组的知识）
                for lu in self.storage.get_linked_uuids(ent.uuid):
                    neighborhood.extend(self.storage.get_facts_mentioning_any_group(lu))
                for f in neighborhood:
                    if f.uuid in seen_fact_ids:
                        continue
                    seen_fact_ids.add(f.uuid)
                    if f.expired_at is None or (as_of and _valid_at_time(f, as_of)):
                        facts.append(f)
            facts = facts[: limit * 2]

        # ---- 组装结果 ----
        result_facts = []
        ent_by_uuid = {
            e.uuid: e
            for e in self.storage.get_entities_by_uuids(
                {f.source_uuid for f in facts} | {f.target_uuid for f in facts}
            ).values()
        }
        for f in facts:
            src = ent_by_uuid.get(f.source_uuid)
            tgt = ent_by_uuid.get(f.target_uuid)
            result_facts.append(
                SearchResult(
                    uuid=f.uuid,
                    fact=f.fact,
                    name=f.name,
                    source_uuid=f.source_uuid,
                    target_uuid=f.target_uuid,
                    source_name=src.name if src else f.source_uuid,
                    target_name=tgt.name if tgt else f.target_uuid,
                    valid_at=f.valid_at,
                    invalid_at=f.invalid_at,
                    score=fused.get(f.uuid, 0.0),
                    episodes=f.episodes,
                )
            )

        result_entities = [
            {
                "uuid": e.uuid,
                "name": e.name,
                "labels": e.labels,
                "summary": e.summary,
                "score": fused_ents.get(e.uuid, 0.0),
            }
            for e in entities
        ]

        # ---- Episode 原文检索（FTS + 向量 RRF，含未提取的 episode，溯源层召回）----
        ep_fts = [
            u for u, _ in self.storage.fts_search_episodes(query, group_id, limit=limit)
        ]
        ep_uuids, ep_mat = self.storage.load_embeddings("episode", group_id)
        ep_vec = _vector_top_k(
            ep_uuids, ep_mat, np.asarray(query_vec, dtype=np.float32), limit
        )
        fused_eps = _rrf([ep_fts, ep_vec])
        cand_eps = {u: self.storage.get_episode(u) for u in fused_eps}
        for u, ep in cand_eps.items():
            if ep is not None:
                fused_eps[u] *= _recency_factor(ep.created_at, now)
        top_ep_uuids = sorted(fused_eps, key=fused_eps.__getitem__, reverse=True)[
            :limit
        ]
        result_episodes = []
        for u in top_ep_uuids:
            ep = cand_eps.get(u)
            if ep:
                result_episodes.append(
                    {
                        "uuid": ep.uuid,
                        "name": ep.name,
                        "content": ep.content[:500],
                        "source": ep.source,
                        "created_at": ep.created_at,
                        "valid_at": ep.valid_at,
                        "score": fused_eps.get(u, 0.0),
                    }
                )

        return SearchResponse(
            facts=result_facts,
            entities=result_entities,
            episodes=result_episodes,
            query=query,
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )

    async def history(
        self, entity_name: str, group_id: str = "default", limit: int = 20
    ) -> list[SearchResult]:
        """实体的事实演变史：该实体相关的所有事实（含已失效），按时间排序。"""
        ents = self.storage.get_entities_by_name(entity_name, group_id)
        if not ents:
            # 向量近似找
            q = await self.llm.embed(entity_name)
            uuids, mat = self.storage.load_embeddings("entity", group_id)
            hits = _vector_top_k(uuids, mat, np.asarray(q, dtype=np.float32), 3)
            ents = [self.storage.get_entity(u) for u in hits]
            ents = [e for e in ents if e is not None]
        if not ents:
            return []
        ent = ents[0]
        facts = self.storage.get_facts_mentioning(ent.uuid, group_id)
        facts.sort(key=lambda f: f.valid_at or "", reverse=True)
        facts = facts[:limit]
        ent_by_uuid = self.storage.get_entities_by_uuids(
            {f.source_uuid for f in facts} | {f.target_uuid for f in facts}
        )
        out = []
        for f in facts:
            src = ent_by_uuid.get(f.source_uuid)
            tgt = ent_by_uuid.get(f.target_uuid)
            out.append(
                SearchResult(
                    uuid=f.uuid,
                    fact=f.fact,
                    name=f.name,
                    source_uuid=f.source_uuid,
                    target_uuid=f.target_uuid,
                    source_name=src.name if src else f.source_uuid,
                    target_name=tgt.name if tgt else f.target_uuid,
                    valid_at=f.valid_at,
                    invalid_at=f.invalid_at,
                    episodes=f.episodes,
                )
            )
        return out


def _valid_at_time(fact: Any, as_of: str) -> bool:
    """事实在 as_of 时刻是否有效。"""
    if fact.expired_at is not None:
        return False
    if fact.valid_at and fact.valid_at > as_of:
        return False
    if fact.invalid_at and fact.invalid_at <= as_of:
        return False
    return True
