# Changelog

本项目的所有显著变更都记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.2.0] - 2026-08-23

### 新增

- **时间感知检索**：查询中的时间表达（"八月中旬"/"8月14日"/"上个月"/"最近"/ISO 日期）自动解析成时间窗口，窗口内记忆加权（默认开启，零额外成本）
- **LLM 二段重排**：RRF 候选池经 LLM 相关度精排（0-10 评分），解决"话题相近但答非所问"的噪音；默认关闭，`/search?rerank=true` / `Searcher(rerank=True)` / `YINOR_RERANK=1` 按需开启（单次 +5~12s 延迟）
- **检索评测体系**（`eval/`）：黄金评测集 + 回放脚本，输出 HitRate@20 / MRR / 延迟，支持 `--rerank` 对比；实测 MRR 0.482 → 0.669（+39%）

### 变更

- LLM API key 环境变量统一为 `LLM_API_KEY`（原 `CYROUTER_API_KEY`）
- Web 控制台搜索默认开启重排（人工搜索延迟可容忍，质量优先）
- README/INSTALL 精简为独立项目表述，去除外部项目引用

### 修复

- 控制台白屏：console.html 语法错误（多余括号）+ 大图（>600 节点）防卡死裁剪渲染
- vis-network 的 IE 兼容残留 CSS 与 sourcemap 引用（控制台警告/404 清零）

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

[Unreleased]: https://github.com/cy7372/yinor/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cy7372/yinor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cy7372/yinor/releases/tag/v0.1.0
