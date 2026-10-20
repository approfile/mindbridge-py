# 系统架构

本文覆盖 MindBridge 的分层结构、一次对话请求的端到端链路、claim-based 多 Agent Runtime、Runtime Harness 编排层、13 张数据表与仓库目录；前置阅读：[README](../README.md) 的「这个项目是什么」一节。文中所有数字与可复现口径同 [BENCHMARKS](./BENCHMARKS.md)。

![MindBridge 端到端链路：客户端 → HTTP 层 → Agent Runtime Harness → 多 Agent Runtime → 服务与存储](./architecture-overview.svg)

*图 1：一条学生消息的端到端链路。HTTP 层只做认证与 SSE 分帧，业务编排（脱敏、报告、trace、工具计划）全部收敛在 Harness。*

---

## 1. 分层结构

系统按「传输 → 编排 → 协作 → 服务」四层切分，每层只做一类事：

| 层 | 职责 | 代码位置 | 刻意不做的事 |
| --- | --- | --- | --- |
| HTTP 层 | Basic Auth、参数校验、SSE 分帧、静态前端挂载 | `app/api/routes.py`、`app/static/` | 不写业务编排，不直接访问黑板 |
| Runtime Harness | 一次 Agent run 的输入准备、持久化、报告、trace、工具计划 | `app/agents/harness.py` | 不介入 Agent 内部如何协作 |
| Agent Runtime | 任务派生、认领、预算、产物合并、最终采纳门闸 | `app/agents/coordinator.py`、`autonomous.py` | 不做落库与合规规则 |
| Services | 模型调用、检索、记忆、风险判定、Skill、工具队列、治理、trace | `app/services/` | 不感知 HTTP 请求与 SSE |

技术选型的出发点是**编排层不依赖 LangChain / LangGraph**：多 Agent 协作、任务调度、预算与安全门闸全部自研，让「谁在什么时候有权否决一条回复」可以逐行阅读和审计。

---

## 2. 端到端链路

```text
POST /api/chat/stream            (HTTP Basic Auth, 学生角色)
  └─ ChatService.stream_chat                          app/services/chat.py
       ├─ MindBridgeAgentHarness.run                  app/agents/harness.py
       │    ├─ PrivacySanitizer.sanitize(input)       手机号 / 邮箱 / 身份证 → [已脱敏]
       │    ├─ 解析或新建 ChatSession
       │    ├─ EventDrivenAgentRuntimeService.run      多 Agent 协作，返回 AgentRunResult
       │    ├─ 持久化学生消息（MySQL + Redis 短期记忆）
       │    ├─ intent != CHAT 时写入 PsychologicalReport
       │    └─ AgentTraceService.save_run              完整协作过程落库
       ├─ AiClient.stream(prompt_plan)                SSE 逐 token 推送
       └─ harness.dispatch_tools(tool_plan)           流结束后派发工具，不阻塞输出
```

关键边界：HTTP 层只负责认证与 SSE 分帧，所有业务编排集中在 Harness 内。因此换传输协议（HTTP / 命令行 / 批处理）不需要改动 Agent 逻辑；`scripts/demo_turn.py` 就是复用同一 runtime 的零服务演示。

---

## 3. Agent Runtime

![MindBridge claim-based 多 Agent Runtime：共享黑板、控制面与四个 worker](./agent-runtime.svg)

*图 2：claim-based 多 Agent 协作运行时。Worker 观察黑板上的开放任务并按置信度认领，协调者每轮结束按五项条件检查是否收敛；箭头为 claim ↔ publish 双向。*

### 3.1 为什么不用固定 workflow

心理危机场景里，**参与角色和顺序取决于输入本身**：普通聊天不需要检索知识库和记忆摘要；高风险输入必须强制升级、必须检索、必须由安全角色审查候选回复。固定顺序会带来两种坏情况——普通聊天被迫承担检索与记忆的延迟，高风险流程又可能因为某个前置步骤缺失而被跳过。

因此这里采用 **claim-based actor runtime**：

- `CoordinatorAgent` 不调用其他 Agent，只维护任务板与预算；
- 专业 Agent 主动观察黑板上的开放任务，按能力匹配与置信度自行认领；
- 每轮结束由协调者检查最终采纳条件，满足即收敛。

### 3.2 共享黑板

`app/agents/events.py` 用 `@dataclass(frozen=True)` 定义全部协作数据结构，任何「更新」都返回新对象（`dataclasses.replace`），黑板因此是 append-only 的：

| 结构 | 作用 |
| --- | --- |
| `tasks: dict[str, AgentTask]` | 任务板。`AgentTask` 含 `priority`、`status`、`required_capabilities`、`claimed_by`、`depends_on` |
| `events: tuple[AgentEvent, ...]` | 全量事件流（13 种 `AgentEventType`），是审计与 trace 的事实来源 |
| `artifacts: tuple[AgentArtifact, ...]` | 各 Agent 发布的产物（6 种 kind：`intent` / `risk` / `context` / `response_proposal` / `safety_review` / `critique`），每个都带 `confidence` |
| `messages: tuple[AgentMessage, ...]` | Agent 间定向消息（`recipient` 支持 `*` 广播） |
| `final_artifact_id` | 被最终采纳的回复产物 ID |

### 3.3 一轮对话的调度循环

`EventDrivenCoordinator.run()`（`app/agents/coordinator.py`）：

```text
for round in 1..agent_max_rounds:            # 默认 8 轮
    ROUND_STARTED
    _derive_missing_work()                   # 缺什么产物就补什么任务
    _try_accept_final()                      # 满足条件 -> FINAL_ACCEPTED，立即收敛
    candidates = _claim_candidates()          # 按 优先级 → 置信度 → agent 名 降序
    for task, candidate in candidates:        # 每轮最多 agent_max_claims_per_round 个
        TASK_CLAIMED                          # 认领写回黑板，claimed_by 留痕
        result = candidate.agent.act(task, board)
        apply_turn_result(...)                # 事件 / 产物 / 后续任务 / 关闭任务
    _try_accept_final()
BUDGET_EXHAUSTED                              # 预算耗尽仍未收敛则显式标注
```

**预算控制**由三个独立阈值组成，防止单个 Agent 刷满预算或死循环：

```text
AGENT_MAX_ROUNDS=8                      # 调度轮数上限
AGENT_MAX_CLAIMS_PER_ROUND=4            # 单轮最多执行几个认领
AGENT_MAX_CLAIMS_PER_AGENT=3            # 单个 Agent 整轮对话最多认领几次
AGENT_FINAL_ACCEPTANCE_MIN_CONFIDENCE=0.6
```

### 3.4 五个 Agent 的分工

| Agent | 能力 | 认领依据（decide 置信度） | 产物（artifact 置信度） |
| --- | --- | --- | --- |
| `CoordinatorAgent` | `COORDINATION` | 不认领，由调度循环驱动（`decide()` 恒返回 `False`） | 无产物；负责根任务、预算、`FINAL_ACCEPTED` |
| `UnderstandingAgent` | `UNDERSTANDING` | 存在未完成的 `intent` 任务（0.82） | `intent`：CHAT/CONSULT 为 0.78，RISK 为 0.92 |
| `SafetyAgent` | `SAFETY` | 候选回复待审查（0.95）／风险待评估（高危信号 0.98，否则 0.84）／显式任务（0.80） | `risk`（置信度取评估结果，硬拦为 0.95）；`safety_review` 或 `critique`（0.95） |
| `ContextAgent` | `CONTEXT` | 显式任务（0.86）／咨询或风险路径（0.82） | `context`（0.88）：记忆摘要、模型历史、检索证据、skill 约束 |
| `ResponseAgent` | `RESPONSE` | 普通聊天（0.78）／上下文齐备或高风险（0.84）／显式任务（0.65） | `response_proposal`（0.86，回复 prompt 方案，非最终文本） |

每个 Agent 有独立的 `AgentProfile`（system_prompt、memory_policy、model_profile、tool_permissions）；`AgentModelRegistry` 允许按角色指定不同 provider/model（`AGENT_MODEL_*_PROVIDER` / `AGENT_MODEL_*_MODEL`），例如理解用便宜模型、安全判定用更严格的模型。

### 3.5 最终采纳条件（安全闸门）

`_try_accept_final()` 要求**五个条件同时满足**，任意一条不满足就继续认领任务，直到预算耗尽：

```text
1. 存在 response_proposal 产物
2. 存在 safety_review 产物
3. safety_review.metadata.responseArtifactId == response_proposal.id   ← 审查必须针对当前这一版
4. safety_review.payload.approved is True
5. response_proposal.confidence >= 0.60
```

第 3 条是刻意设计的：防止「审查了旧版本、却采纳了新版本」。如果 `SafetyAgent` 否决候选回复，它会发布 `REVISION_REQUESTED` 和一个 `task:revise-response:*` 任务，由 `ResponseAgent` 重新提出方案。审查与修订细节见 [SAFETY](./SAFETY.md)。

### 3.6 `SAFETY_OVERRIDE` 的作用范围

`SafetyAgent` 判定 `risk == HIGH` 时额外发布 `SAFETY_OVERRIDE` 事件。该事件在**四个位置**被消费，效果是「本轮全局强制升级」而非只改风险字段：

```text
EventDrivenAgentRuntimeService._select_intent()  → intent 直接变为 RISK
EventDrivenAgentRuntimeService._select_risk()    → risk 直接变为 HIGH
EventDrivenCoordinator._risk_value()             → context / response 任务优先级提升到 CRITICAL
autonomous._risk_level()（ResponseAgent 等）      → ResponseAgent 走 support/high-risk 分支
```

---

## 4. Runtime Harness

`MindBridgeAgentHarness`（`app/agents/harness.py`）是业务编排层，把「一次 Agent run」包装成应用可用的一次事务。它不介入内部协作方式，只做七件事：

```text
1. 输入脱敏        PrivacySanitizer.sanitize()          原始文本入库，脱敏文本进模型
2. 会话解析        sessionId 命中则复用，否则新建 ChatSession
3. 调用 runtime    create_agent_runtime() → 事件驱动 runtime
4. 消息持久化      MySQL chat_messages + Redis 短期记忆
5. 心理报告        intent != CHAT 时写入 psychological_reports
6. Trace 落库      AgentTraceService.save_run()          事件 / 任务 / 产物全量快照
7. 工具计划        AgentToolPlan(report_id, risk_level) → 队列或 MCP 直连
```

之所以单独抽这一层：Agent Runtime 只关心「如何在黑板上协作产出候选回复」，而「要不要写报告、写不写 Excel 台账、发不发预警、trace 存哪些字段」是业务与合规问题，会随学校政策变化。两层解耦后，runtime 可以独立测试，业务规则可以独立演进。

`agent_run_traces` 保存的不是最终答案，而是完整协作快照：`intent`、`risk_level`、`original_input`、`sanitized_input`、`memory_brief`、`agent_steps_json`（含 `agent_event` / `agent_task` / `agent_artifact` 三类条目）、`retrieved_knowledge_json`、`response_messages_json`、`assessment_json`。

---

## 5. 数据模型

`app/models/entities.py` 共 13 张表，按职责分为四组：

```text
身份与会话      user_accounts · chat_sessions · chat_messages
知识与检索      knowledge_chunks（含 embedding_json 向量缓存）
风险与处置      psychological_reports · risk_cases · case_notes
                alert_records · excel_records
任务与可观测    tool_jobs · dead_letter_records · tool_audit_records · agent_run_traces
```

几个值得说明的设计：

- `chat_sessions.public_id` 用 UUID 对外暴露，**自增主键不离开服务端**；
- `knowledge_chunks.embedding_json` 缓存已算出的向量，`_embeddings_for_chunks()` 只对缺失项发起请求；
- `risk_cases.report_id` 唯一索引保证个案幂等；`tool_jobs.run_after` + `status` 建索引，支撑轮询式派发；
- `agent_run_traces` 的 JSON 字段保留完整协作过程，便于事后复盘一条不安全回复的决策路径。

---

## 6. 目录结构

```text
app/
├── agents/            # 多 Agent runtime：黑板、事件、注册表、协调者、Harness
│   ├── events.py            CollaborationBlackboard / AgentTask / AgentEvent / AgentArtifact
│   ├── registry.py          能力注册与候选排序
│   ├── autonomous.py        5 个 Agent 的真实实现
│   ├── coordinator.py       claim-based 调度、预算、最终采纳闸门
│   ├── event_driven_runtime.py  对外入口，产出 AgentRunResult
│   ├── harness.py           MindBridgeAgentHarness 业务编排
│   └── result.py            AgentRunResult / AgentStep
├── api/routes.py      # 20 条 HTTP 路由（其中 2 条 async）
├── core/              # 配置、数据库、启动播种、鉴权、枚举
├── harness/           # 6 组工程 Harness
├── knowledge/         # 11 篇内置校园心理知识库
├── mcp_tools/server.py# FastMCP 工具服务（6 个工具）
├── models/entities.py # 13 张 ORM 表
├── rag_eval/          # RAG 评测脚本 + 60 条评测集
├── schemas/           # Pydantic DTO
├── services/          # AI、检索、向量库、记忆、评估、报告、Skill、工具队列、治理、trace
└── static/            # 原生前端（学生端 + 管理端 4 个面板；任务/trace 仅 API）

docs/  skills/  tests/  scripts/  models/
                       # 文档组 / 7 个 SKILL.md / 23 个测试用例 / 本地脚本 / Modelfile
```

---

## 7. 实现边界

- 调度是**单进程顺序执行**：任务板、优先级与认领是一套控制流语义，不代表并发执行；多 worker 下的分布式协调尚未实现。
- `TaskStatus.BLOCKED` 与 `AgentEventType.TASK_RELEASED` 已定义但当前运行时不会发出（死枚举）。
- 20 条路由中只有 `/api/chat/stream` 与 `/api/admin/knowledge/file` 是 `async def`，其余 18 条为同步 `def`（FastAPI 放入线程池）。
- 工具队列与 MCP 直连复用同一套 `ToolOrchestrationService`，但重试、限流、死信与策略审计等队列治理能力只在 `TOOL_QUEUE_ENABLED=true` 时生效（完整边界见 [LIMITATIONS](./LIMITATIONS.md)，设计取舍见 [DESIGN-NOTES](./DESIGN-NOTES.md)）。

---

## 相关文档

- [README](../README.md)：项目定位、快速开始与能力索引
- [SAFETY](./SAFETY.md)：风险判定、安全审查与隐私边界
- [RAG](./RAG.md)：路由式检索与混合检索链路
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：分层记忆、上下文压缩与 Skill 体系
- [API](./API.md)：认证模型与 20 条路由
- [QUALITY](./QUALITY.md)：测试策略与交付前缺陷审计
- [BENCHMARKS](./BENCHMARKS.md)：实测数据与复现命令
- [LIMITATIONS](./LIMITATIONS.md)：已知边界与演进路线
- [DEVELOPMENT](./DEVELOPMENT.md)：开发时间线、环境准备与本地运行
- [DESIGN-NOTES](./DESIGN-NOTES.md)：关键设计决策与取舍
