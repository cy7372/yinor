"""LLM 提取/消歧 prompts（针对 json_object 模式优化）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .entity_types import entity_type_prompt

# ---------- 提取输出模型 ----------


class ExtractedEntityItem(BaseModel):
    name: str = Field(description="提取出的实体名")
    entity_type: str = Field(description="实体类型，必须是提供的类型名之一")
    description: str = Field(
        default="", description="一句话描述该实体在这个上下文中是什么"
    )


class EntitiesExtraction(BaseModel):
    extracted_entities: list[ExtractedEntityItem] = Field(
        description="提取出的实体列表"
    )


class ExtractedFactItem(BaseModel):
    source_name: str = Field(description="主语实体名（必须在提供的实体列表中）")
    source_type: str = Field(description="主语实体类型")
    target_name: str = Field(description="宾语实体名（必须在提供的实体列表中）")
    target_type: str = Field(description="宾语实体类型")
    name: str = Field(
        description="关系名，用简短的动词或介词短语，如 'works_at'、'loves'、'previous_role'"
    )
    fact: str = Field(
        description="完整事实陈述句，如 'Kamala Harris works at California as Attorney General'"
    )
    valid_at: str | None = Field(
        default=None,
        description="事实明确开始时间（ISO8601，如 2020-01-01 或 2020-01-01T12:00:00+00:00）；文本未给出明确时间则不填",
    )
    invalid_at: str | None = Field(
        default=None,
        description="事实明确结束时间（ISO8601）；文本未给出明确结束时间则不填",
    )


class FactsExtraction(BaseModel):
    extracted_facts: list[ExtractedFactItem] = Field(description="提取出的事实列表")


class EntityDuplicate(BaseModel):
    is_duplicate: bool = Field(description="新实体是否与某个候选实体是同一个实体")
    duplicate_of: int | None = Field(
        default=None, description="候选实体索引（is_duplicate=true 时必填）"
    )
    reason: str = Field(default="", description="判断理由")


class EdgeDuplicate(BaseModel):
    duplicate_facts: list[int] = Field(
        default_factory=list, description="新事实与哪些 EXISTING FACTS 重复（索引）"
    )
    contradicted_facts: list[int] = Field(
        default_factory=list,
        description="新事实与哪些 EXISTING/INVALIDATION FACTS 矛盾（索引）",
    )


class EntitySummaries(BaseModel):
    summaries: list[dict[str, str]] = Field(
        description="[{name, summary}] 只包含需要更新 summary 的实体"
    )


# ---------- prompts ----------

EXTRACT_ENTITIES_SYSTEM = f"""你是知识图谱的实体提取器。从对话/文本中提取值得长期记忆的实体。

实体类型定义：
{entity_type_prompt()}

规则：
- 只提取有长期记忆价值的实体，忽略无关细节
- 同一实体只提取一次；提取的实体名用规范形式（如全名而非昵称）
- 对每个实体给出一句话描述
- 重要：偏好、决策、流程这类信息不要提取成实体（如"弃用 X 改用 Y"、"喜欢 Z"），它们应表达为事实关系；实体是具体的人、物、项目、地点、组织、概念
- 中英文实体名用最常用形式（如项目叫 yinor 就写 yinor，而不是"记忆系统"或"精简版 yinor"）
"""

EXTRACT_ENTITIES_USER = """提取以下内容中的实体：

{episode}

上下文（更早的内容，用于消歧和完整理解）：
{context}

输出 JSON：{{"extracted_entities": [{{"name": "...", "entity_type": "...", "description": "..."}}]}}
"""

EXTRACT_FACTS_SYSTEM = """你是知识图谱的关系提取器。给定实体列表和文本，提取实体之间的稳定事实关系（三元组）。

规则：
- 提取文本中明确陈述的事实，包括有明确时间范围的事实（任期、职位、经历）——把时间信息写进 fact 文本
- source/target 必须在提供的实体列表中
- 关系名用英文短动词/介词短语（如 works_at, lives_in, owns, member_of, previous_role）
- fact 字段写完整的自然语言事实陈述
- 不提取：纯推测、假设、将来时承诺、与实体无关的琐碎细节
- 同一事实只提一次；从上下文已知道的事实（如职位变更）也应提取
- 至少提取 1 条事实（只要文本涉及任何两个实体），不要返回空列表
- 时间字段：文本给出明确时间（如'2018年3月'、'2020-01-01'）时，**必须同时**把开始时间填入 valid_at、结束时间填入 invalid_at（仅当有明确结束时），一律用 ISO8601；**不能只写进 fact 文本而不填字段**；没有明确时间则不填
- 相对时间（昨天/本周/下个月/明年等）按用户消息中的“当前时间”解析成绝对 ISO8601 日期
"""

EXTRACT_FACTS_USER = """文本：
{episode}

当前时间（用于解析相对日期）：{now}

已提取实体（name: type）：
{entities}

输出 JSON：{{"extracted_facts": [{{"source_name": "...", "source_type": "...", "target_name": "...", "target_type": "...", "name": "...", "fact": "...", "valid_at": "...或null", "invalid_at": "...或null"}}]}}
"""

DEDUPE_ENTITY_SYSTEM = """你是实体消歧器。判断"新实体"是否与"候选实体"中的某个是同一个实体。

规则：
- 同一个人/物/概念的不同表述（全名/昵称/缩写/别名/中英文名/带修饰词）算同一个：
  如 "yinor" 与 "记忆系统" 与 "精简版 yinor"、"Yinor" 与 "yinor"、"OpenAI" 与 "openai"
- 名称相近但指代不同的事物不算同一个
- 只要语义上明显指同一实体，就判为重复（激进合并，避免碎片化）
"""

DEDUPE_ENTITY_USER = """新实体：{name}（类型 {entity_type}）{description}

候选实体：
{indexed_candidates}

输出 JSON：{{"is_duplicate": true/false, "duplicate_of": 候选索引或null, "reason": "..."}}
"""

DEDUPE_EDGE_SYSTEM = """你是事实去重器。给定一条新事实和一批已存在事实，判断哪些是重复、哪些是矛盾。

规则：
- duplicate：同一对实体之间的相同/等价事实（换了个说法但意思一样）
- contradicted：与新事实冲突的事实（两者不可能同时为真），新事实取代旧事实
- 索引从 0 开始
"""

DEDUPE_EDGE_USER = """新事实：{new_fact}

EXISTING FACTS（可能重复的对象）：
{indexed_existing}

FACT INVALIDATION CANDIDATES（可能被新事实取代的旧事实）：
{indexed_invalidation}

输出 JSON：{{"duplicate_facts": [索引...], "contradicted_facts": [索引...]}}
"""

SUMMARIZE_ENTITY_SYSTEM = """你是实体摘要器。根据对话内容更新实体的长期摘要。

规则：
- 摘要要精炼、包含关键事实（职位、关系、状态等）
- 保留之前摘要中仍然有效的信息，合并新信息
- 摘要控制在 100-200 字
"""

SUMMARIZE_ENTITY_USER = """实体：{name}
当前摘要：{current_summary}

本次新增信息：
{episode}

输出 JSON：{{"summaries": [{{"name": "...", "summary": "..."}}]}}
"""
