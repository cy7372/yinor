# 贡献指南

欢迎 issue / PR。本项目是单人主导演进的自研记忆系统，接受以下方向的贡献：

- 提取管线 prompt 调优（中英文皆可）
- 检索质量（分词、融合、重排）
- Web 控制台体验
- 文档与部署案例

## 开发环境

```bash
uv venv .venv && uv pip install -r requirements.txt
cp .env.example .env   # 填入你的 LLM key
```

代码风格用 ruff（配置在 pyproject.toml）：`ruff check yinor`。

## 提交与测试

- 提交前跑 `ruff check yinor` 与 `python test_fts_migration.py`（不依赖 LLM）
- `test_yinor.py` / `test_real.py` / `test_time_aware.py` 需要真实 LLM 端点，本地跑通再提
- 提交信息用简洁中文或英文，一行说清改了什么

## 注意

- **不要提交 `.env`、数据库文件、个人记忆导出**（.gitignore 已拦截，留意绕过路径）
- schema 变更需保证存量库幂等迁移（参考 storage.py `_migrate_*` 的做法）
