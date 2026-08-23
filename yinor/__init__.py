"""yinor — 记忆系统（时序上下文图）。

核心能力：
- Episode 溯源：每条事实可追溯到原始数据
- 时序有效性窗口：事实演变可查询（支持 as_of 历史查询）
- 实体消歧 + 事实消歧（LLM 判定重复/矛盾）
- 混合检索：FTS5 + 向量 → RRF 融合 + 图遍历扩展
- group_id 分区
"""

from .llm import LLMClient
from .memory import DEFAULT_DB_PATH, Memory, fmt_search
from .models import Episode, Fact, SearchResponse
from .storage import Storage

__version__ = "0.1.0"

__all__ = [
    "Memory",
    "LLMClient",
    "Storage",
    "Episode",
    "Fact",
    "SearchResponse",
    "fmt_search",
    "DEFAULT_DB_PATH",
]
