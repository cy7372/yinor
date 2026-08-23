# 印·Yinor — 自研记忆系统

> 正式命名：**印·Yinor**（印记、印证之意；2026-08-10 定名）

基于 [Graphiti](https://github.com/getzep/graphiti)（Zep 开源时序知识图谱）的架构思想，
自研的轻量记忆系统，逐步替换 MindMemOS。

## 目录结构

```
yinor/
├── docs/GRAPHITI_ARCHITECTURE.md  # 架构研究笔记（自研蓝图）
├── yinor/                 # 自研记忆系统（Python 包）
│   ├── models.py           # 数据模型（Episode/Entity/Fact/搜索）
│   ├── storage.py          # SQLite+FTS5 存储层（含向量存取、邻域查询）
│   ├── llm.py              # OpenAI 兼容 LLM 客户端（chat json_object + embedding，端点/模型 .env 可配）
│   ├── pipeline.py         # 摄入：实体提取→消歧→事实提取→时序失效→summary
│   ├── search.py           # 混合检索（FTS+向量→RRF融合→图遍历）
│   ├── dedup.py            # 实体自动合并/跨分区链接（保守可解释规则）
│   ├── reflect.py          # 反思机制：窗口洞察提炼
│   ├── memory.py           # Memory 门面
│   ├── server.py           # FastAPI 本地服务（pi 扩展对接 + Web 控制台）
│   ├── cli.py              # CLI
│   └── schema.sql          # 建表 + FTS5 索引
├── run_server.py           # 服务入口
├── requirements.txt        # 运行时依赖
├── .env.example            # 配置模板（复制为 .env）
├── servy_yinor.example.json # Windows 服务注册示例（Servy）
├── INSTALL.md              # 部署指南（装到其他机器）
└── test_*.py               # 端到端/场景测试
```

## 快速开始

> **安装到其他电脑**：见 [INSTALL.md](INSTALL.md)（依赖清单、Servy/systemd 常驻、记忆库迁移、常见问题）。本机开发自用继续往下看。

```bash
# 0. 装依赖（Python ≥ 3.11，自带 SQLite/FTS5 无需另装）
uv venv .venv && uv pip install -r requirements.txt

# 1. 配置：复制模板，填入 LLM key（端点/模型均可在 .env 覆盖，见 .env.example）
cp .env.example .env

# 2. 启动服务
.venv/Scripts/python.exe run_server.py   # Windows
# .venv/bin/python run_server.py         # Linux/macOS

# 3. CLI 直接使用
.venv/Scripts/python.exe -m yinor.cli add "用户偏好用 Python 写工具"
.venv/Scripts/python.exe -m yinor.cli search "用户喜欢什么语言"
.venv/Scripts/python.exe -m yinor.cli history "yinor"
.venv/Scripts/python.exe -m yinor.cli stats

# 4. pi 集成：全局扩展 ~/.pi/agent/extensions/yinor.ts（重启 pi 生效）
#    提供 yinor_add / yinor_search / yinor_episode / yinor_history / yinor_stats
#    五个工具 + 会话自动上下文注入（before_agent_start）
# 5. Web 控制台（图谱可视化）：http://127.0.0.1:20102/
```

## 关键技术决策

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 存储 | SQLite + FTS5 | 零依赖、文件即库、FTS5 强、向量 numpy 自管（单用户量级够用） |
| LLM | cyRouter (127.0.0.1:20100) | 本地路由，deepseek-v4-flash 提取（temp=0.1 稳定） |
| Embedding | text-embedding-v3 (1024维) | cyRouter 自带，维度匹配 |
| 结构化输出 | json_object 模式 | cyRouter 不支持 json_schema；不注入 schema（实测干扰模型） |
| 时序 | valid_at/invalid_at/expired_at | 事实不删除只失效，支持 as_of 历史查询 |

## 已验证能力

- [x] Episode 摄入 + LLM 实体/事实提取（中文英文均可）
- [x] 实体消歧（名称/向量/LLM 三层，中英文别名合并）
- [x] 事实消歧 + 时序失效（带日期范围的新事实自动取代旧事实）
- [x] 混合检索（FTS5 + 向量 RRF 融合 + 图遍历扩展）
- [x] as_of 历史时刻查询、实体演变史
- [x] 实体 summary 随对话演进
- [x] HTTP 服务 + pi 扩展工具

## 已知限制 / 待改进

1. 提取质量依赖模型，prompt 需持续调优（过度提取/漏提取）
2. 向量检索是 brute-force（SQLite 全量加载），实体>10万时需换方案
3. 单用户定位：SQLite 单连接串行写，不考虑多进程并发写
4. facts 层对无词面重叠的查询仍有噪音（提取质量 + embedding 聚类，持续治理中）
