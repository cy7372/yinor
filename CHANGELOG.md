# Changelog

本项目的所有显著变更都记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] - 2026-08-23

首个公开版本。

### 新增

- **时序知识图谱核心**：Episode 摄入 → LLM 提取实体/事实（带有效时间窗口）→ 事实随演变自动失效，支持 `as_of` 历史查询与实体演变史
- **三层实体消歧 + 层0 确定性预匹配**：完全匹配 / 向量召回 / LLM 判定，写入前同组全量规则预匹配拦截重复实体
- **混合检索**：中文单字分词 FTS5 + 向量 RRF 融合 + 图遍历扩展 + recency 衰减（半衰期 60 天）
- **数据治理**：保守可解释的实体自动合并（dedup）、跨分区 same_as 实体链接、Web 质检视图
- **反思机制**：窗口洞察提炼（方法层从知识层生长），产出走完整提取管线写回
- **Web 控制台**：知识图谱可视化（暗色图谱、悬停聚焦、抽屉详情）、事实/实体/原文浏览与搜索、分区管理
- **服务与集成**：FastAPI 服务（`run_server.py`）、CLI（`python -m yinor.cli`）、pi 扩展（五工具 + 会话自动上下文注入）
- **部署**：requirements.txt 最小依赖、`.env.example` 配置模板（任意 OpenAI 兼容端点）、INSTALL.md 部署指南、Servy/systemd 常驻示例

[Unreleased]: https://github.com/cy7372/yinor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cy7372/yinor/releases/tag/v0.1.0
