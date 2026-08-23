-- yinor SQLite schema
-- yinor：时序上下文图（SQLite+FTS5 实现）

CREATE TABLE IF NOT EXISTS episodes (
    uuid               TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    group_id           TEXT NOT NULL,
    source             TEXT NOT NULL DEFAULT 'text',   -- text|json|message
    source_description TEXT DEFAULT '',
    content            TEXT NOT NULL,
    created_at         TEXT NOT NULL,                  -- ISO8601 UTC (写入时间)
    valid_at           TEXT                            -- ISO8601 UTC (事件发生时间)
);

CREATE TABLE IF NOT EXISTS entities (
    uuid            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    labels          TEXT NOT NULL DEFAULT '[]',        -- JSON array 实体类型
    summary         TEXT DEFAULT '',
    attributes      TEXT DEFAULT '{}',                 -- JSON 结构化属性
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    uuid            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,                     -- 关系名 (谓词)
    fact            TEXT NOT NULL,                     -- 事实文本
    source_uuid     TEXT NOT NULL REFERENCES entities(uuid),
    target_uuid     TEXT NOT NULL REFERENCES entities(uuid),
    group_id        TEXT NOT NULL,
    episodes        TEXT NOT NULL DEFAULT '[]',        -- JSON array 溯源 episode uuids
    valid_at        TEXT,                              -- 事实开始有效时间
    invalid_at      TEXT,                              -- 事实失效时间 (NULL=当前有效)
    expired_at      TEXT,                              -- 软删除标记时间 (NULL=未删)
    reference_time  TEXT,                              -- 产生该事实的 episode 时间
    attributes      TEXT DEFAULT '{}'
);

-- 溯源：episode → entity 提及
CREATE TABLE IF NOT EXISTS mentions (
    episode_uuid TEXT NOT NULL REFERENCES episodes(uuid),
    entity_uuid  TEXT NOT NULL REFERENCES entities(uuid)
);
CREATE INDEX IF NOT EXISTS idx_mentions_episode ON mentions(episode_uuid);
CREATE INDEX IF NOT EXISTS idx_mentions_entity  ON mentions(entity_uuid);

-- 跨分区实体同一性（2026-08-13）：逻辑互认，不合并实体、保留分区边界。
-- 规范化存储：a_uuid < b_uuid（避免双向重复）；kind 预留扩展（same_as/part_of/...）。
CREATE TABLE IF NOT EXISTS entity_links (
    a_uuid     TEXT NOT NULL REFERENCES entities(uuid),
    b_uuid     TEXT NOT NULL REFERENCES entities(uuid),
    kind       TEXT NOT NULL DEFAULT 'same_as',
    created_at TEXT NOT NULL,
    PRIMARY KEY (a_uuid, b_uuid)
);
CREATE INDEX IF NOT EXISTS idx_entity_links_a ON entity_links(a_uuid);
CREATE INDEX IF NOT EXISTS idx_entity_links_b ON entity_links(b_uuid);

-- 邻域/图遍历索引
CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_uuid);
CREATE INDEX IF NOT EXISTS idx_facts_target ON facts(target_uuid);
CREATE INDEX IF NOT EXISTS idx_facts_group  ON facts(group_id);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(valid_at);

-- 向量存储（实体名/事实文本的 embedding，float32 numpy bytes）
CREATE TABLE IF NOT EXISTS embeddings (
    uuid   TEXT PRIMARY KEY,
    kind   TEXT NOT NULL,              -- 'entity' | 'fact'
    vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embeddings_kind ON embeddings(kind);

-- FTS5 全文索引（standalone 表 + Python 侧同步，2026-08-10 重构）
-- 为什么不用 content= 外部内容表 + 触发器：unicode61 分词把整段中文当单 token（中文召回全废），
-- 需要在入库前对 CJK 字符间插空格做预切分，SQL 触发器做不到 → Python 层同步。
-- fts 行 rowid 与内容表 rowid 一致（Python 维护），查询 JOIN 方式不变。
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(fact, name);
CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(name, summary);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(content, name);
