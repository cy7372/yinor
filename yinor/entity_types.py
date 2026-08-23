"""默认实体类型（ontology）：一组适用于个人记忆的默认集。

每个类型带一句提取指引（会注入提取 prompt）。
"""

from __future__ import annotations

DEFAULT_ENTITY_TYPES: dict[str, str] = {
    "Person": "人名、人物角色。",
    "Project": "项目、任务、正在进行的工作。",
    "Tool": "软件、工具、库、命令、服务、设备。",
    "Location": "物理或虚拟地点。",
    "Organization": "公司、团队、机构、开源社区。",
    "Event": "事件、会议、时间点发生的事情。",
    "Concept": "概念、术语、领域知识、抽象想法。",
    "Preference": "用户的偏好、选择、意见、喜好（如 '我喜欢 X'、'X 更好'、'不要 Y'）。",
    "Procedure": "操作流程、步骤、约定、规则（如 '先做 X 再做 Y'、'遇到 Z 时做 W'）。",
    "Document": "文档、文件、网页、资料。",
}


def entity_type_prompt() -> str:
    """把实体类型定义格式化为 prompt 片段。"""
    lines = []
    for i, (name, desc) in enumerate(DEFAULT_ENTITY_TYPES.items()):
        lines.append(f"{i}. {name}: {desc}")
    return "\n".join(lines)
