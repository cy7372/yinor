# 印·Yinor 部署指南（安装到其他电脑）

> 适用：把 yinor 装到一台新机器上跑起来（开发机自用请看 README.md 的快速开始）。
> 全程约 10 分钟；需要一台可访问的 OpenAI 兼容 LLM 端点。

## 0. 你需要准备什么

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | ≥ 3.11（3.13 实测通过） | 自带 SQLite/FTS5，无需装数据库 |
| 包管理 | uv（推荐）或 pip | uv 装依赖快得多 |
| LLM 端点 | OpenAI 兼容 `/v1` 接口 | 需要 chat（支持 json_object 更佳）+ embeddings 两个能力 |
| 磁盘 | ~200MB（代码+venv），记忆库另算 | data/yinor.db 单文件 |

没有本机 LLM 网关也可以：任何兼容端点都行（DeepSeek、硅基流动、自建 vLLM 等），
在 `.env` 里改 `YINOR_LLM_BASE_URL` / `YINOR_LLM_MODEL` / `YINOR_EMBED_MODEL` 即可。
**注意**：embedding 模型维度需与 `YINOR_EMBED_DIM` 一致（默认 1024），且换模型后
旧向量与新向量不互通——新机器全新建库无影响，迁移旧库请沿用同一 embedding 模型。

## 1. 获取代码

### 方式 A：Windows 安装包（推荐，免 Python）

从 [Releases](https://github.com/cy7372/yinor/releases) 下载 `yinor-Setup-v<版本>.exe`，双击安装（免管理员权限，装到 `%APPDATA%\yinor`）。安装后：

1. 进入 `%APPDATA%\yinor`，把 `.env.example` 复制为 `.env`，填入你的 LLM key（直连云端的完整配置见第 3 节）
2. 双击桌面/开始菜单的 yinor 图标启动（控制台窗口即日志窗，关窗即停）
3. 浏览器打开 <http://127.0.0.1:20102/>

**更新**：下载新版 Setup 覆盖安装即可，`.env` 与 `data/`（记忆库）自动保留，不会丢数据。卸载同样保留数据。

### 方式 B：源码运行（所有平台）

git clone 或从 GitHub Release 下载源码包，解压即得最小文件集：

```text
yinor/                     # Python 包（含 schema.sql、static/ 控制台资源）
run_server.py              # 服务入口
requirements.txt
.env.example                # 复制为 .env 后填写
servy_yinor.example.json   # Windows 服务注册示例（路径需替换）
INSTALL.md / README.md
```

若从源机器手动拷贝：**不要带** `.venv/`、`logs/`、`data/`（除非迁移记忆，见第 5 节）、`__pycache__/`。

## 2. 建环境、装依赖

```powershell
# Windows（uv）
uv venv .venv
uv pip install -r requirements.txt

# 或 pip
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

```bash
# Linux / macOS
uv venv .venv && uv pip install -r requirements.txt
```

## 3. 配置 .env

```powershell
copy .env.example .env
# 编辑 .env：至少填 LLM_API_KEY
```

直连云端的典型配置（默认指向本机网关，无网关时必须显式配置）：

```ini
YINOR_LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的key
YINOR_LLM_MODEL=deepseek-chat
YINOR_EMBED_MODEL=...      # 该服务商的 embedding 模型
YINOR_EMBED_DIM=...        # 对应维度
```

## 4. 启动与验证

```powershell
# 前台跑（调试用）
.venv\Scripts\python.exe run_server.py

# 验证
curl http://127.0.0.1:20102/health        # {"status":"ok",...}
curl http://127.0.0.1:20102/stats
# 写一条记忆试试（同步等待提取，>180s 超时；group 换成新机器的项目名）
curl -X POST http://127.0.0.1:20102/episodes -H "Content-Type: application/json" ^
  -d "{\"content\":\"新机器部署验证\",\"source\":\"probe\",\"extract\":true,\"wait\":true}"
```

浏览器打开 `http://127.0.0.1:20102/` 是 Web 控制台（图谱/实体/事实/质检）。

### 常驻方式（二选一）

**Windows + Servy**（源机器同款方案）：

```powershell
# 复制 servy_yinor.example.json 为 servy_yinor.json，
# 把里面的 C:\path\to\yinor 替换为本机实际路径后：
sudo servy-cli import --file servy_yinor.json
sudo servy-cli start --name=yinor
sudo servy-cli status --name=yinor
```

要改的字段：`ExecutablePath`、`StartupDirectory`、`StdoutPath`、`StderrPath`
（注意先建 `logs/` 目录）。

**Linux + systemd**：

```ini
# /etc/systemd/system/yinor.service
[Unit]
Description=yinor Memory System
After=network.target

[Service]
WorkingDirectory=/opt/yinor
ExecStart=/opt/yinor/.venv/bin/python run_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now yinor
```

## 5. 迁移旧记忆（可选）

记忆库就是单个文件 `data/yinor.db`（SQLite + WAL）。

```powershell
# 源机器：先停服务（或至少 checkpoint），避免 -wal 里有未落盘数据
sudo servy-cli stop --name=yinor     # Windows
# Linux 亦可：sqlite3 data/yinor.db "PRAGMA wal_checkpoint(TRUNCATE);"

# 复制到新机器的 <项目根>/data/yinor.db（只拷 .db 主文件即可）
```

新机器首次启动会自动做 schema 迁移（FTS 重建等，幂等）。
**沿用同一 embedding 模型**，否则旧向量与新向量相似度失真。

## 6. Agent 集成（可选）

yinor 是纯 REST 服务，任意 agent 框架 / HTTP 客户端均可对接，核心接口：

```text
POST /episodes     写入记忆（JSON: content, group_id, extract, wait）
GET  /search?q=    混合检索（facts/entities/episodes 三段）
GET  /history?entity=  实体演变史
GET  /stats        统计
```

与 agent 会话循环集成时，常见做法是：用户消息到达 → 先 /search 检索相关记忆
注入上下文 → 会话结束把值得记住的内容 POST /episodes（extract=true 后台提取）。

## 7. 常见问题

| 症状 | 原因/解法 |
| --- | --- |
| 启动即 `缺少 LLM_API_KEY` | .env 没建或没填；服务跑在 Session 0 时不继承用户环境，必须靠项目根 .env |
| 提取一直失败/空响应 | LLM 端点不支持 json_object 或模型太弱；换模型，提取管线对结构化输出要求高 |
| embedding 400 | 批量上限约 10 条/次（已在代码内分块）；单条失败多为维度不匹配，核对 YINOR_EMBED_DIM |
| `database is locked` | 多进程各开连接抢写锁；确认只有一个服务实例在跑，CLI 调试时先停服务 |
| 端口被占 | `YINOR_PORT` 换端口，pi 扩展侧同步改 YINOR_URL |
