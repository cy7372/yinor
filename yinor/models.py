"""yinor 数据模型（参考 Graphiti 设计精简）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utc_now().isoformat()


def new_uuid() -> str:
    return str(uuid4())


class Episode(BaseModel):
    """原始数据单元（ground truth）。"""

    uuid: str = Field(default_factory=new_uuid)
    name: str
    group_id: str
    content: str
    source: str = "text"  # text | json | message
    source_description: str = ""
    created_at: str = Field(default_factory=now_iso)
    valid_at: str | None = None  # 事件发生时间


class Entity(BaseModel):
    """实体节点。"""

    uuid: str = Field(default_factory=new_uuid)
    name: str
    group_id: str
    labels: list[str] = Field(default_factory=list)  # 实体类型
    summary: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)

    def to_row(self) -> tuple:
        return (
            self.uuid,
            self.name,
            self.group_id,
            json.dumps(self.labels, ensure_ascii=False),
            self.summary,
            json.dumps(self.attributes, ensure_ascii=False),
            self.created_at,
        )


class Fact(BaseModel):
    """事实（关系三元组 + 时序有效性窗口）。"""

    uuid: str = Field(default_factory=new_uuid)
    name: str  # 关系名/谓词
    fact: str  # 事实文本
    source_uuid: str
    target_uuid: str
    group_id: str
    episodes: list[str] = Field(default_factory=list)  # 溯源
    valid_at: str | None = None
    invalid_at: str | None = None
    expired_at: str | None = None
    reference_time: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    def to_row(self) -> tuple:
        return (
            self.uuid,
            self.name,
            self.fact,
            self.source_uuid,
            self.target_uuid,
            self.group_id,
            json.dumps(self.episodes, ensure_ascii=False),
            self.valid_at,
            self.invalid_at,
            self.expired_at,
            self.reference_time,
            json.dumps(self.attributes, ensure_ascii=False),
        )


class Mention(BaseModel):
    episode_uuid: str
    entity_uuid: str


# ---------- LLM 提取输出模型 ----------


class ExtractedEntity(BaseModel):
    """LLM 提取出的候选实体。"""

    name: str
    entity_type: str  # 分类后的类型名（与 ENTITY_TYPES 的 key 对应）
    description: str = ""  # 一句话描述，用于生成 summary 和消歧


class ExtractedFact(BaseModel):
    """LLM 提取出的候选事实（引用实体名，未消歧前）。"""

    source_name: str
    source_type: str
    target_name: str
    target_type: str
    name: str  # 关系名
    fact: str  # 事实文本


class SearchResult(BaseModel):
    """搜索返回的一条事实。"""

    uuid: str
    fact: str
    name: str
    source_uuid: str
    target_uuid: str
    source_name: str
    target_name: str
    valid_at: str | None
    invalid_at: str | None
    score: float = 0.0
    episodes: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """完整搜索响应：事实 + 实体 + 邻域扩展。"""

    facts: list[SearchResult]
    entities: list[dict[str, Any]] = Field(default_factory=list)
    episodes: list[dict[str, Any]] = Field(
        default_factory=list
    )  # 原文层召回（含未提取 episode）
    query: str
    elapsed_ms: float = 0.0
