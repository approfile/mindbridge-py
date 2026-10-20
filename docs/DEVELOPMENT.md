# 开发说明

本文覆盖 MindBridge 的开发时间线、目录结构、环境准备、本地运行方式与常见问题；前置阅读：[README](../README.md) 的快速开始一节。可复现命令的完整清单见 [BENCHMARKS](./BENCHMARKS.md)。

---

## 1. 开发时间线

- **2026-06 至 2026-09**：本地开发跨度约三个月。6 月初搭建 FastAPI 骨架与配置，6 月中旬至 7 月底完成主体功能（多 Agent Runtime、三级风险判定、RAG、记忆、Skill、工具队列、前端），9 月补充离线验证脚本、回归测试与文档；
- **2026-09-19**：集中整理并发布首个公开快照。公开仓库的 4 个初始 commit 落在约 41 分钟的时间窗口内，这是整理上传的节奏，不代表开发周期；时间线只陈述可验证事实，不伪造更早的提交日期，后续开发以增量提交记录演进。

---

## 2. 项目结构

```text
app/      # 后端：agents / api / core / harness / knowledge / mcp_tools / models /
          #      rag_eval / schemas / services / static（详见 ARCHITECTURE 第 6 节）
skills/   tests/   scripts/   models/   docs/
```

`skills/` 是 7 个标准 SKILL.md，`tests/` 是 23 个单元与回归用例，`scripts/` 是运行、模型、打包、离线验证与演示脚本，`models/` 只含 Ollama Modelfile 与权重放置说明（不含 GGUF），`docs/` 是架构图与深度设计文档。

---

## 3. 环境准备

要求 Python 3.12（与 `requirements.txt`、CI、Dockerfile 一致）。

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # 按需填写；也可以只用环境变量
```

**配置默认值要特别注意**：`app/core/config.py` 的默认值是 `AI_PROVIDER=ollama` 与 MySQL 连接串，因此**一个空 `.env` 并不等于 mock + SQLite**；零服务运行必须显式设置下面第 4 节的变量（`.env.example` 模板已给出这组值）。API Key 只放在本地 `.env`（已被 `.gitignore` 排除），不要写进代码或提交到仓库。

演示账号由启动播种写入：`student / student123`（学生）与 `admin / admin123`（管理员）。

---

## 4. 本地运行

### 4.1 mock + SQLite（无需任何外部服务）

```bash
export AI_PROVIDER=mock
export DATABASE_URL=sqlite:///./data/mindbridge.db
export KNOWLEDGE_VECTOR_ENABLED=false     # 无 embedding Key 时用纯 BM25 检索
export TOOL_QUEUE_ENABLED=false           # 简化本地运行；完整闭环见 5.4

mkdir -p data target                      # SQLite 需要目录已存在
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

打开 <http://127.0.0.1:8080> 使用学生端 / 管理端。`TOOL_QUEUE_ENABLED=false` 时工具派发走 MCP stdio 直连；`=true` 时走数据库队列（SQLite 下也可用，能观察重试、限流、死信与审计链路）。

### 4.2 零服务演示与验证

```bash
python scripts/demo_turn.py                # 三个场景的完整协作事件流
python -m app.harness.runner               # 6 组工程 harness（mock + 临时 SQLite）
python -m unittest discover -s tests       # 23 个单元测试
python scripts/verify_offline.py           # 缺依赖环境下的 33 项断言
```

### 4.3 Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

端口映射：App `8080`、MySQL `13306 → 3306`、Redis `16379 → 6379`。compose 会透传 SMTP 配置，`ALERT_EMAIL_DELIVERY_MODE=log` 时预警只记录不发信。

### 4.4 接真实模型

本地 Ollama 用 `AI_PROVIDER=ollama OLLAMA_MODEL=mindbridge-qwen2.5-7b-ft:latest ./scripts/run-dev.sh`（需自备 GGUF 权重）；接任意 OpenAI 兼容端点则设置 `AI_PROVIDER=openai`、`OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` 后正常启动。

---

## 5. 常见问题

| 问题 | 处理 |
| --- | --- |
| 端口被占用 | 默认 App 端口 8080；compose 中 MySQL / Redis 映到 13306 / 16379，可按需修改环境变量或端口映射 |
| `data/` 或 `target/` 不存在导致启动失败 | SQLite 无法在不存在目录中建库文件；先 `mkdir -p data target`（CI 也单独执行了这一步），或改用已有的 MySQL |
| 空 `.env` 启动后连不上模型 / 数据库 | 代码默认是 Ollama + MySQL；零服务运行需显式设置 `AI_PROVIDER=mock` 与 SQLite `DATABASE_URL` |
| 高风险场景没有台账 / 个案 / 预警 | 完整闭环需要可用的工具执行路径：`TOOL_QUEUE_ENABLED=true`（队列，推荐）或安装 `mcp` 后使用直连；未安装 `mcp` 时聊天与报告正常，但工具产物为空（服务端只记 warning）。`requirements.txt` 已含 `mcp>=1.2.0,<2.0.0`；手动安装 `mcp` 时注意它与 starlette / fastapi 的版本约束 |
| 为什么 `mcp` 要锁 `<2.0.0` | `app/mcp_tools/server.py` 使用 1.x 的 `FastMCP` API，2.x 将其改名为 `MCPServer`，直接升级会导致 MCP 工具服务启动即退出；迁移到 2.x API 属于待办 |
| Key 放在哪里 | 只放本地 `.env` 或进程环境变量；`.gitignore` 已排除 `.env` / `.env.*` 并保留 `.env.example` |
| 没有 Redis / 没有 Chroma 能跑吗 | 能。Redis 不可达时短期记忆降级为空历史；Chroma 或 embedding 不可用时检索退回纯 BM25（见 [RAG](./RAG.md)） |

---

## 6. 后续计划

短中期方向见 [LIMITATIONS](./LIMITATIONS.md) 第 7 节：v0.1.0 公开快照 → v0.2.0 工程加固 → v0.3.0 运维与并发。

---

## 相关文档

- [README](../README.md)：项目定位、能力索引与快速开始
- [ARCHITECTURE](./ARCHITECTURE.md)：分层与目录结构
- [QUALITY](./QUALITY.md)：harness、单测与 CI
- [BENCHMARKS](./BENCHMARKS.md)：可复现命令与实测数据
- [LIMITATIONS](./LIMITATIONS.md)：边界与版本路线
- [API](./API.md)：本地账号、认证与调用示例
