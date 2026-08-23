# Graphiti 架构研究笔记（自研记忆系统蓝图）

> 研究时间：2026-08-09。来源：getzep/graphiti 0.29.3 源码 + 本机跑通验证。
> 目标：提取可复用的设计，指导自研精简版记忆系统。

## 1. 核心数据模型（时序上下文图）

```
EpisodicNode  (原始数据单元，ground truth 流)
  uuid / name / group_id / created_at / source(text|json|message) /
  source_description / content / valid_at / entity_edges[]

EntityNode    (实体)
  uuid / name / labels(实体类型) / created_at /
  name_embedding(FLOAT[]) / summary(随时间演进) / attributes(结构化属性)

EntityEdge    (事实 = 关系, 图里叫 RELATES_TO, Kuzu 里是独立节点表 RelatesToNode_)
  uuid / name(关系名) / fact(事实文本) / fact_embedding /
  episodes[](溯源：哪些 episode 提到了这条事实) /
  valid_at / invalid_at / expired_at(时序有效性窗口) / reference_time / attributes

EpisodicEdge  (MENTIONS：Episode → Entity 溯源链接)
CommunityNode (实体聚类，大图才需要)
SagaNode      (命名 episode 流：HAS_EPISODE / NEXT_EPISODE)
group_id      (图分区，多租户/多项目隔离)
```

**Kuzu 特化**：实体边用 `RelatesToNode_` 节点表 + 双向 `RELATES_TO` 关系表示（Kuzu 关系表存属性弱）。

## 2. 摄入 Pipeline（add_episode，串行、异步、约 40s/episode）

```
1. retrieve_episodes   取前 N 个 episode 做上下文（window=10）
2. extract_nodes       LLM 提取候选实体（带 entity_type_id 分类）
3. resolve_nodes       实体消歧：
                         a) 语义检索候选（name embedding 相似）
                         b) 确定性相似度匹配（阈值）
                         c) 剩余 LLM 判定 duplicate
4. extract_edges       LLM 提取实体间关系（事实）
5. resolve_edges       事实去重 + 矛盾检测（时序核心，见 §3）
6. extract_attributes  LLM 按实体类型抽结构化属性 + 更新实体 summary
7. _process_episode_data 落库：episode、MENTIONS、节点、边（含失效）
8. (可选) update_communities
```

LLM 调用成本：每 episode ~5-10 次，大部分走 small model。

## 3. 时序核心（resolve_extracted_edge）

```
新事实 vs 已存在事实：
- 完全匹配（归一化文本相同）→ 复用旧边，追加 episode 溯源
- LLM 判定 EdgeDuplicate{duplicate_facts[], contradicted_facts[]}
  - duplicate → 复用旧边（保留旧时间戳）
  - contradicted → 失效候选
- 失效规则：
  - 若失效候选 valid_at > 新事实 valid_at → 新事实被"过期"（说明已有更新的信息）
  - 否则旧事实 invalid_at = 新事实 valid_at（被新事实取代）
  - expired_at = now 标记软删除
```

**关键设计**：事实永不物理删除，只标注有效性窗口 → 支持历史查询（"当时是什么情况"）。

## 4. 搜索 Pipeline（四层并行混合检索）

```
search(query) → 4 scope 并发:
  edge_search / node_search / episode_search / community_search
  每层: BM25(FTS) + cosine(向量) → RRF 融合 → 重排
    重排可选: RRF / MMR / node_distance(图距离) / cross_encoder
增强:
  center_node_uuid: 以某节点为中心按图距离重排（相关性强）
  bfs_origin_node_uuids: 从起点 BFS 扩展
默认 recipe: EDGE_HYBRID_SEARCH_RRF = [bm25, cosine] + RRF
```

## 5. Ontology 机制（学习重点）

- **实体类型 = Pydantic 模型**，docstring 即提取指令（如 Preference/Requirement/Procedure）
- 类型名 → 节点 label；字段 → 结构化属性（LLM 提取）
- `entity_types: dict[str, type[BaseModel]]` 传给 add_episode
- `excluded_entity_types` 排除不想要的类型
- 自定义边类型同理（edge_types）

## 6. 关键设计决策（自研要保留的）

| 设计 | 价值 |
| --- | --- |
| Episode 溯源 (episodes[] + MENTIONS) | 每条事实可追溯原始数据，可审计 |
| 时序有效性窗口 (valid_at/invalid_at) | 事实演变可查询，支持"当时"问题 |
| 增量更新（矛盾检测代替重算） | 无需全图重算 |
| LLM 消歧（实体/事实去重） | 语义级去重，非字符串级 |
| 实体 summary 演进 | 节点携带自聚合摘要 |
| 混合检索（FTS + 向量 + 图距离） | 召回率高 + 上下文相关 |
| group_id 分区 | 多项目/多用户隔离 |
| 结构化属性（ontology） | 事实之外的结构化知识 |

## 7. 已知问题 / 简化机会（自研时处理）

1. **Kuzu driver bug**：setup_schema 漏建 FTS 索引（已修，见 run_graphiti.py）
2. **falkordblite 不支持 Windows**（redislite 仅 Linux/macOS）
3. 摄入慢（~40s/episode，deepseek-v4-flash 是推理模型）
4. **json_object 模式**（cyRouter 无 json_schema）：schema 注入 prompt，靠 4 次重试兜底
5. 依赖重：图数据库 + FTS + 向量索引，自研可考虑 SQLite(FTS5) + 自管向量
6. communities/sagas 对单用户记忆系统可能冗余

## 8. 本机跑通配置（已验证）

```
LLM:       cyRouter 127.0.0.1:20100/v1, model=deepseek-v4-flash
Embedding: cyRouter, model=text-embedding-v3, dim=1024 (匹配 EMBEDDING_DIM 默认)
Structured: OpenAIGenericClient(structured_output_mode='json_object')
DB:        KuzuDriver(db=文件路径) + FTS 索引补丁
脚本:      yinor/run_graphiti.py
