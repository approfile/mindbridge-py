<div align="center">

# MindBridge

**把一句求助，变成一条负责任、可追溯的回复**

*Turn one call for help into a responsible, fully traceable reply.*

校园心理支持场景的多 Agent 后端 · Python / FastAPI

[![Test](https://github.com/approfile/mindbridge-py/actions/workflows/test.yml/badge.svg)](https://github.com/approfile/mindbridge-py/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Runtime](https://img.shields.io/badge/runtime-claim--based%20multi--agent-6d28d9)
![RAG](https://img.shields.io/badge/RAG-Chroma%20%2B%20BM25-0f766e)
![MCP](https://img.shields.io/badge/tools-MCP%20%2B%20async%20queue-c2410c)

[效果演示](#效果演示) · [架构说明](docs/ARCHITECTURE.md) · [安全设计](docs/SAFETY.md) · [质量与测试](docs/QUALITY.md) · [评测数据](docs/BENCHMARKS.md) · [已知边界](docs/LIMITATIONS.md) · [MIT License](LICENSE)

</div>

![MindBridge 学生端：快捷表达与 SSE 流式心理陪伴对话（mock 演示）](docs/screenshots/student-chat-consult.png)

**MindBridge 是一个面向高校心理支持场景的多 Agent 后端**：回答「学生的一句话，如何变成一条既被学生接受、又符合校园危机干预流程、并且全程可追溯的回复」。

一条请求链路同时处理四件事：**理解**（`CHAT` / `CONSULT` / `RISK` 三路意图路由）、**证据**（11 篇校园心理知识库的路由式混合检索）、**安全**（三级风险判定 + 独立安全审查）、**闭环**（报告 / Excel 台账 / 风险个案 / 辅导员预警 + 全链路 trace）。

- **自研 claim-based 多 Agent Runtime**：共享黑板 + 能力认领 + 预算控制（8 轮 / 单轮 4 认领 / 单 Agent 3 认领）+ 五条件安全采纳门闸，不依赖 LangChain / LangGraph，控制流完全可读可审计。
- **安全优先**：三级风险判定（词典硬拦，置信度 0.95 且不调用模型 → 严格 JSON 模型判定 → 启发式兜底）；SafetyAgent 只审查「即将被采纳的那一版」回复，未通过即打回重写；`HIGH` 风险在 4 处强制升级。
- **可复现**：6 组零依赖工程 Harness + 23 个单元测试；60 条 RAG 评测集实测 HitRate@4 **0.967** / MRR **0.908** / NDCG@4 **0.905**，阈值写进 CI 断言。

### 快速开始（mock 模式，3 条命令，无需外部服务）

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
mkdir -p data && AI_PROVIDER=mock DATABASE_URL=sqlite:///./data/mindbridge.db KNOWLEDGE_VECTOR_ENABLED=false TOOL_QUEUE_ENABLED=false uvicorn app.main:app --port 8080
# 浏览器打开 http://127.0.0.1:8080 —— 学生 student / student123，管理员 admin / admin123
```

> `TOOL_QUEUE_ENABLED=false` 时工具派发走 MCP stdio，需已安装 `mcp`（`requirements.txt` 已包含）；未安装时报告仍会生成，但 Excel 台账 / 风险个案 / 预警会被跳过。完整闭环也可改用 `TOOL_QUEUE_ENABLED=true` 的队列模式。

### 项目性质

MindBridge 是我**独立设计与实现**的个人项目（solo project）：需求拆解、架构设计、编码、测试与文档全部由我完成，不存在多人分工，也没有基于现成模板或脚手架生成。下表区分自研部分与开源组件，不冒领后者：

| 自研部分 | 使用的开源 / 现成组件 |
| --- | --- |
| claim-based 多 Agent Runtime（黑板 / 事件 / 认领 / 预算 / 五条件采纳门闸） | FastAPI、uvicorn、SQLAlchemy 2.0、Pydantic |
| 路由式混合检索：BM25、归一化融合、确定性重排、三层降级 | Chroma（向量存储）、OpenAI 兼容 embedding API |
| 三级风险判定、版本绑定安全审查、三层记忆与压缩、Skill 加载校验 | Redis、MySQL、FastMCP（MCP 工具服务底座） |
| 6 组工程 Harness、60 条 RAG 评测集、路由级回归测试 | GitHub Actions（CI） |

---

## 能力地图

| 能力 | 实现要点 | 深入阅读 |
| --- | --- | --- |
| Agent Runtime | 共享黑板、claim-based 调度、预算（8 轮 / 4 认领 / 3 认领）与五条件采纳门闸 | [ARCHITECTURE](docs/ARCHITECTURE.md) · [DESIGN-NOTES](docs/DESIGN-NOTES.md) |
| 安全工程 | 三级风险判定、版本绑定安全审查、`SAFETY_OVERRIDE` 四处消费、学生可见内容隔离、输入脱敏 | [SAFETY](docs/SAFETY.md) |
| RAG | 路由式触发、Chroma + 自研 BM25、归一化融合、确定性重排、三层降级 | [RAG](docs/RAG.md) |
| 记忆 | Redis 短期记忆（40 条 / TTL 24h）、8 条触发压缩、摘要作用域声明 | [MEMORY-SKILLS](docs/MEMORY-SKILLS.md) |
| Skills | 7 个标准 `SKILL.md`、目录与 `## Workflow` 校验、READY / WARN / FAILED（仅 `/api/agent/status` 暴露） | [MEMORY-SKILLS](docs/MEMORY-SKILLS.md) |
| MCP + 队列 | 6 个 MCP 工具、异步队列：3 次尝试 / 15s、30s 线性退避 / 60s 滑窗 30 次限流 / 死信 / 依赖 / 审计 | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| 可观测性 | 全链路 trace、报告 / 台账 / 个案 / 预警闭环、管理端 4 个面板与只读 API | [API](docs/API.md) |

## 效果演示

| 学生端高风险对话 | 管理端总览 |
| :---: | :---: |
| ![MindBridge 学生端高风险对话：mock 安全计划回复，不输出风险标签](docs/screenshots/student-chat-risk.png) | ![MindBridge 管理端：风险个案、咨询报告、RAG 知识库与会话档案](docs/screenshots/admin-console.png) |

*图：mock 模式下真实运行的两端界面。学生端为 SSE 逐 token 输出，高风险时给出安全计划且不展示后台标签；管理端为 4 个面板——风险个案、心理风险与咨询报告、RAG 知识库维护、会话档案（Agent 协作 trace 当前只提供只读 API，无前端视图）。*

| 登录与角色入口 | 报告与会话档案 |
| :---: | :---: |
| ![MindBridge 登录页：学生 / 管理员双角色入口](docs/screenshots/login.png) | ![MindBridge 管理端：HIGH 风险报告与只读会话档案审阅](docs/screenshots/admin-report-detail.png) |

*图：登录页按角色进入学生端 / 管理端；管理端可只读审阅报告对应的会话档案，高风险输入会触发报告、个案与预警闭环。*

两种运行方式：`AI_PROVIDER=mock` 用确定性桩模型零外部服务复现全链路；切换 `ollama` / `openai` 即接入真实模型，其余链路不变（见[快速开始](#快速开始完整版)）。

## 架构与运行时

![MindBridge 端到端链路：客户端 → HTTP 层 → Agent Runtime Harness → 多 Agent Runtime → 服务与存储](docs/architecture-overview.svg)

*图：一条学生消息的端到端链路。HTTP 层只做认证与 SSE 分帧，业务编排（脱敏、报告、trace、工具计划）全部收敛在 Harness。*

```text
POST /api/chat/stream
  └─ ChatService.stream_chat
       ├─ MindBridgeAgentHarness.run        脱敏 → 会话 → 多 Agent 协作 → 报告 → trace
       ├─ AiClient.stream(prompt_plan)       SSE 逐 token 推送
       └─ harness.dispatch_tools(tool_plan)  队列或 MCP，不阻塞流式输出
```

**为什么不用 LangChain / LangGraph**：心理安全场景里「谁在什么时候有权否决一条回复」必须完全可读、可审计。自研 runtime 让调度、预算与采纳门闸就是 `app/agents/` 里的普通 Python 控制流，而不是框架黑盒；代价是约 5.9k 行后端代码要自己维护，换来的是可解释性与可测试性。

![MindBridge claim-based 多 Agent Runtime：共享黑板、控制面与四个 worker](docs/agent-runtime.svg)

*图：claim-based 多 Agent 协作运行时。Worker 观察黑板上的开放任务并按置信度认领，协调者每轮结束按五项条件判断是否收敛；箭头为 claim ↔ publish 双向。*

![MindBridge 异步工具队列：依赖门闸、重试与死信](docs/tool-queue.svg)

*图：异步工具队列。高危触发后的台账 / 个案 / 预警三类工具经策略与依赖门闸后执行，失败按 15s / 30s 线性退避重试（共 3 次尝试），超过上限进入死信。*

完整的调度伪代码、数据模型与目录结构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 工程质量与实测数据

| 维度 | 结果 |
| --- | --- |
| 工程 Harness | 6 组零依赖 suite（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue），mock 模型 + 临时 SQLite + 内存记忆，任一组失败返回非 0 |
| 单元测试 | 23 个用例（标准库 `unittest`，无 pytest 依赖），CI 与 Harness 分两步独立执行 |
| CI | push / PR 到 `main`：安装依赖 → 编译检查 → 单元测试 → Harness → 上传报告 artifact，全程无需外部服务或 API Key |
| 代码规模 | `app/` 44 个 Python 模块、约 5.9k 行；13 张 ORM 表、20 条路由、6 个 MCP 工具、7 个 Skill、11 篇知识库文档、34 个 chunk、13 种协作事件、6 种产物（含 `critique`） |

### RAG 评测（60 条评测集，向量关闭的 BM25 + 重排路径）

| 指标 | 实测值 | Harness 阈值 |
| --- | --- | --- |
| HitRate@4 | **0.9667**（58 / 60） | ≥ 0.95 |
| Recall@4 | **0.9667** | ≥ 0.95 |
| MRR | **0.9083** | ≥ 0.75 |
| NDCG@4 | **0.9053** | ≥ 0.75 |
| Precision@4 | **0.6458** | — |

*指标定义、两条未命中分析与完整结果见 [docs/BENCHMARKS.md](docs/BENCHMARKS.md)。*

![MindBridge 路由式 RAG：混合检索链路与三层降级](docs/rag-pipeline.svg)

*图：路由式 RAG。普通聊天不检索，咨询 / 风险才进入「改写 → 向量 + BM25 召回 → 归一化 → 加权融合 → 确定性重排 → 邻接扩展」；降级路径显式暴露在 /api/admin/knowledge/status。*

```bash
python -m unittest discover -s tests     # 23 tests OK
python -m app.harness.runner             # 6/6 suite PASS，报告写入 target/harness/
python scripts/verify_offline.py         # 33 项断言，无 FastAPI / Redis / Chroma 环境也可跑
```

完整的测试分层与交付前缺陷复盘见 [docs/QUALITY.md](docs/QUALITY.md)。

## 快速开始（完整版）

### 方式一：mock 模式（零外部服务）

按首屏 3 条命令启动后，可先看多 Agent 如何协作而不起服务：

```bash
python scripts/demo_turn.py              # CHAT / CONSULT / RISK 三个场景，打印完整协作事件流
```

### 方式二：Docker Compose（MySQL + Redis + App）

```bash
cp .env.example .env                     # 按需填写真实模型与 SMTP 配置
docker compose up -d --build             # App → http://127.0.0.1:8080
```

### 方式三：接入真实模型

```bash
# 本地 Ollama（models/ 只提供 Modelfile 与放置说明，需自备 GGUF 权重）
AI_PROVIDER=ollama OLLAMA_MODEL=mindbridge-qwen2.5-7b-ft:latest ./scripts/run-dev.sh

# 任意 OpenAI 兼容端点
AI_PROVIDER=openai OPENAI_BASE_URL=https://api.openai.com/v1 OPENAI_API_KEY=sk-... \
OPENAI_MODEL=gpt-4o-mini uvicorn app.main:app --port 8080
```

配置项、开发脚本与本地调试方式见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)；可视化流程与截图见[效果演示](#效果演示)。

## 已知边界

- **调度是单进程顺序执行**：actor-style 只体现在控制流语义，不是并发执行；多 worker 下缺少分布式协调与队列 lease。
- **鉴权为 HTTP Basic Auth + SHA-256 无盐口令哈希**：无 MFA、无令牌过期、无登录限流；生产应替换为 OIDC / JWT。
- **风险词典是子串匹配，无否定检测**：「我不想自杀」会被判 `HIGH`，误报方向偏安全，但会产生不必要的干预。
- **脱敏只覆盖手机号 / 邮箱 / 身份证**：姓名、学号、住址等实体未覆盖。
- **安全审查是规则要素检查，不是回复安全分类器**：能拦住缺少即时安全要素的回复，不等价于语义级安全判别。
- **评测集只覆盖 2 / 11 篇知识库文档**；重排是确定性公式而非学习型模型。
- **工程完备性**：无 Alembic 迁移、结构化日志 / 指标、覆盖率与 lint 门禁；管理端无 trace 前端视图。
- **产品化**：前端未做无障碍与移动端适配；`models/` 不含 GGUF 权重。

完整的边界清单与优先级见 [docs/LIMITATIONS.md](docs/LIMITATIONS.md)。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | 分层结构、端到端链路、Agent Runtime 调度、数据模型、目录结构 |
| [SAFETY](docs/SAFETY.md) | 三级风险判定、安全审查与 revise 循环、五条件门闸、脱敏、工具策略 |
| [RAG](docs/RAG.md) | 路由式触发、混合检索链路、确定性重排、三层降级、指标定义 |
| [MEMORY-SKILLS](docs/MEMORY-SKILLS.md) | 三层记忆、上下文压缩、Skill 体系与校验规则 |
| [API](docs/API.md) | 20 条路由、认证模型、SSE 事件格式、调用示例 |
| [QUALITY](docs/QUALITY.md) | 测试策略、六组 Harness、23 个单测、CI、交付前缺陷审计 |
| [BENCHMARKS](docs/BENCHMARKS.md) | 工程规模、RAG 评测结果、未命中分析、复现命令 |
| [LIMITATIONS](docs/LIMITATIONS.md) | 已知边界、风险等级与改进路线 |
| [DESIGN-NOTES](docs/DESIGN-NOTES.md) | D1–D18 设计决策、关键取舍与边界说明（面向代码评审者） |
| [DEVELOPMENT](docs/DEVELOPMENT.md) | 开发时间线、环境准备、本地运行与常见问题 |

```text
app/
├── agents/       # claim-based runtime：黑板 / 事件 / 5 个 Agent（含协调者）/ Harness
├── api/          # 20 条 HTTP 路由
├── core/         # 配置、数据库、鉴权、启动播种
├── knowledge/    # 11 篇校园心理知识库
├── mcp_tools/    # FastMCP 工具服务（6 个工具）
├── models/       # 13 张 ORM 表
├── services/     # AI、检索、记忆、评估、报告、Skill、工具队列、治理、trace
└── static/       # 原生前端（学生端 + 管理端）
docs/             # 架构图、截图与深度文档
skills/           # 7 个标准 SKILL.md
tests/            # 单元测试 + 路由回归测试
```

## 项目性质与维护

MindBridge 定位是「可运行、可审计、可复现」的校园心理场景后端，**不是生产医疗系统**。项目由 Ziyi Shen（[@approfile](https://github.com/approfile)）独立维护，以 [MIT License](LICENSE) 发布；版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## English Summary

**MindBridge** is a campus mental-health support backend written in Python/FastAPI. It ships a
self-built claim-based multi-agent runtime (shared blackboard, capability-based task claiming,
budgeted scheduling, five-condition safety acceptance gate), a routed hybrid RAG pipeline
(Chroma + custom BM25 + deterministic reranking with layered fallback), a three-tier risk
assessment chain (keyword hard gate → structured LLM JSON → heuristic fallback) with version-bound
safety review, and an MCP-backed async tool queue with retries, rate limiting, dead-letter handling
and risk-scoped audit. Six dependency-free harness suites and 23 unit tests run in CI, and a
60-query retrieval benchmark is enforced as a threshold assertion (HitRate@4 0.967 / MRR 0.908 /
NDCG@4 0.905). This is a solo project and an engineering-focused backend, not a production medical
system or a substitute for professional help.

---

<div align="center">

**MindBridge** — 把「一句话」变成「一条负责任的回复」，并让每一步都可追溯。

非生产医疗系统，不替代专业帮助。如你或身边的人处于紧急危险，请联系校内心理中心、当地心理援助热线，或拨打 110 / 120。

</div>
