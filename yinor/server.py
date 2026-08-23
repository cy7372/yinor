"""yinor 本地 HTTP 服务（供 pi 扩展 / 其他客户端调用）。

端点：
  POST /episodes           摄入一条信息（异步：先存后提取）
  GET  /search?q=&group=   混合检索
  GET  /history?entity=    实体演变史
  GET  /stats?group=       统计
  GET  /episodes/{uuid}    取单条 episode
  DELETE /episodes/{uuid}  删除 episode
  GET  /health             健康检查
  GET  /                   Web 控制台（图谱可视化）
  GET  /api/entities       实体列表（控制台）
  GET  /api/facts          事实列表（控制台）
  GET  /api/episodes       episode 列表（控制台）
  GET  /api/graph          图谱数据（节点+边，控制台）

启动：python -m yinor.server  或  yinor cli serve
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .llm import LLMClient
from .memory import DEFAULT_DB_PATH, Memory, fmt_search
from .models import Episode
from .storage import Storage

# 服务进程跑在 Session 0（Servy/Windows 服务），不继承用户 shell 环境，
# 必须显式加载项目根目录的 .env（LLM_API_KEY 等）
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("YINOR_DB_PATH", DEFAULT_DB_PATH)


class AddEpisodeRequest(BaseModel):
    content: str = Field(..., description="要记忆的内容文本")
    group_id: str = "default"
    name: str | None = None
    source: str = "text"
    source_description: str = ""
    valid_at: str | None = None
    extract: bool = True
    update_summary: bool = True
    wait: bool = False  # True=同步等提取完成；False=立即返回


class SearchRequest(BaseModel):
    q: str
    group_id: str = "default"
    limit: int = 20
    as_of: str | None = None
    include_graph: bool = True


class MergeRequest(BaseModel):
    keep: str = Field(..., description="保留的实体 uuid")
    remove: str = Field(..., description="被合并掉的实体 uuid（引用全部重写到 keep）")


class LinkRequest(BaseModel):
    a: str = Field(..., description="链接一端实体 uuid")
    b: str = Field(..., description="链接另一端实体 uuid")


app = FastAPI(title="yinor", version="0.1.0")


_STORAGE_SINGLETON: Storage | None = None


def _storage() -> Storage:
    """全局单例 Storage（单一 sqlite 连接）。

    所有请求与后台任务（提取/补偿/反思）共享同一连接：asyncio 单线程下
    写操作天然串行，根治"每请求新连接"导致的多连接写锁竞争
    （database is locked）。单连接下 storage 的 busy_timeout 永不触发。
    """
    global _STORAGE_SINGLETON
    if _STORAGE_SINGLETON is None:
        _STORAGE_SINGLETON = Storage(DB_PATH)
    return _STORAGE_SINGLETON


def _memory() -> Memory:
    """复用全局单例 Storage（不拥有，close 不关闭连接）；LLMClient 无状态可每次新建。"""
    return Memory(storage=_storage(), llm=LLMClient())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/episodes")
async def add_episode(req: AddEpisodeRequest) -> dict[str, Any]:
    from .pipeline import add_episode as pipeline_add

    mem = _memory()
    try:
        ep = Episode(
            name=req.name or req.content[:60],
            group_id=req.group_id,
            content=req.content,
            source=req.source,
            source_description=req.source_description,
            valid_at=req.valid_at,
        )
        # 先落库（保证不丢），提取异步跑
        from .models import now_iso

        ep.valid_at = ep.valid_at or now_iso()
        mem.storage.upsert_episode(ep)

        if not req.wait:
            # 后台任务：提取（用独立连接，避免请求关闭后数据库不可用）
            # 传同一个 ep：管线内部复用该 uuid 落库，不会重复建行
            async def _extract() -> None:
                bg = _memory()
                try:
                    await pipeline_add(
                        storage=bg.storage,
                        llm=bg.llm,
                        content=req.content,
                        group_id=req.group_id,
                        name=req.name,
                        source=req.source,
                        source_description=req.source_description,
                        valid_at=ep.valid_at,
                        extract=True,
                        update_summary=req.update_summary,
                        episode=ep,
                    )
                except Exception as e:
                    logger.error("后台提取失败: %s", e)
                finally:
                    bg.close()

            asyncio.create_task(_extract())
            return {"status": "queued", "episode_uuid": ep.uuid}
        else:
            await pipeline_add(
                storage=mem.storage,
                llm=mem.llm,
                content=req.content,
                group_id=req.group_id,
                name=req.name,
                source=req.source,
                source_description=req.source_description,
                valid_at=ep.valid_at,
                extract=req.extract,
                update_summary=req.update_summary,
                episode=ep,
            )
            return {"status": "done", "episode_uuid": ep.uuid}
    finally:
        mem.close()


@app.get("/search")
async def search(
    q: str = Query(...),
    group_id: str = "default",
    limit: int = 20,
    as_of: str | None = None,
    include_graph: bool = True,
) -> dict[str, Any]:
    mem = _memory()
    try:
        resp = await mem.search(
            q, group_id=group_id, limit=limit, as_of=as_of, include_graph=include_graph
        )
        return {
            "query": resp.query,
            "elapsed_ms": resp.elapsed_ms,
            "facts": [f.model_dump() for f in resp.facts],
            "entities": resp.entities,
            "episodes": resp.episodes,
            "text": fmt_search(resp),
        }
    finally:
        mem.close()


@app.get("/history")
async def history(
    entity: str = Query(...),
    group_id: str = "default",
    limit: int = 20,
) -> dict[str, Any]:
    mem = _memory()
    try:
        results = await mem.history(entity, group_id=group_id, limit=limit)
        return {"entity": entity, "facts": [f.model_dump() for f in results]}
    finally:
        mem.close()


@app.get("/stats")
async def stats(group_id: str | None = None) -> dict[str, int]:
    mem = _memory()
    try:
        return mem.stats(group_id)
    finally:
        mem.close()


# ── Web 控制台 ─────────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def console() -> FileResponse:
    # no-cache：迭代频繁，防止旧 HTML 缓存（残留 CDN 引用等）
    return FileResponse(
        STATIC_DIR / "console.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/static/vis-network.min.js", include_in_schema=False)
async def vis_js() -> FileResponse:
    """vis-network 本地化（jsdelivr CDN 国内不稳，2026-08-10 vendored 9.1.9）。"""
    return FileResponse(
        STATIC_DIR / "vis-network.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/static/favicon.svg", include_in_schema=False)
async def favicon_svg() -> FileResponse:
    """品牌 favicon 矢量版（印·图谱：核心节点+四角辐射，2026-08-13 加入）。"""
    return FileResponse(
        STATIC_DIR / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/static/favicon-32.png", include_in_schema=False)
async def favicon_png() -> FileResponse:
    """favicon PNG fallback（不支持 SVG favicon 的旧浏览器）。"""
    return FileResponse(
        STATIC_DIR / "favicon-32.png",
        media_type="image/png",
        headers={"Cache-Control": "max-age=86400"},
    )


@app.get("/api/groups")
async def api_groups() -> dict[str, Any]:
    """分区列表：每个 group_id 的 episodes/entities/facts 计数（含全部视图用）。"""
    mem = _memory()
    try:
        ep = mem.storage.conn.execute(
            "SELECT group_id, COUNT(*) AS c FROM episodes GROUP BY group_id", ()
        ).fetchall()
        en = mem.storage.conn.execute(
            "SELECT group_id, COUNT(*) AS c FROM entities GROUP BY group_id", ()
        ).fetchall()
        fa = mem.storage.conn.execute(
            "SELECT group_id, COUNT(*) AS c FROM facts "
            "WHERE expired_at IS NULL GROUP BY group_id",
            (),
        ).fetchall()
        groups: dict[str, dict[str, int]] = {}
        for r in ep:
            groups.setdefault(r["group_id"], {})["episodes"] = r["c"]
        for r in en:
            groups.setdefault(r["group_id"], {})["entities"] = r["c"]
        for r in fa:
            groups.setdefault(r["group_id"], {})["facts"] = r["c"]
        return {
            "groups": [
                {
                    "group_id": g,
                    "episodes": v.get("episodes", 0),
                    "entities": v.get("entities", 0),
                    "facts": v.get("facts", 0),
                }
                for g, v in sorted(groups.items())
            ]
        }
    finally:
        mem.close()


@app.get("/api/entities")
async def api_entities(group_id: str = "default") -> dict[str, Any]:
    mem = _memory()
    try:
        if group_id == "all":
            rows = mem.storage.conn.execute(
                "SELECT uuid, name, labels, summary, created_at, group_id FROM entities "
                "ORDER BY created_at DESC",
                (),
            ).fetchall()
        else:
            rows = mem.storage.conn.execute(
                "SELECT uuid, name, labels, summary, created_at, group_id FROM entities "
                "WHERE group_id = ? ORDER BY created_at DESC",
                (group_id,),
            ).fetchall()
        import json as _json

        return {
            "entities": [
                {
                    "uuid": r["uuid"],
                    "name": r["name"],
                    "labels": _json.loads(r["labels"] or "[]"),
                    "summary": r["summary"] or "",
                    "created_at": r["created_at"],
                    "group_id": r["group_id"],
                }
                for r in rows
            ]
        }
    finally:
        mem.close()


@app.get("/api/facts")
async def api_facts(
    group_id: str = "default", include_expired: bool = False
) -> dict[str, Any]:
    mem = _memory()
    try:
        if include_expired and group_id == "all":
            rows = mem.storage.conn.execute(
                "SELECT f.uuid, f.name, f.fact, f.valid_at, f.invalid_at, f.expired_at, "
                "f.episodes, f.group_id, "
                "s.name AS source_name, t.name AS target_name "
                "FROM facts f "
                "JOIN entities s ON s.uuid = f.source_uuid "
                "JOIN entities t ON t.uuid = f.target_uuid "
                "ORDER BY f.valid_at DESC",
                (),
            ).fetchall()
        elif include_expired:
            rows = mem.storage.conn.execute(
                "SELECT f.uuid, f.name, f.fact, f.valid_at, f.invalid_at, f.expired_at, "
                "f.episodes, f.group_id, "
                "s.name AS source_name, t.name AS target_name "
                "FROM facts f "
                "JOIN entities s ON s.uuid = f.source_uuid "
                "JOIN entities t ON t.uuid = f.target_uuid "
                "WHERE f.group_id = ? ORDER BY f.valid_at DESC",
                (group_id,),
            ).fetchall()
        elif group_id == "all":
            rows = mem.storage.conn.execute(
                "SELECT f.uuid, f.name, f.fact, f.valid_at, f.invalid_at, f.expired_at, "
                "f.episodes, f.group_id, "
                "s.name AS source_name, t.name AS target_name "
                "FROM facts f "
                "JOIN entities s ON s.uuid = f.source_uuid "
                "JOIN entities t ON t.uuid = f.target_uuid "
                "WHERE f.expired_at IS NULL ORDER BY f.valid_at DESC",
                (),
            ).fetchall()
        else:
            rows = mem.storage.conn.execute(
                "SELECT f.uuid, f.name, f.fact, f.valid_at, f.invalid_at, f.expired_at, "
                "f.episodes, f.group_id, "
                "s.name AS source_name, t.name AS target_name "
                "FROM facts f "
                "JOIN entities s ON s.uuid = f.source_uuid "
                "JOIN entities t ON t.uuid = f.target_uuid "
                "WHERE f.group_id = ? AND f.expired_at IS NULL ORDER BY f.valid_at DESC",
                (group_id,),
            ).fetchall()
        import json as _json

        facts = []
        for r in rows:
            d = dict(r)
            d["episodes"] = _json.loads(d["episodes"] or "[]")
            facts.append(d)
        return {"facts": facts}
    finally:
        mem.close()


@app.get("/api/episodes")
async def api_episodes(group_id: str = "default", limit: int = 100) -> dict[str, Any]:
    mem = _memory()
    try:
        if group_id == "all":
            rows = mem.storage.conn.execute(
                "SELECT uuid, name, source, content, created_at, valid_at, group_id "
                "FROM episodes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = mem.storage.conn.execute(
                "SELECT uuid, name, source, content, created_at, valid_at, group_id "
                "FROM episodes WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return {
            "episodes": [
                {
                    "uuid": r["uuid"],
                    "name": r["name"],
                    "source": r["source"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "valid_at": r["valid_at"],
                    "group_id": r["group_id"],
                }
                for r in rows
            ]
        }
    finally:
        mem.close()


@app.get("/api/graph")
async def api_graph(group_id: str = "default") -> dict[str, Any]:
    """图谱数据：实体为节点，当前有效事实为边（vis-network 直接可用）。"""
    mem = _memory()
    try:
        if group_id == "all":
            nodes = mem.storage.conn.execute(
                "SELECT uuid, name, labels, summary, group_id FROM entities", ()
            ).fetchall()
            edges = mem.storage.conn.execute(
                "SELECT uuid, name, fact, source_uuid, target_uuid, valid_at, "
                "invalid_at, group_id FROM facts WHERE expired_at IS NULL",
                (),
            ).fetchall()
        else:
            nodes = mem.storage.conn.execute(
                "SELECT uuid, name, labels, summary, group_id FROM entities "
                "WHERE group_id = ?",
                (group_id,),
            ).fetchall()
            edges = mem.storage.conn.execute(
                "SELECT uuid, name, fact, source_uuid, target_uuid, valid_at, "
                "invalid_at, group_id FROM facts "
                "WHERE group_id = ? AND expired_at IS NULL",
                (group_id,),
            ).fetchall()
        import json as _json

        node_ids = {n["uuid"] for n in nodes}
        links = [
            {"from": link["a_uuid"], "to": link["b_uuid"], "kind": link["kind"]}
            for link in mem.storage.get_all_links()
            if link["a_uuid"] in node_ids and link["b_uuid"] in node_ids
        ]

        return {
            "links": links,
            "nodes": [
                {
                    "id": n["uuid"],
                    "label": n["name"],
                    "title": n["summary"] or n["name"],
                    "labels": _json.loads(n["labels"] or "[]"),
                    "group": n["group_id"],
                }
                for n in nodes
            ],
            "edges": [
                {
                    "id": e["uuid"],
                    "from": e["source_uuid"],
                    "to": e["target_uuid"],
                    "label": e["name"],
                    "title": e["fact"],
                    "active": e["invalid_at"] is None,
                    "group": e["group_id"],
                }
                for e in edges
            ],
        }
    finally:
        mem.close()


@app.get("/episodes/{uuid}")
async def get_episode(uuid: str) -> dict[str, Any]:
    mem = _memory()
    try:
        ep = mem.get_episode(uuid)
        if not ep:
            raise HTTPException(status_code=404, detail="episode not found")
        return ep.model_dump()
    finally:
        mem.close()


@app.delete("/episodes/{uuid}")
async def delete_episode(uuid: str) -> dict[str, str]:
    mem = _memory()
    try:
        await mem.forget(uuid)
        return {"status": "deleted", "episode_uuid": uuid}
    finally:
        mem.close()


@app.post("/api/facts/{uuid}/invalidate")
async def api_fact_invalidate(uuid: str) -> dict[str, Any]:
    """手动失效一条事实（软删除：invalid_at=expired_at=now）。"""
    mem = _memory()
    try:
        f = mem.storage.invalidate_fact(uuid)
        if not f:
            raise HTTPException(404, f"事实不存在: {uuid}")
        return {"ok": True, "uuid": f.uuid, "invalid_at": f.invalid_at}
    finally:
        mem.close()


@app.delete("/api/facts/{uuid}")
async def api_fact_delete(uuid: str) -> dict[str, Any]:
    """硬删除一条事实（含 embedding/FTS 索引）。"""
    mem = _memory()
    try:
        if not mem.storage.get_fact(uuid):
            raise HTTPException(404, f"事实不存在: {uuid}")
        mem.storage.delete_fact(uuid)
        return {"ok": True, "deleted": uuid}
    finally:
        mem.close()


@app.post("/api/entities/merge")
async def api_entity_merge(req: MergeRequest) -> dict[str, Any]:
    """实体合并：remove 的引用重写到 keep，然后删除 remove。"""
    mem = _memory()
    try:
        result = mem.storage.merge_entities(req.keep, req.remove)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    finally:
        mem.close()


@app.get("/api/links/candidates")
async def api_link_candidates() -> dict[str, Any]:
    """跨分区 same_as 链接候选（预览：归一化/alnum 桶的跨组配对）。"""
    from .dedup import find_link_candidates

    mem = _memory()
    try:
        return {"candidates": find_link_candidates(mem.storage)}
    finally:
        mem.close()


@app.post("/api/links")
async def api_link_add(req: LinkRequest) -> dict[str, Any]:
    """手动建立实体链接（same_as 逻辑互认，不合并实体）。"""
    mem = _memory()
    try:
        mem.storage.add_link(req.a, req.b)
        return {"ok": True}
    finally:
        mem.close()


@app.delete("/api/links")
async def api_link_remove(a: str, b: str) -> dict[str, Any]:
    """拆除实体链接。"""
    mem = _memory()
    try:
        return {"ok": True, "removed": mem.storage.remove_link(a, b)}
    finally:
        mem.close()


@app.post("/api/links/auto")
async def api_link_auto(
    dry_run: bool = Query(False, description="True=只返回候选不写库"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """自动建立安全跨组链接（归一化同名/分隔符差异，确定性规则，scope/路径排除）。"""
    from .dedup import run_auto_link

    mem = _memory()
    try:
        return run_auto_link(mem.storage, dry_run=dry_run, limit=limit)
    finally:
        mem.close()


@app.get("/api/quality")
async def api_quality(
    group_id: str = Query(
        "all", description="分区过滤；all=全部分区（与列表端点一致）"
    ),
) -> dict[str, Any]:
    """数据质量扫描：短事实候选 + 实体合并候选（含自动合并标记）。

    group_id 与 /api/facts 等列表端点同语义：all 全量，否则只扫该分区——
    前端质检视图随分区选择器联动。
    """
    mem = _memory()
    try:
        st = mem.storage
        # 1. 短事实（过短的陈述可能是低质量提取，人工判断）
        if group_id != "all":
            rows = st.conn.execute(
                """SELECT f.uuid, f.fact, f.name, f.group_id,
                          s.name AS source_name, t.name AS target_name
                   FROM facts f
                   JOIN entities s ON s.uuid = f.source_uuid
                   JOIN entities t ON t.uuid = f.target_uuid
                   WHERE f.expired_at IS NULL AND length(f.fact) < 20
                         AND f.group_id = ?
                   ORDER BY f.group_id, length(f.fact)""",
                (group_id,),
            ).fetchall()
        else:
            rows = st.conn.execute(
                """SELECT f.uuid, f.fact, f.name, f.group_id,
                          s.name AS source_name, t.name AS target_name
                   FROM facts f
                   JOIN entities s ON s.uuid = f.source_uuid
                   JOIN entities t ON t.uuid = f.target_uuid
                   WHERE f.expired_at IS NULL AND length(f.fact) < 20
                   ORDER BY f.group_id, length(f.fact)"""
            ).fetchall()
        weak_facts = [{**dict(r), "reason": "事实过短"} for r in rows]

        # 2. 实体合并候选（含 auto_merge 标签，供一键合并 / 后台自动用）——统一收口于 dedup
        from .dedup import find_candidates

        merge_candidates = find_candidates(st, group_id=group_id)
        return {"weak_facts": weak_facts, "merge_candidates": merge_candidates}
    finally:
        mem.close()


@app.post("/api/dedup")
async def api_dedup(
    dry_run: bool = Query(False, description="True=只返回候选不写库"),
    group_id: str | None = Query(None, description="指定分区；缺省=all 扫全部分区"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """自动合并安全候选（同名 / 分隔符 / 路径 / 域名 www / 中英对照），跳过 scope 包等误报。

    dry_run 预览将要合并的对；不传或 False 则执行。复用 storage.merge_entities
    （labels 合并、facts/mentions 引用重写、删 remove 含 embedding/FTS）。
    """
    from .dedup import run_auto_merge

    mem = _memory()
    try:
        return run_auto_merge(
            mem.storage, group_id=group_id, dry_run=dry_run, limit=limit
        )
    finally:
        mem.close()


@app.post("/api/episodes/backfill")
async def api_backfill(
    limit: int = Query(20, ge=1, le=500),
    strategy: str | None = Query(
        None,
        description="None=常规补偿（非迁移）; migration_quality=迁移高质量（宽关键词∩长文）",
    ),
) -> dict[str, Any]:
    """补偿提取（fire-and-forget）：无 mentions 的 episode 重跑提取。

    后台任务执行、立即返回——HTTP 长请求客户端断连会取消提取（已知坑）。
    strategy=migration_quality：选择性提取迁移数据（2026-08-13 ④），
    筛选=迁移类源 ∩ 宽关键词 ∩ length>200，实测子集 ~141 条。
    """
    asyncio.create_task(_run_backfill(limit, strategy=strategy))
    return {"status": "queued", "limit": limit, "strategy": strategy}


_BACKFILL_LOCK = asyncio.Lock()  # 串行：并发补偿会交叉写库，sqlite 同步等锁阻塞事件循环


async def _run_backfill(
    limit: int,
    window_days: int | None = None,
    strategy: str | None = None,
    concurrency: int = 1,
) -> int:
    """补偿提取共用循环：全局锁串行，每条独立连接，单条失败不中断整体。

    concurrency>1 时用 Semaphore 并发提取（LLM 等待是真并发 IO；sqlite 写
    在单例 Storage + 事件循环单线程下天然串行安全）。返回成功条数（调用方
    用于故障熔断：处理率过低说明 LLM 故障期）。锁被占用时返回 -1。
    """
    if _BACKFILL_LOCK.locked():
        logger.info("补偿提取已在运行，跳过本次")
        return -1
    from .pipeline import add_episode as pipeline_add

    async with _BACKFILL_LOCK:
        mem = _memory()
        try:
            if strategy == "migration_all":
                # 迁移全量消化：所有无 mentions 迁移源，短文优先（快速见效）
                rows = mem.storage.conn.execute(
                    """SELECT p.* FROM episodes p
                       WHERE p.source IN ('migration', 'migration-local',
                                          'failures-md', 'migrate-user-md')
                         AND NOT EXISTS (
                             SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)
                       ORDER BY length(p.content) ASC LIMIT ?""",
                    (limit,),
                ).fetchall()
            elif strategy == "migration_quality":
                # 迁移数据选择性提取：宽关键词 ∩ 长文 ∩ 无 mentions（~141 条子集）
                rows = mem.storage.conn.execute(
                    """SELECT p.* FROM episodes p
                       WHERE p.source IN ('migration', 'migration-local',
                                          'failures-md', 'migrate-user-md')
                         AND length(p.content) > 200
                         AND (p.content LIKE '%端口%' OR p.content LIKE '%配置%'
                              OR p.content LIKE '%决策%' OR p.content LIKE '%部署%'
                              OR p.content LIKE '%修复%' OR p.content LIKE '%网关%'
                              OR p.content LIKE '%yinor%' OR p.content LIKE '%记忆%'
                              OR p.content LIKE '%搜索%' OR p.content LIKE '%模型%')
                         AND NOT EXISTS (
                             SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            elif window_days:
                rows = mem.storage.conn.execute(
                    """SELECT p.* FROM episodes p
                       WHERE p.source NOT IN ('migration', 'migration-local')
                         AND datetime(p.created_at) > datetime('now', '-7 day')
                         AND NOT EXISTS (
                             SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = mem.storage.conn.execute(
                    """SELECT p.* FROM episodes p
                       WHERE p.source NOT IN ('migration', 'migration-local')
                         AND NOT EXISTS (
                             SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        finally:
            mem.close()

        ok = 0
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(r: Any) -> None:
            nonlocal ok
            async with sem:
                mem2 = _memory()
                try:
                    await pipeline_add(
                        storage=mem2.storage,
                        llm=mem2.llm,
                        content=r["content"],
                        group_id=r["group_id"],
                        name=r["name"],
                        source=r["source"],
                        source_description=r["source_description"],
                        valid_at=r["valid_at"],
                        extract=True,
                        update_summary=True,
                        episode=_episode_from_row(r),
                    )
                    ok += 1
                    logger.info("补偿提取完成: %s", r["uuid"])
                except Exception as e:  # noqa: BLE001 —— 补偿任务需全部跑完
                    logger.warning("补偿提取失败 %s: %s", r["uuid"], e)
                finally:
                    mem2.close()
                # 节流 + 被动检查点：给其他写入方让出锁窗口，分散 WAL checkpoint 压力
                mem3 = _memory()
                try:
                    mem3.storage.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                finally:
                    mem3.close()
                await asyncio.sleep(2)

        await asyncio.gather(*[_one(r) for r in rows])
        if rows:
            logger.info("补偿提取结束: %d/%d", ok, len(rows))
        return ok


@app.post("/api/reflect")
async def api_reflect(
    group_id: str = Query("yinor"),
    days: int = Query(7, ge=1, le=90),
    max_insights: int = Query(3, ge=1, le=10),
) -> dict[str, Any]:
    """反思（fire-and-forget）：近期记忆 → 高层洞察 → 写回为 reflection episode。

    参考 Generative Agents 的 reflection：方法层从知识层生长的机制。
    后台任务执行、立即返回——同补偿提取，HTTP 长请求断连会取消管线（已知坑）。
    """
    asyncio.create_task(_run_reflect(group_id, days, max_insights))
    return {"status": "queued", "group_id": group_id, "days": days}


async def _run_reflect(group_id: str, days: int, max_insights: int) -> None:
    """反思后台任务：与补偿提取共用全局锁串行，每条洞察独立连接走提取管线。"""
    if _BACKFILL_LOCK.locked():
        logger.info("反思跳过：批量写任务进行中")
        return
    from .pipeline import add_episode as pipeline_add
    from .reflect import reflect

    async with _BACKFILL_LOCK:
        mem = _memory()
        try:
            insights = await reflect(mem.storage, mem.llm, group_id, days, max_insights)
        except Exception as e:  # noqa: BLE001 —— 反思失败不影响服务
            logger.warning("反思失败: %s", e)
            return
        finally:
            mem.close()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for i, insight in enumerate(insights):
            mem2 = _memory()
            try:
                ep = await pipeline_add(
                    storage=mem2.storage,
                    llm=mem2.llm,
                    content=insight,
                    group_id=group_id,
                    name=f"反思洞察 {today} #{i + 1}",
                    source="reflection",
                    source_description=f"近 {days} 天记忆综合",
                    extract=True,
                    update_summary=True,
                )
                logger.info("反思洞察已入库: %s", ep.uuid)
            except Exception as e:  # noqa: BLE001 —— 洞察需全部尝试
                logger.warning("反思洞察入库失败: %s", e)
            finally:
                mem2.close()
            await asyncio.sleep(2)  # 节流：与补偿提取同，让出写锁窗口
        if insights:
            logger.info("反思写回完成: group=%s %d 条", group_id, len(insights))


def _episode_from_row(r: Any) -> Episode:
    from .models import Episode as _Ep

    return _Ep(
        uuid=r["uuid"],
        name=r["name"],
        group_id=r["group_id"],
        content=r["content"],
        source=r["source"],
        source_description=r["source_description"],
        created_at=r["created_at"],
        valid_at=r["valid_at"],
    )


@app.on_event("startup")
async def _schedule_backfill() -> None:
    """启动后 60s 自动补偿提取一批（近 7 天窗口，不阻塞服务就绪）。"""

    async def _run() -> None:
        await asyncio.sleep(60)
        # 迁移消化期（存量>0）让路：消化任务 t+90s 直接拿锁，restart 恢复快
        mem = _memory()
        try:
            migration_left = mem.storage.conn.execute(
                """SELECT COUNT(*) FROM episodes p
                   WHERE p.source IN ('migration', 'migration-local',
                                      'failures-md', 'migrate-user-md')
                     AND NOT EXISTS (
                         SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)"""
            ).fetchone()[0]
        finally:
            mem.close()
        if migration_left > 0:
            logger.info("迁移存量 %d 条，补偿提取让路给迁移消化", migration_left)
            return
        try:
            await _run_backfill(10, window_days=7)
        except Exception as e:  # noqa: BLE001
            logger.warning("启动补偿任务异常: %s", e)

    asyncio.create_task(_run())


async def _run_dedup() -> None:
    """后台自动合并安全候选（同名/分隔符/路径/域名/中英对照）。

    与补偿提取/反思共用 _BACKFILL_LOCK 串行——避免与提取管线抢 sqlite 写锁。
    只合并 auto_merge=True 的候选；scope 包/路径↔非路径等误报天然跳过。
    """
    if _BACKFILL_LOCK.locked():
        logger.info("自动合并跳过：批量任务进行中")
        return
    from .dedup import run_auto_merge

    async with _BACKFILL_LOCK:
        mem = _memory()
        try:
            r = run_auto_merge(mem.storage, dry_run=False, limit=50)
            if r.get("merged"):
                logger.info("自动合并完成: %d 对", r["merged"])
            from .dedup import run_auto_link

            lr = run_auto_link(mem.storage, limit=100)
            if lr.get("linked"):
                logger.info("自动链接完成: %d 对", lr["linked"])
        except Exception as e:  # noqa: BLE001
            logger.warning("自动合并失败: %s", e)
        finally:
            mem.close()


@app.on_event("startup")
async def _schedule_dedup() -> None:
    """启动后 120s 跑一次自动合并（避开 60s 的补偿提取），之后每 24h 一次。"""

    async def _run() -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await _run_dedup()
            except Exception as e:  # noqa: BLE001
                logger.warning("定时合并异常: %s", e)
            await asyncio.sleep(24 * 3600)

    asyncio.create_task(_run())


@app.on_event("startup")
async def _schedule_migration_digest() -> None:
    """迁移消化：t+90s 检测未提取迁移存量，>0 自动启动全量提取（并发 3）。

    NOT EXISTS mentions 幂等——服务重启后自动跳过已完成条目续跑（2026-08-13 用户
    决定全量重处理 4787 条迁移记忆，短文优先，实测预计 3-4 天）。
    消化期间 dedup 24h 周期会被锁跳过（层 0 源头拦截大部分重复，消化完统一清理）。
    """

    async def _run() -> None:
        await asyncio.sleep(90)
        mem = _memory()
        try:
            n = mem.storage.conn.execute(
                """SELECT COUNT(*) FROM episodes p
                   WHERE p.source IN ('migration', 'migration-local',
                                      'failures-md', 'migrate-user-md')
                     AND NOT EXISTS (
                         SELECT 1 FROM mentions m WHERE m.episode_uuid = p.uuid)"""
            ).fetchone()[0]
        finally:
            mem.close()
        if n <= 0:
            return
        logger.info("迁移消化: %d 条待提取，启动全量任务（并发 3）", n)
        # 锁可能被 startup backfill(t+60s)/dedup 持有——重试循环直到拿到锁跑完，
        # 防 _run_backfill 的「锁持有即跳过」把消化任务永久吞掉
        while True:
            if not _BACKFILL_LOCK.locked():
                ok_n = await _run_backfill(
                    limit=5000, strategy="migration_all", concurrency=3
                )
                mem2 = _memory()
                try:
                    left = mem2.storage.conn.execute(
                        """SELECT COUNT(*) FROM episodes p
                           WHERE p.source IN ('migration', 'migration-local',
                                              'failures-md', 'migrate-user-md')
                             AND NOT EXISTS (
                                 SELECT 1 FROM mentions m
                                 WHERE m.episode_uuid = p.uuid)"""
                    ).fetchone()[0]
                finally:
                    mem2.close()
                if left == 0:
                    logger.info("迁移消化完成：全部提取完毕")
                    return
                # 故障熔断：处理率 <10% 说明 LLM 在故障期（2026-08-13 并发5
                # 把上游 LLM 打过载的教训），拉长重试间隔防无限空转烧请求
                if 0 <= ok_n < max(1, n // 10):
                    logger.warning(
                        "迁移消化处理率过低(%d/%d)，疑似 LLM 故障，30 分钟后重试",
                        ok_n,
                        n,
                    )
                    await asyncio.sleep(1800)
                else:
                    await asyncio.sleep(300)  # 被跳过/部分完成，5 分钟后续跑
            else:
                await asyncio.sleep(60)

    asyncio.create_task(_run())


def _resolve_port() -> int:
    raw = os.environ.get("YINOR_PORT", "20102")
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"YINOR_PORT 无效: {raw!r}（应为整数端口）") from None


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    try:
        uvicorn.run(app, host="127.0.0.1", port=_resolve_port())
    except OSError as e:
        raise SystemExit(f"yinor 服务启动失败（端口可能已被占用）: {e}") from e
