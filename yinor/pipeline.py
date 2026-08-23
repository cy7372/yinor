"""摄入 pipeline：LLM 提取 → 实体消歧 → 事实消歧（时序） → summary 演进 → 落库。

单 episode 流程：
1. 存 episode（先落库，失败可重试）
2. LLM 提取候选实体（带类型分类）
3. 实体消歧：名称/向量/LLM 三层判定，映射到图内已有实体或新建
4. LLM 提取候选事实（引用实体名）
5. 事实消歧：完全匹配复用 → LLM 判 duplicate/contradiction → 应用时序失效窗口
6. LLM 更新受影响实体的 summary
7. 写 mentions + embeddings + FTS 索引
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import numpy as np

from .dedup import should_auto_merge
from .llm import LLMClient
from .models import Entity, Episode, Fact, now_iso
from .prompts import (
    DEDUPE_EDGE_SYSTEM,
    DEDUPE_EDGE_USER,
    DEDUPE_ENTITY_SYSTEM,
    DEDUPE_ENTITY_USER,
    EXTRACT_ENTITIES_SYSTEM,
    EXTRACT_ENTITIES_USER,
    EXTRACT_FACTS_SYSTEM,
    EXTRACT_FACTS_USER,
    EdgeDuplicate,
    EntitiesExtraction,
    EntityDuplicate,
    EntitySummaries,
    ExtractedEntityItem,
    ExtractedFactItem,
    FactsExtraction,
)
from .storage import Storage

logger = logging.getLogger(__name__)

EMBEDDING_SIM_THRESHOLD = 0.72  # 实体名向量相似度阈值（高于此直接判为同一实体）
CANDIDATE_LIMIT = 5
INVALIDATION_CANDIDATE_LIMIT = 12

_NORMALIZE_RE = re.compile(r"[^\w\s\u4e00-\u9fff]|_")


_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

# 中文日期兜底提取（LLM 不填结构化时间字段时，从 fact 文本里挖）
_CHINESE_DATE_RE = re.compile(
    r"(20\d{2}|19\d{2})\s*年\s*(?:(\d{1,2})\s*月)?(?:(\d{1,2})\s*[日号])?"
)
_RANGE_SEP_RE = re.compile(r"至|到|—|–|~|～|→|->|－")


def _dates_from_text(text: str) -> tuple[str | None, str | None]:
    """从事实文本提取显式时间范围 (start, end)；无显式日期返回 (None, None)。"""
    matches = list(_CHINESE_DATE_RE.finditer(text or ""))
    if not matches:
        return None, None

    def to_iso(m: re.Match) -> str | None:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        return _norm_iso(f"{y}-{mo or '01'}-{d or '01'}")

    start = to_iso(matches[0])
    end = None
    for i in range(len(matches) - 1):
        between = text[matches[i].end() : matches[i + 1].start()]
        if _RANGE_SEP_RE.search(between):
            end = to_iso(matches[i + 1])
            break
    return start, end


def _norm_iso(s: str | None) -> str | None:
    """LLM 输出的时间字段归一化成规范 ISO8601 UTC；解析失败返回 None。"""
    if not s or not s.strip():
        return None
    s = s.strip()
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)  # 无时区按 UTC 解释（全库 UTC）
        return dt.astimezone(UTC).isoformat()
    logger.warning("无法解析时间字段 %r，忽略", s)
    return None


def normalize_name(s: str) -> str:
    return _NORMALIZE_RE.sub(" ", s.lower()).strip()


# ---------- 工具 ----------


def cosine_similarity(mat: np.ndarray, vec: np.ndarray) -> np.ndarray:
    """mat (N,d) 与 vec (d,) 的余弦相似度。"""
    norms = np.linalg.norm(mat, axis=1)
    vnorm = np.linalg.norm(vec)
    if vnorm == 0 or norms.size == 0:
        return np.zeros(mat.shape[0], dtype=np.float32)
    return (mat @ vec) / (norms * vnorm + 1e-9)


async def _embed_many(llm: LLMClient, texts: list[str]) -> list[list[float]]:
    return await llm.embed_batch(texts)


# ---------- 1. 实体提取 ----------


async def extract_entities(
    llm: LLMClient, episode: Episode, context: list[Episode]
) -> list[ExtractedEntityItem]:
    """LLM 提取候选实体。"""
    context_text = "\n".join(f"- {e.content[:500]}" for e in context[-5:]) or "（无）"
    user = EXTRACT_ENTITIES_USER.format(
        episode=episode.content[:4000], context=context_text[:2000]
    )
    resp = await llm.chat(
        [
            {"role": "system", "content": EXTRACT_ENTITIES_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_model=EntitiesExtraction,
    )
    parsed = EntitiesExtraction(**resp)
    # 过滤类型不在定义内的
    from .entity_types import DEFAULT_ENTITY_TYPES

    valid = [
        e for e in parsed.extracted_entities if e.entity_type in DEFAULT_ENTITY_TYPES
    ]
    return valid


# ---------- 2. 实体消歧 ----------


async def _entity_candidates(
    storage: Storage, llm: LLMClient, name: str, group_id: str
) -> list[Entity]:
    """语义候选：精确名 → FTS → 向量相似度，去重排序。"""
    exact = storage.get_entities_by_name(name, group_id)
    if exact:
        return exact

    candidates: list[Entity] = []
    seen: set[str] = set()

    fts_hits = storage.fts_search_entities(normalize_name(name), group_id, limit=5)
    ents = storage.get_entities_by_uuids([u for u, _ in fts_hits])
    for u, _ in fts_hits:
        if u not in seen:
            seen.add(u)
            if u in ents:
                candidates.append(ents[u])

    # 向量相似度候选（按组隔离）
    query_emb = await llm.embed(name)
    uuids, mat = storage.load_embeddings("entity", group_id)
    if uuids and mat.shape[1] > 0:
        sims = cosine_similarity(mat, np.asarray(query_emb, dtype=np.float32))
        order = np.argsort(-sims)
        for idx in order[:CANDIDATE_LIMIT]:
            if sims[idx] >= EMBEDDING_SIM_THRESHOLD * 0.6:
                u = uuids[idx]
                if u not in seen:
                    seen.add(u)
                    ent = storage.get_entity(u)
                    if ent:
                        candidates.append(ent)
    return candidates[:CANDIDATE_LIMIT]


async def resolve_entities(
    storage: Storage,
    llm: LLMClient,
    extracted: list[ExtractedEntityItem],
    group_id: str,
) -> tuple[list[Entity], dict[str, str]]:
    """消歧提取实体 → 图内实体。返回 (实体列表[新建+复用], name→uuid 映射)。

    层0 确定性预匹配：dedup.should_auto_merge 同款保守规则前置——写入前对同组
    全量实体跑规则判定，命中直接复用。原三层（完全匹配/向量/LLM）只在 FTS/向量
    召回的候选内判定，召回不到旧实体就会漏判新建（重复实体的主要来源）；层0
    不依赖召回，从源头堵住归一化同名/分隔符差异等确定性重复。
    """
    resolved: list[Entity] = []
    name_map: dict[str, str] = {}
    group_ents = storage.get_all_entities(group_id)  # 层0 判定池（新建实体也入池）

    for item in extracted:
        name = item.name.strip()
        if not name:
            continue

        target: Entity | None = None
        # 层0: 确定性规则预匹配（sim=None：无向量证据，相似度依赖规则自动禁用）
        for e in group_ents:
            ok, _reason = should_auto_merge(name, e.name, None)
            if ok:
                target = e
                break

        # 层0 未命中才走召回三层；命中则传空候选，原三层自然跳过
        candidates = (
            await _entity_candidates(storage, llm, name, group_id)
            if target is None
            else []
        )

        # 层1: 完全匹配
        for c in candidates:
            if normalize_name(c.name) == normalize_name(name):
                target = c
                break

        # 层2: 高向量相似度（名称近似判定，批量对比候选向量）
        if target is None and candidates:
            q_emb = await llm.embed(name)
            cand_vecs = []
            cand_map: dict[int, Entity] = {}
            for c in candidates:
                rows = storage.conn.execute(
                    "SELECT vector FROM embeddings WHERE uuid=?", (c.uuid,)
                ).fetchone()
                if rows:
                    cand_vecs.append(np.frombuffer(rows["vector"], dtype=np.float32))
                    cand_map[len(cand_vecs) - 1] = c
            if cand_vecs:
                mat = np.stack(cand_vecs)
                sims = cosine_similarity(mat, np.asarray(q_emb, dtype=np.float32))
                best_idx = sims.argmax().item()  # cand_vecs 非空（外层 if 保证）
                if sims[best_idx] >= EMBEDDING_SIM_THRESHOLD:
                    target = cand_map[best_idx]

        # 层3: LLM 判定（候选存在但没命中阈值）
        if target is None and candidates:
            indexed = "\n".join(
                f"{i}. {c.name} ({', '.join(c.labels)}) {c.summary[:80]}"
                for i, c in enumerate(candidates)
            )
            user = DEDUPE_ENTITY_USER.format(
                name=name,
                entity_type=item.entity_type,
                description=f"- {item.description}" if item.description else "",
                indexed_candidates=indexed,
            )
            resp = await llm.chat(
                [
                    {"role": "system", "content": DEDUPE_ENTITY_SYSTEM},
                    {"role": "user", "content": user},
                ],
                response_model=EntityDuplicate,
            )
            decision = EntityDuplicate(**resp)
            if (
                decision.is_duplicate
                and decision.duplicate_of is not None
                and 0 <= decision.duplicate_of < len(candidates)
            ):
                target = candidates[decision.duplicate_of]

        if target is not None:
            # 复用已有实体，补充 labels（新类型）
            if item.entity_type not in target.labels:
                target.labels.append(item.entity_type)
                storage.upsert_entity(target)
            resolved.append(target)
            name_map[name] = target.uuid
        else:
            new_ent = Entity(
                name=name,
                group_id=group_id,
                labels=[item.entity_type],
                summary=item.description,
            )
            emb = await llm.embed(name)
            storage.upsert_entity(new_ent, embedding=emb)
            group_ents.append(new_ent)  # 同 episode 后续实体可匹配到
            resolved.append(new_ent)
            name_map[name] = new_ent.uuid

    return resolved, name_map


# ---------- 3. 事实提取 ----------


async def extract_facts(
    llm: LLMClient, episode: Episode, entities: list[Entity]
) -> list[ExtractedFactItem]:
    """LLM 提取候选事实（带时间字段）。"""
    entity_list = "\n".join(f"- {e.name}: {', '.join(e.labels)}" for e in entities)
    user = EXTRACT_FACTS_USER.format(
        episode=episode.content[:4000],
        entities=entity_list[:2000],
        now=episode.valid_at or episode.created_at,
    )
    resp = await llm.chat(
        [
            {"role": "system", "content": EXTRACT_FACTS_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_model=FactsExtraction,
    )
    parsed = FactsExtraction(**resp)
    return [
        f for f in parsed.extracted_facts if f.source_name and f.target_name and f.fact
    ]


# ---------- 4. 事实消歧（时序核心） ----------


async def resolve_facts(
    storage: Storage,
    llm: LLMClient,
    extracted: list[ExtractedFactItem],
    name_map: dict[str, str],
    episode: Episode,
    group_id: str,
) -> list[Fact]:
    """消歧提取事实 → 图内事实，应用时序失效窗口。返回所有生效事实。"""
    ref_time = episode.valid_at or episode.created_at
    results: list[Fact] = []

    for item in extracted:
        src_uuid = name_map.get(item.source_name)
        tgt_uuid = name_map.get(item.target_name)
        if not src_uuid or not tgt_uuid:
            logger.debug("事实引用了未解析实体，跳过: %s", item.fact)
            continue

        related = storage.get_facts_between(src_uuid, tgt_uuid, group_id)

        # 层1: 完全匹配（归一化文本）
        norm = normalize_name(item.fact)
        matched = [f for f in related if normalize_name(f.fact) == norm]
        if matched:
            existing = matched[0]
            if episode.uuid not in existing.episodes:
                existing.episodes.append(episode.uuid)
                storage.upsert_fact(existing)
            results.append(existing)
            continue

        # 候选集：端点相同的相关事实 + 任一端点相关的失效候选
        invalidation_candidates: list[Fact] = []
        seen_ids: set[str] = {f.uuid for f in related}
        for euuid in (src_uuid, tgt_uuid):
            for f in storage.get_facts_mentioning(euuid, group_id):
                if f.uuid not in seen_ids:
                    seen_ids.add(f.uuid)
                    invalidation_candidates.append(f)
        invalidation_candidates = invalidation_candidates[:INVALIDATION_CANDIDATE_LIMIT]

        d_start, d_end = _dates_from_text(item.fact)
        new_fact = Fact(
            name=item.name,
            fact=item.fact,
            source_uuid=src_uuid,
            target_uuid=tgt_uuid,
            group_id=group_id,
            episodes=[episode.uuid],
            valid_at=_norm_iso(item.valid_at) or d_start or ref_time,
            invalid_at=_norm_iso(item.invalid_at) or d_end,
            reference_time=ref_time,
        )

        if not related and not invalidation_candidates:
            emb = await llm.embed(item.fact)
            storage.upsert_fact(new_fact, embedding=emb)
            results.append(new_fact)
            continue

        # 层2: LLM 判 duplicate / contradiction
        indexed_existing = "\n".join(f"{i}. {f.fact}" for i, f in enumerate(related))
        offset = len(related)
        indexed_inval = "\n".join(
            f"{offset + i}. {f.fact}" for i, f in enumerate(invalidation_candidates)
        )
        user = DEDUPE_EDGE_USER.format(
            new_fact=item.fact,
            indexed_existing=indexed_existing or "（无）",
            indexed_invalidation=indexed_inval or "（无）",
        )
        resp = await llm.chat(
            [
                {"role": "system", "content": DEDUPE_EDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_model=EdgeDuplicate,
        )
        decision = EdgeDuplicate(**resp)

        resolved = new_fact
        # duplicate → 复用旧事实
        for idx in decision.duplicate_facts:
            if 0 <= idx < len(related):
                resolved = related[idx]
                break

        if resolved.uuid == new_fact.uuid:
            # 全新事实才提取向量；复用的事实保持原向量
            emb = await llm.embed(item.fact)
        else:
            emb = None
            if episode.uuid not in resolved.episodes:
                resolved.episodes.append(episode.uuid)
                resolved.reference_time = ref_time

        # 矛盾 → 应用时序失效窗口
        contradicted: list[Fact] = []
        for idx in decision.contradicted_facts:
            if 0 <= idx < len(related):
                contradicted.append(related[idx])
            elif offset <= idx < offset + len(invalidation_candidates):
                contradicted.append(invalidation_candidates[idx - offset])

        if contradicted and resolved.valid_at is not None:
            # 若失效候选比新事实更新 → 新事实反而被"过期"
            newer = [c for c in contradicted if (c.valid_at or "") > resolved.valid_at]
            if newer:
                resolved.invalid_at = newer[0].valid_at
                resolved.expired_at = now_iso()
            else:
                # 新事实取代旧事实
                for c in contradicted:
                    c.invalid_at = resolved.valid_at
                    c.expired_at = now_iso()
                    storage.upsert_fact(c)

        storage.upsert_fact(resolved, embedding=emb)
        results.append(resolved)

    return results


# ---------- 5. summary 更新 ----------


async def update_summaries(
    storage: Storage, llm: LLMClient, entities: list[Entity], episode: Episode
) -> None:
    """用新 episode 更新受影响实体的 summary（每个实体一次 LLM 调用）。"""
    for ent in entities:
        user = (
            f"实体：{ent.name}\n当前摘要：{ent.summary or '（无）'}\n\n本次新增信息：\n{episode.content[:2000]}"
            '\n\n输出 JSON：{"summaries": [{"name": "...", "summary": "..."}]}'
        )
        try:
            resp = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": "你是实体摘要器。根据对话内容更新实体的长期摘要。规则：精炼、合并新旧信息、保留仍有效的事实、控制在100-200字。",
                    },
                    {"role": "user", "content": user},
                ],
                response_model=EntitySummaries,
            )
            summaries = EntitySummaries(**resp).summaries
            for s in summaries:
                if s.get("name") == ent.name and s.get("summary"):
                    ent.summary = s["summary"][:2000]
            storage.upsert_entity(ent)
        except Exception as e:
            logger.warning("summary 更新失败 %s: %s", ent.name, e)


# ---------- 主入口 ----------


async def add_episode(
    storage: Storage,
    llm: LLMClient,
    content: str,
    group_id: str = "default",
    name: str | None = None,
    source: str = "text",
    source_description: str = "",
    valid_at: str | None = None,
    extract: bool = True,
    update_summary: bool = True,
    episode: Episode | None = None,
) -> Episode:
    """摄入一个 episode 并更新知识图。返回 episode。

    episode: 可选的预构造 Episode（server 层先落库防丢后传入复用，避免重复建行）。
    """
    ep = episode or Episode(
        name=name or content[:60],
        group_id=group_id,
        content=content,
        source=source,
        source_description=source_description,
        valid_at=valid_at or now_iso(),
    )
    storage.upsert_episode(ep)

    # 原文层向量化（含 extract=False 的迁移数据；失败不阻塞，backfill 脚本补）
    try:
        emb = await llm.embed(ep.content[:2000])
        storage.save_embedding(ep.uuid, "episode", emb)
    except Exception as e:
        logger.warning("episode embedding 失败 %s: %s", ep.uuid, e)

    if not extract:
        return ep

    context = storage.get_recent_episodes(group_id, limit=10)
    context = [c for c in context if c.uuid != ep.uuid]

    # 1. 提取实体
    extracted_entities = await extract_entities(llm, ep, context)
    logger.info(
        "提取实体 %d 个: %s",
        len(extracted_entities),
        [e.name for e in extracted_entities],
    )

    # 2. 实体消歧
    entities, name_map = await resolve_entities(
        storage, llm, extracted_entities, group_id
    )
    logger.info("实体消歧后 %d 个", len(entities))

    # 3. 提取事实
    extracted_facts = await extract_facts(llm, ep, entities)
    logger.info("提取事实 %d 条", len(extracted_facts))

    # 4. 事实消歧 + 时序
    facts = await resolve_facts(storage, llm, extracted_facts, name_map, ep, group_id)
    logger.info("事实消歧后生效 %d 条", len(facts))

    # mentions
    storage.add_mentions(ep.uuid, [e.uuid for e in entities])

    # 5. summary 更新
    if update_summary:
        await update_summaries(storage, llm, entities, ep)

    return ep


# (no-op: 触发 pyright 重分析，ExtractedFactItem.valid_at 已在 prompts.py 定义)
