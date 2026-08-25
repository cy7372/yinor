"""SQLite 存储层：schema 初始化、CRUD、FTS5、向量存取、邻域查询。

同步 sqlite3（内存快、逻辑简单），服务层（FastAPI）用线程池执行即可。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from .frozen import resource_path
from .models import Entity, Episode, Fact, now_iso

SCHEMA_PATH = resource_path("schema.sql")

# CJK 字符范围（基本块+扩展A+兼容+全角），用于 FTS 预切分
_CJK = r"\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
_CJK_CJK_RE = re.compile(rf"([{_CJK}])(?=[{_CJK}])")
_CJK_LAT_RE = re.compile(rf"([{_CJK}])(?=[0-9A-Za-z])")
_LAT_CJK_RE = re.compile(rf"([0-9A-Za-z])(?=[{_CJK}])")


def fts_text(s: str) -> str:
    """FTS5 预切分：CJK 字符间及 CJK↔英数边界插空格。

    unicode61 分词把整段中文当单 token（中文召回全废）；切成单字 token 后
    查询侧同构转换做短语匹配，子串召回与 Lucene 中文单字模式等效。
    """
    s = s or ""
    s = _CJK_CJK_RE.sub(r"\1 ", s)
    s = _CJK_LAT_RE.sub(r"\1 ", s)
    s = _LAT_CJK_RE.sub(r"\1 ", s)
    return s


def _parse_json(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError as _:
        return default


class Storage:
    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.db_path, timeout=5.0
        )  # busy_timeout 5s：同步 API 等锁会阻塞事件循环，上限必须短
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "PRAGMA synchronous=NORMAL"
        )  # WAL 下安全且大幅减少 fsync 卡顿
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # ---------- init ----------

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_fts()
        self._migrate_mentions_uniq()
        self.conn.commit()

    def _migrate_mentions_uniq(self) -> None:
        """mentions 存量去重 + 唯一索引（幂等）。

        历史坑（2026-08-13 实测）：表无 UNIQUE 约束，add_mentions 的
        INSERT OR IGNORE 形同虚设——双提取场景（wait=true 客户端断连取消 +
        startup backfill 重跑同一 episode）会把同一 (episode, entity) 双写。
        先删重复（保 rowid 最小），再建唯一索引让 OR IGNORE 真正生效。
        """
        self.conn.execute(
            """DELETE FROM mentions WHERE rowid NOT IN (
                   SELECT MIN(rowid) FROM mentions
                   GROUP BY episode_uuid, entity_uuid)"""
        )
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS mentions_pair_uniq
               ON mentions(episode_uuid, entity_uuid)"""
        )

    def _migrate_fts(self) -> None:
        """旧库迁移：content= 外部内容 FTS + 触发器 → standalone + Python 同步（幂等）。"""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='facts_fts'"
        ).fetchone()
        if not row or "content=" not in (row["sql"] or ""):
            return  # 新库或已迁移
        self.conn.executescript(
            """DROP TRIGGER IF EXISTS facts_ai;
               DROP TRIGGER IF EXISTS facts_ad;
               DROP TRIGGER IF EXISTS facts_au;
               DROP TRIGGER IF EXISTS entities_ai;
               DROP TRIGGER IF EXISTS entities_ad;
               DROP TRIGGER IF EXISTS entities_au;
               DROP TRIGGER IF EXISTS episodes_ai;
               DROP TRIGGER IF EXISTS episodes_ad;
               DROP TRIGGER IF EXISTS episodes_au;
               DROP TABLE IF EXISTS facts_fts;
               DROP TABLE IF EXISTS entities_fts;
               DROP TABLE IF EXISTS episodes_fts;"""
        )
        # 按新 schema 重建 standalone FTS 表并全量回填
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._rebuild_fts()

    def _rebuild_fts(self) -> None:
        for r in self.conn.execute("SELECT rowid, fact, name FROM facts").fetchall():
            self.conn.execute(
                "INSERT INTO facts_fts(rowid, fact, name) VALUES (?, ?, ?)",
                (r["rowid"], fts_text(r["fact"]), fts_text(r["name"])),
            )
        for r in self.conn.execute(
            "SELECT rowid, name, summary FROM entities"
        ).fetchall():
            self.conn.execute(
                "INSERT INTO entities_fts(rowid, name, summary) VALUES (?, ?, ?)",
                (r["rowid"], fts_text(r["name"]), fts_text(r["summary"])),
            )
        for r in self.conn.execute(
            "SELECT rowid, content, name FROM episodes"
        ).fetchall():
            self.conn.execute(
                "INSERT INTO episodes_fts(rowid, content, name) VALUES (?, ?, ?)",
                (r["rowid"], fts_text(r["content"]), fts_text(r["name"])),
            )

    def close(self) -> None:
        self.conn.close()

    # ---------- episodes ----------

    def upsert_episode(self, ep: Episode) -> None:
        self.conn.execute(
            """INSERT INTO episodes (uuid, name, group_id, source, source_description, content, created_at, valid_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET
                 name=excluded.name, source=excluded.source,
                 source_description=excluded.source_description,
                 content=excluded.content, valid_at=excluded.valid_at""",
            (
                ep.uuid,
                ep.name,
                ep.group_id,
                ep.source,
                ep.source_description,
                ep.content,
                ep.created_at,
                ep.valid_at,
            ),
        )
        rowid = self.conn.execute(
            "SELECT rowid FROM episodes WHERE uuid=?", (ep.uuid,)
        ).fetchone()["rowid"]
        self.conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (rowid,))
        self.conn.execute(
            "INSERT INTO episodes_fts(rowid, content, name) VALUES (?, ?, ?)",
            (rowid, fts_text(ep.content), fts_text(ep.name)),
        )
        self.conn.commit()

    def get_episode(self, uuid: str) -> Episode | None:
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE uuid=?", (uuid,)
        ).fetchone()
        return self._episode_from_row(row) if row else None

    def get_recent_episodes(self, group_id: str, limit: int = 10) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE group_id=? ORDER BY COALESCE(valid_at, created_at) DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
        return [self._episode_from_row(r) for r in rows]

    def delete_episode(self, uuid: str) -> None:
        """删除 episode 及其 mentions；连带删除只被该 episode 引用的事实。"""
        # 找到只被该 episode 引用的事实（episodes 数组只含这一个 uuid）
        rows = self.conn.execute(
            """SELECT f.uuid FROM facts f
               WHERE json_array_length(f.episodes) = 1
                 AND (SELECT value FROM json_each(f.episodes)) = ?""",
            (uuid,),
        ).fetchall()
        for r in rows:
            self.delete_fact(r["uuid"])
        self.conn.execute("DELETE FROM embeddings WHERE uuid=?", (uuid,))
        r = self.conn.execute(
            "SELECT rowid FROM episodes WHERE uuid=?", (uuid,)
        ).fetchone()
        if r:
            self.conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (r["rowid"],))
        self.conn.execute("DELETE FROM mentions WHERE episode_uuid=?", (uuid,))
        self.conn.execute("DELETE FROM episodes WHERE uuid=?", (uuid,))
        self.conn.commit()

    @staticmethod
    def _episode_from_row(row: sqlite3.Row) -> Episode:
        return Episode(
            uuid=row["uuid"],
            name=row["name"],
            group_id=row["group_id"],
            content=row["content"],
            source=row["source"],
            source_description=row["source_description"],
            created_at=row["created_at"],
            valid_at=row["valid_at"],
        )

    # ---------- entities ----------

    def upsert_entity(self, ent: Entity, embedding: list[float] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO entities (uuid, name, group_id, labels, summary, attributes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET
                 name=excluded.name, labels=excluded.labels, summary=excluded.summary,
                 attributes=excluded.attributes""",
            (
                ent.uuid,
                ent.name,
                ent.group_id,
                json.dumps(ent.labels, ensure_ascii=False),
                ent.summary,
                json.dumps(ent.attributes, ensure_ascii=False),
                ent.created_at,
            ),
        )
        rowid = self.conn.execute(
            "SELECT rowid FROM entities WHERE uuid=?", (ent.uuid,)
        ).fetchone()["rowid"]
        self.conn.execute("DELETE FROM entities_fts WHERE rowid=?", (rowid,))
        self.conn.execute(
            "INSERT INTO entities_fts(rowid, name, summary) VALUES (?, ?, ?)",
            (rowid, fts_text(ent.name), fts_text(ent.summary)),
        )
        if embedding is not None:
            self.save_embedding(ent.uuid, "entity", embedding)
        self.conn.commit()

    def get_entity(self, uuid: str) -> Entity | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE uuid=?", (uuid,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_entities_by_name(self, name: str, group_id: str) -> list[Entity]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE name=? AND group_id=?", (name, group_id)
        ).fetchall()
        return [self._entity_from_row(r) for r in rows]

    def get_all_entities(self, group_id: str) -> list[Entity]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE group_id=?", (group_id,)
        ).fetchall()
        return [self._entity_from_row(r) for r in rows]

    def get_entities_by_uuids(self, uuids: Iterable[str]) -> dict[str, Entity]:
        uuids = list(uuids)
        if not uuids:
            return {}
        # 用 json_each 避免动态拼接 IN 子句
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE uuid IN (SELECT value FROM json_each(?))",
            (json.dumps(uuids),),
        ).fetchall()
        return {r["uuid"]: self._entity_from_row(r) for r in rows}

    @staticmethod
    def _entity_from_row(row: sqlite3.Row) -> Entity:
        return Entity(
            uuid=row["uuid"],
            name=row["name"],
            group_id=row["group_id"],
            labels=_parse_json(row["labels"], []),
            summary=row["summary"] or "",
            attributes=_parse_json(row["attributes"], {}),
            created_at=row["created_at"],
        )

    # ---------- facts ----------

    def upsert_fact(self, fact: Fact, embedding: list[float] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO facts (uuid, name, fact, source_uuid, target_uuid, group_id, episodes, valid_at, invalid_at, expired_at, reference_time, attributes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET
                 name=excluded.name, fact=excluded.fact,
                 episodes=excluded.episodes, valid_at=excluded.valid_at,
                 invalid_at=excluded.invalid_at, expired_at=excluded.expired_at,
                 attributes=excluded.attributes""",
            (
                fact.uuid,
                fact.name,
                fact.fact,
                fact.source_uuid,
                fact.target_uuid,
                fact.group_id,
                json.dumps(fact.episodes, ensure_ascii=False),
                fact.valid_at,
                fact.invalid_at,
                fact.expired_at,
                fact.reference_time,
                json.dumps(fact.attributes, ensure_ascii=False),
            ),
        )
        rowid = self.conn.execute(
            "SELECT rowid FROM facts WHERE uuid=?", (fact.uuid,)
        ).fetchone()["rowid"]
        self.conn.execute("DELETE FROM facts_fts WHERE rowid=?", (rowid,))
        self.conn.execute(
            "INSERT INTO facts_fts(rowid, fact, name) VALUES (?, ?, ?)",
            (rowid, fts_text(fact.fact), fts_text(fact.name)),
        )
        if embedding is not None:
            self.save_embedding(fact.uuid, "fact", embedding)
        self.conn.commit()

    def get_fact(self, uuid: str) -> Fact | None:
        row = self.conn.execute("SELECT * FROM facts WHERE uuid=?", (uuid,)).fetchone()
        return self._fact_from_row(row) if row else None

    def get_facts_between(
        self, source_uuid: str, target_uuid: str, group_id: str
    ) -> list[Fact]:
        """端点完全相同的候选事实（去重/矛盾检测用）。"""
        rows = self.conn.execute(
            """SELECT * FROM facts
               WHERE group_id=? AND source_uuid=? AND target_uuid=? AND expired_at IS NULL""",
            (group_id, source_uuid, target_uuid),
        ).fetchall()
        return [self._fact_from_row(r) for r in rows]

    def get_facts_mentioning(self, entity_uuid: str, group_id: str) -> list[Fact]:
        """与某实体相连的所有事实（图遍历核心）。"""
        rows = self.conn.execute(
            """SELECT * FROM facts
               WHERE group_id=? AND expired_at IS NULL AND (source_uuid=? OR target_uuid=?)""",
            (group_id, entity_uuid, entity_uuid),
        ).fetchall()
        return [self._fact_from_row(r) for r in rows]

    def get_all_facts(self, group_id: str) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE group_id=?", (group_id,)
        ).fetchall()
        return [self._fact_from_row(r) for r in rows]

    def delete_fact(self, uuid: str) -> None:
        r = self.conn.execute(
            "SELECT rowid FROM facts WHERE uuid=?", (uuid,)
        ).fetchone()
        if r:
            self.conn.execute("DELETE FROM facts_fts WHERE rowid=?", (r["rowid"],))
        self.conn.execute("DELETE FROM embeddings WHERE uuid=?", (uuid,))
        self.conn.execute("DELETE FROM facts WHERE uuid=?", (uuid,))
        self.conn.commit()

    # ---------- 数据管理（手动失效 / 实体合并） ----------

    def invalidate_fact(self, uuid: str) -> Fact | None:
        """手动失效一条事实（invalid_at=expired_at=now），返回更新后的事实或 None。"""
        now = now_iso()
        self.conn.execute(
            "UPDATE facts SET invalid_at=?, expired_at=? WHERE uuid=? AND expired_at IS NULL",
            (now, now, uuid),
        )
        self.conn.commit()
        return self.get_fact(uuid)

    def merge_entities(self, keep_uuid: str, remove_uuid: str) -> dict[str, Any]:
        """合并实体：remove 的一切引用（facts 端点/mentions/embeddings/FTS）重写到 keep，删 remove。

        跨组同名实体也可合并：facts/mentions 各自保留原 group_id，实体引用统一到 keep。
        """
        if keep_uuid == remove_uuid:
            raise ValueError("keep 与 remove 不能是同一个实体")
        keep = self.get_entity(keep_uuid)
        rem = self.get_entity(remove_uuid)
        if not keep or not rem:
            raise ValueError("实体不存在")

        # 合并 labels（去重保序）与 summary（取较长者）
        keep.labels = list(dict.fromkeys(keep.labels + rem.labels))
        if len(rem.summary or "") > len(keep.summary or ""):
            keep.summary = rem.summary

        facts_rewritten = 0
        for col in ("source_uuid", "target_uuid"):
            cur = self.conn.execute(
                f"UPDATE facts SET {col}=? WHERE {col}=?", (keep_uuid, remove_uuid)
            ).rowcount
            facts_rewritten += cur
        mentions_rewritten = self.conn.execute(
            "UPDATE mentions SET entity_uuid=? WHERE entity_uuid=?",
            (keep_uuid, remove_uuid),
        ).rowcount
        self.conn.execute("DELETE FROM embeddings WHERE uuid=?", (remove_uuid,))
        self.conn.execute(
            "DELETE FROM entities_fts WHERE rowid=(SELECT rowid FROM entities WHERE uuid=?)",
            (remove_uuid,),
        )
        # 链接重定向：remove 的 same_as 链接转给 keep（合并后跨组同一性延续）
        for r in self.conn.execute(
            "SELECT a_uuid, b_uuid, kind FROM entity_links WHERE a_uuid=? OR b_uuid=?",
            (remove_uuid, remove_uuid),
        ).fetchall():
            other = r["b_uuid"] if r["a_uuid"] == remove_uuid else r["a_uuid"]
            if other != keep_uuid:
                self._add_link_sync(keep_uuid, other, r["kind"])
        self.conn.execute(
            "DELETE FROM entity_links WHERE a_uuid=? OR b_uuid=?",
            (remove_uuid, remove_uuid),
        )
        self.conn.execute("DELETE FROM entities WHERE uuid=?", (remove_uuid,))
        self.upsert_entity(keep)  # 同步 keep 的 FTS
        self.conn.commit()
        return {
            "kept": keep_uuid,
            "removed": remove_uuid,
            "facts_rewritten": facts_rewritten,
            "mentions_rewritten": mentions_rewritten,
        }

    # ---------- entity_links（跨分区实体同一性 same_as） ----------

    @staticmethod
    def _link_pair(a: str, b: str) -> tuple[str, str]:
        """规范化存储顺序 a < b：任一端查询都能命中索引，避免双向重复行。"""
        return (a, b) if a < b else (b, a)

    def add_link(self, a: str, b: str, kind: str = "same_as") -> None:
        """建立实体间逻辑链接（默认 same_as 跨组同一性）。幂等。"""
        self._add_link_sync(a, b, kind)
        self.conn.commit()

    def _add_link_sync(self, a: str, b: str, kind: str = "same_as") -> None:
        if a == b:
            return
        x, y = self._link_pair(a, b)
        self.conn.execute(
            """INSERT OR IGNORE INTO entity_links (a_uuid, b_uuid, kind, created_at)
               VALUES (?, ?, ?, ?)""",
            (x, y, kind, now_iso()),
        )

    def remove_link(self, a: str, b: str) -> int:
        """拆除链接。返回删除行数（0=链接不存在）。"""
        x, y = self._link_pair(a, b)
        n = self.conn.execute(
            "DELETE FROM entity_links WHERE a_uuid=? AND b_uuid=?", (x, y)
        ).rowcount
        self.conn.commit()
        return n

    def get_linked_uuids(self, uuid: str, kind: str = "same_as") -> list[str]:
        """某实体的所有链接对端 uuid（检索跨组扩展用）。"""
        rows = self.conn.execute(
            """SELECT a_uuid, b_uuid FROM entity_links
               WHERE kind=? AND (a_uuid=? OR b_uuid=?)""",
            (kind, uuid, uuid),
        ).fetchall()
        return [r["b_uuid"] if r["a_uuid"] == uuid else r["a_uuid"] for r in rows]

    def get_all_links(self, kind: str = "same_as") -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT a_uuid, b_uuid, kind, created_at FROM entity_links WHERE kind=?",
            (kind,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_facts_mentioning_any_group(self, entity_uuid: str) -> list[Fact]:
        """与某实体相连的所有事实，不限分区（same_as 跨组检索扩展用）。"""
        rows = self.conn.execute(
            """SELECT * FROM facts
               WHERE expired_at IS NULL AND (source_uuid=? OR target_uuid=?)""",
            (entity_uuid, entity_uuid),
        ).fetchall()
        return [self._fact_from_row(r) for r in rows]

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> Fact:
        return Fact(
            uuid=row["uuid"],
            name=row["name"],
            fact=row["fact"],
            source_uuid=row["source_uuid"],
            target_uuid=row["target_uuid"],
            group_id=row["group_id"],
            episodes=_parse_json(row["episodes"], []),
            valid_at=row["valid_at"],
            invalid_at=row["invalid_at"],
            expired_at=row["expired_at"],
            reference_time=row["reference_time"],
            attributes=_parse_json(row["attributes"], {}),
        )

    # ---------- mentions ----------

    def add_mentions(self, episode_uuid: str, entity_uuids: list[str]) -> None:
        for euuid in set(entity_uuids):
            self.conn.execute(
                "INSERT OR IGNORE INTO mentions (episode_uuid, entity_uuid) VALUES (?, ?)",
                (episode_uuid, euuid),
            )
        self.conn.commit()

    def get_mentions(self, episode_uuid: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT entity_uuid FROM mentions WHERE episode_uuid=?", (episode_uuid,)
        ).fetchall()
        return [r["entity_uuid"] for r in rows]

    # ---------- embeddings ----------

    def save_embedding(self, uuid: str, kind: str, vector: list[float]) -> None:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        self.conn.execute(
            "INSERT INTO embeddings (uuid, kind, vector) VALUES (?, ?, ?) "
            "ON CONFLICT(uuid) DO UPDATE SET vector=excluded.vector",
            (uuid, kind, blob),
        )

    def load_embeddings(
        self, kind: str, group_id: str | None = None
    ) -> tuple[list[str], np.ndarray]:
        """加载指定 kind 的向量。group_id 非空时只取该分区的（检索/消歧必须按组隔离）。"""
        if group_id is None:
            rows = self.conn.execute(
                "SELECT uuid, vector FROM embeddings WHERE kind=?", (kind,)
            ).fetchall()
        elif kind == "fact":
            rows = self.conn.execute(
                "SELECT e.uuid, e.vector FROM embeddings e "
                "JOIN facts f ON f.uuid = e.uuid WHERE e.kind=? AND f.group_id=?",
                (kind, group_id),
            ).fetchall()
        elif kind == "episode":
            rows = self.conn.execute(
                "SELECT e.uuid, e.vector FROM embeddings e "
                "JOIN episodes p ON p.uuid = e.uuid WHERE e.kind=? AND p.group_id=?",
                (kind, group_id),
            ).fetchall()
        else:  # entity
            rows = self.conn.execute(
                "SELECT e.uuid, e.vector FROM embeddings e "
                "JOIN entities t ON t.uuid = e.uuid WHERE e.kind=? AND t.group_id=?",
                (kind, group_id),
            ).fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        uuids = [r["uuid"] for r in rows]
        mat = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
        return uuids, mat

    def delete_embedding(self, uuid: str) -> None:
        self.conn.execute("DELETE FROM embeddings WHERE uuid=?", (uuid,))

    # ---------- FTS5 全文检索 ----------

    def fts_search_facts(
        self, query: str, group_id: str, limit: int = 20
    ) -> list[tuple[str, float]]:
        """返回 (fact_uuid, bm25_score)。"""
        rows = self.conn.execute(
            """
            SELECT f.uuid, bm25(facts_fts) AS score
            FROM facts_fts
            JOIN facts f ON f.rowid = facts_fts.rowid
            WHERE facts_fts MATCH ? AND f.group_id = ? AND f.expired_at IS NULL
            ORDER BY score
            LIMIT ?
            """,
            (self._fts_query(query), group_id, limit),
        ).fetchall()
        return [(r["uuid"], -r["score"]) for r in rows]  # bm25 越小越好，转正

    def fts_search_entities(
        self, query: str, group_id: str, limit: int = 20
    ) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            """
            SELECT e.uuid, bm25(entities_fts) AS score
            FROM entities_fts
            JOIN entities e ON e.rowid = entities_fts.rowid
            WHERE entities_fts MATCH ? AND e.group_id = ?
            ORDER BY score
            LIMIT ?
            """,
            (self._fts_query(query), group_id, limit),
        ).fetchall()
        return [(r["uuid"], -r["score"]) for r in rows]

    def fts_search_episodes(
        self, query: str, group_id: str, limit: int = 20
    ) -> list[tuple[str, float]]:
        """episode 原文全文检索（原始溯源层召回，含未提取的 episode）。"""
        rows = self.conn.execute(
            """
            SELECT e.uuid, bm25(episodes_fts) AS score
            FROM episodes_fts
            JOIN episodes e ON e.rowid = episodes_fts.rowid
            WHERE episodes_fts MATCH ? AND e.group_id = ?
            ORDER BY score
            LIMIT ?
            """,
            (self._fts_query(query), group_id, limit),
        ).fetchall()
        return [(r["uuid"], -r["score"]) for r in rows]

    @staticmethod
    def _fts_query(raw: str) -> str:
        """自然语言查询 → FTS5：CJK 同构预切分 + 引号短语 OR（不用前缀*）。"""
        tokens = [t for t in raw.replace("?", " ").split() if t]
        if not tokens:
            return '""'
        parts = []
        for t in tokens[:8]:
            escaped = fts_text(t).replace('"', '""')
            parts.append(f'"{escaped}"')
        return " OR ".join(parts)

    # ---------- 统计 ----------

    def stats(self, group_id: str | None = None) -> dict[str, int]:
        g = group_id if group_id is not None else ""
        return {
            "episodes": self._count("episodes", g),
            "entities": self._count("entities", g),
            "facts": self._count("facts", g),
            "active_facts": self._count_active_facts(g),
        }

    def _count(self, table: str, group_id: str) -> int:
        # group_id 为空字符串表示不限分区（单条参数化查询，避免动态 SQL）
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE (? = '' OR group_id = ?)",
            (group_id, group_id),
        ).fetchone()[0]

    def _count_active_facts(self, group_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE expired_at IS NULL AND (? = '' OR group_id = ?)",
            (group_id, group_id),
        ).fetchone()[0]
