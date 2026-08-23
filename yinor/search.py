"""混合检索：FTS5(BM25) + 向量余弦 → RRF 融合 → 时间感知加权 → LLM 重排 → 图遍历扩展。

- 事实检索：FTS + 向量 → RRF 融合（默认只看当前有效事实，支持 as_of 历史查询）
- 实体检索：FTS + 向量 → 实体候选
- 图遍历：命中实体的一跳邻域事实（"如果改了 X，还关联什么"）
- 时间感知：查询中的时间表达（"八月中旬"/"最近一周"）解析成窗口，窗口内记忆加权
- 二段重排：RRF 候选池经 LLM 相关度精排（可关，失败降级原序）
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .llm import LLMClient
from .models import SearchResponse, SearchResult
from .prompts import RERANK_SYSTEM, RERANK_USER, RerankResult
from .storage import Storage

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 常数
TOP_K = 20
RECENCY_HALF_LIFE_DAYS = 60.0  # recency 衰减半衰期（天）
RECENCY_FLOOR = 0.5  # 旧记忆最低权重——只降权不埋没
TIME_BOOST = 1.5  # 查询时间窗口内记忆的加权倍数
RERANK_POOL = 10  # 送入 LLM 重排的候选池（实测池>10 时 LLM 延迟>8s 抖到超时）
RERANK_TIMEOUT_S = 12.0  # 单次重排超时：超时降级 RRF 原序（自动注入 15s 超时内留余量）

# ---------- 查询时间表达解析（正则，确定性，不调 LLM） ----------

_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}
_RE_ABS_DATE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
_RE_MONTH_DAY = re.compile(r"([一二三四五六七八九十]{1,2}|\d{1,2})\s*月\s*([一二三四五六七八九十]{1,2}|\d{1,2})\s*[日号]")
_RE_MONTH_PART = re.compile(r"([一二三四五六七八九十]{1,2}|\d{1,2})\s*月\s*(初|中旬|底|末)")
_RE_MONTH_ONLY = re.compile(r"([一二三四五六七八九十]{1,2}|\d{1,2})\s*月(?!\d|日|号|初|中|底|末)")
_RE_RECENT = re.compile(
    r"(?:最近|最新|刚刚|近期?)(?:一?周|几?天|一个?月)?|这(?:一?周|几天|个月)"
)
_RE_LAST_MONTH = re.compile(r"上(?:个)?月")


def _cn_num(s: str) -> int | None:
    s = s.strip()
    try:
        if s.isdigit():
            return int(s)
    except ValueError:  # pragma: no cover —— isdigit 已保证
        return None
    if len(s) == 2 and s.startswith("十"):
        return 10 + _CN_NUM.get(s[1], 0)
    return _CN_NUM.get(s)


def _month_last_day(year: int, month: int) -> int:
    try:
        return calendar.monthrange(year, month)[1]
    except (ValueError, calendar.IllegalMonthError):
        return 30


def _month_window(year: int, month: int) -> tuple[str, str]:
    last = _month_last_day(year, month)
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last}"


def _safe_date(y: int, mo: int, d: int) -> str | None:
    """拼合法 ISO 日期；月/日越界返回 None（'13月'/'2月31日' 等）。"""
    try:
        if not (1 <= mo <= 12) or not (1 <= d <= _month_last_day(y, mo)):
            return None
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except (ValueError, TypeError):
        return None


def extract_query_time(query: str, now: datetime | None = None) -> tuple[str, str] | None:
    """从查询中解析时间表达 → (start_iso, end_iso) 窗口；无时间表达返回 None。

    支持：YYYY-MM-DD、X月X日、X月（整月）、X月初/中旬/底、最近/最新（默认 30 天）。
    解析结果不合法时返回 None（不抛异常——时间感知是增强不是依赖）。
    """
    now = now or datetime.now(UTC)
    q = query or ""

    m = _RE_ABS_DATE.search(q)
    if m:
        try:
            d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            d = None
        if d:
            return d, d

    m = _RE_MONTH_DAY.search(q)
    if m:
        mo, dd = _cn_num(m.group(1)), None
        try:
            dd = int(m.group(2))
        except ValueError:
            dd = None
        if mo and dd:
            y = now.year if mo <= now.month else now.year - 1  # 优先当年
            d = _safe_date(y, mo, dd)
            if d:
                return d, d

    m = _RE_MONTH_PART.search(q)
    if m:
        mo = _cn_num(m.group(1))
        part = m.group(2)
        if mo:
            y = now.year if mo <= now.month else now.year - 1
            ranges = {"初": (1, 10), "中旬": (10, 20), "底": (21, 31), "末": (21, 31)}
            lo, hi = ranges[part]
            d1 = _safe_date(y, mo, lo)
            d2 = _safe_date(y, mo, min(hi, _month_last_day(y, mo)))
            if d1 and d2:
                return d1, d2

    m = _RE_MONTH_ONLY.search(q)
    if m:
        mo = _cn_num(m.group(1))
        if mo and 1 <= mo <= 12:
            y = now.year if mo <= now.month else now.year - 1
            return _month_window(y, mo)

    if _RE_LAST_MONTH.search(q):
        y, mo = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        return _month_window(y, mo)

    if _RE_RECENT.search(q):
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        return start, now.strftime("%Y-%m-%d")

    return None


def _time_factor(ts: str | None, window: tuple[str, str]) -> float:
    """记忆时间戳落在查询时间窗口内 → TIME_BOOST，否则 1.0（只加不减）。"""
    if not ts:
        return 1.0
    day = ts[:10]
    return TIME_BOOST if window[0] <= day <= window[1] else 1.0


async def _rerank(
    llm: LLMClient,
    query: str,
    items: list[tuple[str, str]],
) -> dict[str, float] | None:
    """LLM 二段重排：[(uuid, text)] → {uuid: 0-10 相关度}。失败返回 None（降级原序）。"""
    if not items:
        return None
    lines = [f"{i} | {text[:80]}" for i, (_, text) in enumerate(items)]
    try:
        data = await asyncio.wait_for(
            llm.chat(
                [
                    {"role": "system", "content": RERANK_SYSTEM},
                    {
                        "role": "user",
                        "content": RERANK_USER.format(
                            query=query, candidates="\n".join(lines)
                        ),
                    },
                ],
                response_model=RerankResult,
            ),
            timeout=RERANK_TIMEOUT_S,
        )
        id_by_idx = {i: u for i, (u, _) in enumerate(items)}
        out: dict[str, float] = {}
        for row in data.get("scores", []):
            try:
                u = id_by_idx.get(int(row.get("id")))
                if u is not None:
                    out[u] = max(0.0, min(10.0, float(row.get("score", 0))))
            except (TypeError, ValueError):
                continue
        return out or None
    except Exception:  # noqa: BLE001 —— 重排失败不阻塞检索，降级原序
        logger.warning("rerank 失败，降级 RRF 排序", exc_info=True)
        return None


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
    def __init__(self, storage: Storage, llm: LLMClient, rerank: bool | None = None):
        self.storage = storage
        self.llm = llm
        # LLM 重排默认关（+5~12s 延迟，注入场景不可接受）；三种打开方式：
        # Searcher(rerank=True) / search(rerank=True) / 环境变量 YINOR_RERANK=1
        if rerank is None:
            rerank = os.environ.get("YINOR_RERANK", "0") == "1"
        self.rerank = rerank

    async def search(
        self,
        query: str,
        group_id: str = "default",
        limit: int = TOP_K,
        as_of: str | None = None,
        include_graph: bool = True,
        rerank: bool | None = None,
    ) -> SearchResponse:
        """主搜索入口。

        as_of: 查询历史时刻（ISO 时间），此时只返回该时刻仍有效的事实。
        include_graph: 是否对命中的实体做一跳邻域图遍历扩展。
        rerank: 二段 LLM 重排（None=实例默认）。耗时 +5~12s，注入场景不开，
        控制台/评测等延迟不敏感场景开。

        流程：FTS+向量 RRF → recency/时间窗口加权 → [facts/episodes 重排并发]
        → 图遍历扩展。重排失败/超时自动降级 RRF 原序。
        """
        start = time.perf_counter()
        query_vec = await self.llm.embed(query)

        # 查询时间窗口："八月中旬的部署方案" → 只对落在窗口内的记忆加权
        t_window = extract_query_time(query)
        now = _parse_iso(as_of) or datetime.now(UTC)
        qvec = np.asarray(query_vec, dtype=np.float32)

        # ---- 事实候选（FTS + 向量 → RRF → recency/时间加权）----
        fts_hits = [
            u for u, _ in self.storage.fts_search_facts(query, group_id, limit=limit)
        ]
        fact_uuids, fact_mat = self.storage.load_embeddings("fact", group_id)
        vec_hits = _vector_top_k(fact_uuids, fact_mat, qvec, limit)

        fused = _rrf([fts_hits, vec_hits])
        # recency：近期写入的记忆加权；facts 用 reference_time（何时学到，fallback valid_at）
        cand_facts = {u: self.storage.get_fact(u) for u in fused}
        for u, f in cand_facts.items():
            if f is not None:
                fused[u] *= _recency_factor(f.reference_time or f.valid_at, now)
                if t_window:
                    fused[u] *= _time_factor(f.reference_time or f.valid_at, t_window)
        pool_facts = sorted(fused, key=fused.__getitem__, reverse=True)[:RERANK_POOL]

        # ---- Episode 候选（同样 RRF + 加权；候选拉取前移以便与 facts 重排并发）----
        ep_fts = [
            u for u, _ in self.storage.fts_search_episodes(query, group_id, limit=limit)
        ]
        ep_uuids, ep_mat = self.storage.load_embeddings("episode", group_id)
        ep_vec = _vector_top_k(ep_uuids, ep_mat, qvec, limit)
        fused_eps = _rrf([ep_fts, ep_vec])
        cand_eps = {u: self.storage.get_episode(u) for u in fused_eps}
        for u, ep in cand_eps.items():
            if ep is not None:
                fused_eps[u] *= _recency_factor(ep.created_at, now)
                if t_window:
                    fused_eps[u] *= _time_factor(ep.created_at, t_window)
        # 池 = max(RERANK_POOL, limit)：非重排路径保持原 limit 行为（回归教训：
        # 池截到 RERANK_POOL 会把 limit 内的候选砍掉）；重排只送前 RERANK_POOL 条
        pool_eps = sorted(fused_eps, key=fused_eps.__getitem__, reverse=True)[
            : max(RERANK_POOL, limit)
        ]

        # ---- 二段重排（episodes 通道；LLM 相关度精排解决 RRF 挡不住的语义噪音）----
        rerank_task_e = None
        if self.rerank if rerank is None else rerank:
            # 只重排 episodes（注入主通道）；facts 走 RRF+时间感知（延迟预算）
            rerank_task_e = asyncio.create_task(
                _rerank(
                    self.llm,
                    query,
                    [
                        (u, cand_eps[u].name + "：" + cand_eps[u].content)
                        for u in pool_eps[:RERANK_POOL]
                        if cand_eps[u] is not None
                    ],
                )
            )

        # ---- 实体检索（本地计算，趁重排在跑）----
        ent_fts = [
            u for u, _ in self.storage.fts_search_entities(query, group_id, limit=limit)
        ]
        ent_uuids, ent_mat = self.storage.load_embeddings("entity", group_id)
        ent_vec = _vector_top_k(ent_uuids, ent_mat, qvec, limit)
        fused_ents = _rrf([ent_fts, ent_vec])
        top_ent_uuids = sorted(fused_ents, key=fused_ents.__getitem__, reverse=True)[
            :limit
        ]
        entities = [self.storage.get_entity(u) for u in top_ent_uuids]
        entities = [e for e in entities if e is not None]

        # ---- 收重排结果并融合排序 ----
        rel_ep = None
        if rerank_task_e is not None:
            try:
                rel_ep = await rerank_task_e
            except BaseException:  # noqa: BLE001 —— 重排失败降级原序
                rel_ep = None
        if rel_ep:
            mx_e = max(fused_eps.values()) or 1.0
            pool_eps = sorted(
                pool_eps,
                key=lambda u: (rel_ep.get(u, 0.0), fused_eps.get(u, 0.0) / mx_e),
                reverse=True,
            )
            # 重排相关性写回 score：调用方（双层合并/展示）按 score 排序时
            # 不丢失重排序（相关度 0-10 为主 + 名次微调，单调一致）
            for i, u in enumerate(pool_eps):
                fused_eps[u] = rel_ep.get(u, 0.0) + 0.001 * i

        top_fact_uuids = pool_facts[:limit]
        facts = [cand_facts[u] for u in top_fact_uuids]
        facts = [f for f in facts if f is not None]

        # 时序过滤
        if as_of:
            facts = [f for f in facts if _valid_at_time(f, as_of)]
        else:
            facts = [f for f in facts if f.expired_at is None]

        # ---- 图遍历：命中实体的一跳邻域事实（含 same_as 跨组汇聚）----
        if include_graph and entities:
            seen_fact_ids = {f.uuid for f in facts}
            for ent in entities[:5]:
                neighborhood = list(
                    self.storage.get_facts_mentioning(ent.uuid, group_id)
                )
                # same_as：跨分区链接实体的 facts 也汇聚（保留各自 group_id，
                # 跨组互认不合并——在 A 分区搜某实体也能看到 B 分区的相关知识）
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

        top_ep_uuids = pool_eps[:limit]
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
