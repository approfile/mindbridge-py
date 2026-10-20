# 设计说明 / Design Notes

本文覆盖 MindBridge 的关键设计决策、权衡与已知边界；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md) 与 [SAFETY](./SAFETY.md)。本文面向代码评审者，用于支撑设计评审与源码对照，不以岗位叙事组织内容。建议先读架构与安全文档，再按 D1–D18 逐条对照源码。文中数字口径与实测见 [BENCHMARKS](./BENCHMARKS.md)，运行时参数以 `app/core/config.py` 为准。

---

## 一、设计概览

MindBridge 是面向高校心理支持场景的 Python / FastAPI 后端。它把「学生一句话」处理成一条可追溯的安全回复，并在一条请求链路上同时处理四件事：

| 关注点 | 做法 |
| --- | --- |
| 理解 | `CHAT / CONSULT / RISK` 三路意图路由，普通问题不查知识库 |
| 证据 | 校园心理知识库（11 篇）混合检索：向量召回 + 自研 BM25 + 分数融合 + 本地重排 |
| 安全 | 词典硬拦 → LLM 结构化评估 → 启发式兜底；`SafetyAgent` 独立审查当前版本回复 |
| 闭环 | 心理报告落库、Excel 台账、风险个案、辅导员预警、全链路 trace |

编排层不依赖 LangChain / LangGraph：多 Agent 协作、任务调度、预算控制、安全门槛全部自研。核心目标是让「谁在什么时候有权否决一条回复」完全可读、可审计。

---

## 二、技术栈与规模速查

| 维度 | 数值 / 内容 |
| --- | --- |
| 语言与框架 | Python 3.12、FastAPI 0.115、SQLAlchemy 2.0、pydantic-settings |
| ORM 数据表 | 13 张 |
| HTTP 路由 | 20 条（其中 2 条 `async def`） |
| MCP 工具 | 6 个（`mindbridge_excel_report` / `mindbridge_case_create` / `mindbridge_alert_send` / `mindbridge_alert_ack` / `mindbridge_case_note_add` / `mindbridge_alert_notify`） |
| 标准 Skill | 7 个（`skills/*/SKILL.md`） |
| 内置知识文档 | 11 篇 |
| 知识切块 | 34 个 chunk，切块参数 512 / 64 |
| RAG 评测集 | 60 条 |
| Agent 角色 | 5 个（1 个协调 + 4 个专业：Understanding / Safety / Context / Response） |
| 协作事件类型 | 13 种 |
| 产物类型 | 6 种（`intent` / `risk` / `context` / `response_proposal` / `safety_review` / `critique`；`critique` 为审查否决产物） |
| 工程 Harness | 6 组（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue） |
| Runtime 预算 | `agent_max_rounds=8`、`agent_max_claims_per_round=4`、`agent_max_claims_per_agent=3` |
| 验收置信度门槛 | `agent_final_acceptance_min_confidence=0.6` |
| 词典硬拦置信度 | 0.95（且不调用模型） |
| 检索权重 | 向量 0.65 / BM25 0.35；BM25 `k1=1.5`、`b=0.75`；Embedding 批量 20 |
| 重排公式权重 | `base*0.55 + lexical*0.25 + query coverage*0.15 + phrase*0.05` |
| 记忆 | 短窗口 40 条消息、TTL 86400s；模型历史 `chat_history_limit*2 = 20` 条；摘要上限 500 字 |
| 工具队列 | 最多 3 次尝试；线性退避 15s / 30s（第 3 次失败转 DEAD，不再退避）；邮件限流 60 秒滑窗 30 封/分钟 |

### 端到端链路速查

| 步骤 | 位置 | 说明 |
| --- | --- | --- |
| 1. 入口 | `POST /api/chat/stream` | 路由是 async（20 条路由中的 2 条 async 之一） |
| 2. 服务层 | `ChatService.stream_chat`（`app/services/chat.py`） | 组织一次对话调用与 SSE 输出 |
| 3. Harness | `MindBridgeAgentHarness.run`（`app/agents/harness.py`） | 脱敏、会话、runtime、落库、报告、trace、工具计划 |
| 4. 运行时 | `create_agent_runtime` → `EventDrivenAgentRuntimeService.run` | 装配运行时 |
| 5. 协调 | `EventDrivenCoordinator.run`（`app/agents/coordinator.py`） | 派生任务、排序认领、调用 `act()`、合并结果 |
| 6. 执行 | 各 Agent 的 `decide()` / `act()` | 发布 intent / risk / context / response_proposal / safety_review / critique 产物 |
| 7. 结果 | `AgentRunResult` → `AgentTraceService.save_run` | 落结构化 trace |
| 8. 输出 | `AiClient.stream`（`app/services/ai.py`）SSE token 流 | 链路上被 await 的流式环节 |
| 9. 收尾 | `dispatch_tools` | 流结束后构建 `AgentToolPlan` 并派发工具 |

### Agent 角色与置信度速查

置信度分两类：**认领置信度**是 `decide()` 返回的「我是否该做这个任务」，**产物置信度**是 `act()` 发布 artifact 时带的数值，两者不可混读。

| Agent | 能力 | 产出 | 认领置信度（decide） | 产物置信度（artifact） | 是否认领 |
| --- | --- | --- | --- | --- | --- |
| CoordinatorAgent | 调度 | 无 | - | - | 否，`decide()` 恒为 False |
| UnderstandingAgent | 理解意图与主题 | intent | 0.82 | 0.78（CHAT/CONSULT）/ 0.92（RISK） | 是 |
| SafetyAgent | 风险判定 + 回复审查 | risk、safety_review / critique | 0.95（待审查）/ 0.98（高危信号）/ 0.84（普通评估）/ 0.80（显式任务） | risk 取评估置信度（硬拦 0.95）；review/critique 0.95 | 是 |
| ContextAgent | 记忆 + RAG + Skill | context | 0.86（显式任务）/ 0.82（支持路径） | 0.88 | 是 |
| ResponseAgent | 生成回复方案 | response_proposal | 0.78（普通聊天）/ 0.84（上下文齐备或高危）/ 0.65（显式任务） | 0.86 | 是 |

每个 Agent 都有独立的 `AgentProfile`（system_prompt、memory_policy、model_profile、tool_permissions）；`AgentModelRegistry` 允许按角色指定不同 provider/model（`AGENT_MODEL_*_PROVIDER` / `AGENT_MODEL_*_MODEL`）。

### 检索与工具策略速查

RAG 流水线（仅在意图为 CONSULT/RISK 或风险为中/高时执行，触发点在 `ContextAgent.decide()` / `act()` 与协调者的 `_derive_missing_work()`）：

1. LLM 查询改写，截断到 60 字；
2. Chroma 向量召回（余弦，`score = 1/(1+distance)`）+ 自实现 BM25（`k1=1.5`、`b=0.75`，分词混合英文/数字词、单个汉字与汉字二元组）；
3. 按来源分别做 min-max 归一化；
4. 加权融合：向量 0.65、BM25 0.35；向量召回为空时向量权重强制为 0；
5. 确定性本地重排：`base*0.55 + lexical*0.25 + query coverage*0.15 + phrase*0.05`；
6. 对 top-1 chunk 做同源邻居扩展（索引 ±1）。

工具风险范围与作业链：

| 工具 | 允许的风险范围 | 触发条件 |
| --- | --- | --- |
| `EXCEL_REPORT` | LOW / MEDIUM / HIGH | 每次报告都入队 |
| `CASE_CREATE` | MEDIUM / HIGH | 中/高风险 |
| `ALERT_SEND` | HIGH | 仅高危，且 `depends_on_job_id` 指向 CASE_CREATE |

---

## 三、核心概念

| 概念 | 解释 | 代码位置 |
| --- | --- | --- |
| claim-based runtime | 协调者不派活，只把待办拆成任务放到板上，Agent 按能力和优先级自己认领 | `app/agents/coordinator.py` |
| 共享黑板 | 全流程唯一的协作状态载体，任务、消息、产物、事件都存在里面 | `app/agents/events.py` |
| append-only 不可变状态 | 黑板是 frozen dataclass，所有更新都走 `dataclasses.replace` 重建新实例 | `app/agents/events.py` |
| budget | 对轮数与认领次数的硬上限；轮次之间出现空档即发 `BUDGET_EXHAUSTED` 强制收敛 | `app/agents/coordinator.py` |
| SAFETY_OVERRIDE | 安全 Agent 判定 HIGH 时发出的信号，被四处消费：改意图、改风险、抬任务优先级、抬自主运行风险级别 | `events.py`、`coordinator.py`、`autonomous.py` |
| acceptance gate | 最终采纳的五条件：有提案、有审查、审查针对当前版本、审查通过、置信度达标 | `app/agents/coordinator.py` |
| artifact | Agent 被承认的输出形态，共 6 种，带 id / kind / payload / confidence / metadata | `app/agents/events.py` |
| Harness 分层 | Runtime 只管协作；脱敏、落库、trace、工具计划等业务规则放在外层 Harness | `app/agents/harness.py` |
| 路由式 RAG | 只有咨询/风险意图或中/高风险才检索 | `app/agents/autonomous.py`、`app/agents/coordinator.py` |
| 混合检索 | 向量召回与自实现 BM25 各出一份结果，按来源 min-max 后加权融合 | `app/services/knowledge.py` |
| 三层降级 | 构造期软降级 / 构造期快速失败 / 检索期降级 + 融合期权重归零 | `app/services/vector_store.py`、`knowledge.py` |
| 记忆分层 | 全量历史在 MySQL、最近 40 条热窗口在 Redis、每个 Agent 有私有记忆命名空间 | `app/services/memory.py` |
| 上下文压缩 | 历史 > 8 条时发「摘要消息 + 最近 8 条」，摘要确定性生成且限长 500 字 | `app/services/memory.py` |
| Skill 加载校验 | 自解析扁平 YAML 前置元数据，并校验目录名、必需小节、描述长度、模板 | `app/services/skills.py` |
| 三级风险判定 | 词典硬拦（不调模型）→ 严格 JSON 模型判定（含一致性纠正）→ 启发式兜底 | `app/services/assessment.py` |
| 工具策略门闸 | 每个工具声明允许的风险范围，执行前过 `require_allowed()`，每次执行写审计 | `app/services/tool_governance.py`、`tool_queue.py` |
| 死信队列 | 重试到上限仍失败的作业转 DEAD，并记录类型、原因、载荷 | `app/services/tool_queue.py` |
| trace | 每次运行落意图、风险、输入、记忆摘要、协作步骤、检索结果、回复、评估 | `app/services/trace.py` |

---

## 四、设计决策（D1–D18）

### D1. 为什么不用 LangChain / LangGraph？

- 核心诉求是**控制流可解释、可审计**，而不是快速拼装。现成框架会对「谁在什么时候能做什么」做一层封装，出问题时排查要穿过框架内部；自建 runtime 的调度逻辑集中在 `app/agents/coordinator.py`，可以逐行追踪。
- 安全要求反向约束了架构：词典硬拦必须在任何模型调用**之前**发生（有单测用会抛异常的 AI stub 证明高危词命中时模型确实没被调用），这要求对调用顺序有完全控制权。
- 协作事件与产物直接是领域模型，最终写进 `agent_run_traces`，不需要再做「框架概念 → 业务概念」的翻译。
- 代价：没有现成的持久化执行、断点续跑、分布式调度和生态集成；当前靠单进程顺序执行与启动复位兜底（见 [LIMITATIONS](./LIMITATIONS.md)）。这不是「框架不好」，而是把复杂度换成了可解释性与可测试性。

### D2. 为什么不用固定 workflow？

- 固定 workflow 的问题是**参与角色和执行顺序在编码期就定死**。「这一轮需要跑哪些 Agent」本质上取决于输入：普通聊天不会触发 `ContextAgent`，高危输入则跑满全集。
- 用任务板做运行时派生：协调者根据黑板状态派生缺失的工作（understand、assess-safety、gather-context、propose-response、review-response、revise-response），再由 Agent 认领，因此执行集合是输入驱动的。
- 副产品是可扩展性：新增能力只需要一个声明了对应能力的新 Agent，加上它认领的任务类型，不需要改主流程的 if/else。
- 代价：链路不再是「看一眼代码就知道这次跑了什么」，必须看 trace 才能确定实际参与集合；这也是把「高危必跑全集、普通聊天不检索」做成自动化断言的原因。

### D3. 黑板为什么用不可变数据结构？

- `CollaborationBlackboard` 是 frozen dataclass，字段是 `tasks` dict、`messages` / `artifacts` / `events` 三个 tuple 与 `final_artifact_id`，所有写入都通过 `dataclasses.replace` 产生新实例。
- 直接好处是**没有就地修改**：任务状态变更、产物发布、事件追加都是整块重建，读者拿到的状态不会在读取过程中被改掉，顺序执行的调度循环因此不需要锁去防「读到半个状态」。
- 第二重好处是审计语义：状态演进是一串快照，事后 trace 与事件序列能对上，可以按事件回放。
- 代价：整块重建对大数据量不划算，而且只解决**单进程内**的可见性问题；多 worker 部署时跨进程协调（例如队列 RUNNING 任务抢占）不在这个设计的覆盖范围内。

### D4. 怎么防止 Agent 无限循环？

三层约束，一层比一层硬：

1. **轮数上限**：`agent_max_rounds=8`；轮与轮之间如果出现需求空档，直接发 `BUDGET_EXHAUSTED`，不再空转；
2. **认领上限**：`agent_max_claims_per_round=4`（单轮总认领数）与 `agent_max_claims_per_agent=3`（单个 Agent 的总认领数）；
3. **收敛条件**：五条件门闸本身就是终止判据；协调者不会为了「再想一轮」而继续。

另外 `CoordinatorAgent.decide()` 恒为 False，它永远不认领任务、不产出产物，因此不存在「协调者把自己的任务再拆一遍」的自激循环。需要说明的是：预算是**硬截断**而不是优雅收敛，预算耗尽时可能拿不到最终产物，这类运行在 trace 中表现为没有最终采纳产物，属于已知行为。

### D5. 怎么保证安全审查不会被绕过？

- 关键设计是**采纳权不在回复方手里**：`ResponseAgent` 只能发布 `response_proposal`，没有能力把回复标记为最终结果；只有协调者的 `_try_accept_final` 能设置 `final_artifact_id`。
- 门闸是五条件同时成立：存在提案；存在安全审查；审查的 `metadata.responseArtifactId` 等于当前提案 id；`approved` 为真；提案置信度 ≥ 0.6。
- 高危时 `SAFETY_OVERRIDE` 被四种消费者分别消费：强制意图为 RISK、强制风险为 HIGH、把上下文与回复任务优先级抬到 CRITICAL、抬升自主运行的风险级别。
- 端到端断言兜底：Risk Safety harness 检查真实 SSE 输出不含 `["风险等级","报告ID","emotionScore","HIGH_RISK"]`。
- 边界：审查本身是**基于要素检查的规则审查**，不是回复安全分类器；它的设计目标是拦住「缺少即时安全要素」的回复；实现上以要素词判断，接近格式检查，不等价于语义层面的安全判别。

### D6. 为什么要审查「当前这一版」回复？

- 审查和回复之间会插入**修订循环**：高危回复缺少即时安全要素时，安全 Agent 发布 `critique` 并发 `REVISION_REQUESTED`，协调者派生 `revise-response:{id}` 任务，`ResponseAgent` 重做一版。
- 如果门闸只检查「存在一次通过审查」，那么第一版被审、第二版被改坏，仍然能通过。所以验收条件写成 `safety_review.metadata.responseArtifactId == response_proposal.id`：审查必须指向**即将被采纳的那一版**。
- 换句话说，修订会让旧的审查自动失效，必须重新审查新版本。这是「审查对象可追溯」而不是「审查动作发生过」。
- 代价：每次修订都要再走一轮安全审查，高危路径的延迟和模型调用次数都会增加。这里选择安全优先。

### D7. 为什么高危必须保留词典硬拦？

- 直接原因是失败模式不对称：在危机场景里，「模型不可用」绝不能解释成「没有风险」。如果风险判定完全依赖模型，超时、限流、返回格式错误这些工程故障会直接转化为漏判。
- 所以链路最前面是硬词典：命中 `HIGH_RISK_WORDS` 立即判 HIGH、置信度 0.95，完全不调用模型。单元测试用一个「一旦被调用就抛异常」的 AI stub 验证这一点。
- 后两级是补充而非替代：严格 JSON 的模型判定负责开放式表达，并做一致性纠正（`emotionScore >= 4` 强制 HIGH、`>= 3` 强制 MEDIUM、`emotion == HIGH_RISK` 强制 HIGH）；第三级是模型失败时的启发式兜底。
- 代价明确：词典是子串匹配、**没有否定检测**，「我不想自杀」会被判成 HIGH。方向上是「宁可多干预」，但会产生不必要的干预，改进方向是引入否定作用域识别。

### D8. 为什么重排不用模型？

- 重排是确定性公式：`base*0.55 + lexical*0.25 + query coverage*0.15 + phrase*0.05`，输入是融合分、词法重叠、查询覆盖率与短语命中，不依赖额外模型。
- 理由有三条：一是**规模**，知识库只有 34 个 chunk（11 篇按 512/64 切块），几十到几百量级的候选用大模型重排，收益远小于部署成本与延迟；二是**可解释**，每一分来源都能拆开；三是**可回归**，确定性公式能在评测集上稳定复现，不会因重排模型换版本导致分数漂移——安全敏感域里这比多挣一点 NDCG 重要。
- 代价：公式无法理解语义相似但用词完全不同的表述，也无法做真正的相关性建模。知识库规模或语言复杂度上升后，这一步需要替换为学习型重排，并保留公式作为降级路径。

### D9. 向量库挂了怎么办？

- 降级分三层，处理不同失败时机：
  1. **构造期软降级**：缺少 API Key 或 chromadb 不可用 → `can_embed=False`，系统以纯词法方式继续工作；
  2. **构造期硬失败（可选）**：`KNOWLEDGE_VECTOR_REQUIRED=true` 时同样条件直接抛错，用于「部署时就要求检索能力完整」的场景；
  3. **检索期降级**：embedding 或 Chroma 在查询时抛异常，记 warning 后回退纯 BM25；required 模式则原样抛出。
- 融合阶段细节：向量权重默认 0.65，但向量召回为空时强制置 0，避免「没有向量结果却仍按 0.65 权重拉低词法命中」。
- Embedding 通过 OpenAI 兼容 `/embeddings` 批量调用（每批 20 条），`knowledge_chunks.embedding_json` 缓存向量、只重算缺失部分，因此临时故障恢复后不需要全量重建。
- Chroma 是持久化集合，每次 upsert 同时生成快照（默认保留最近 5 份），便于回滚到上一可用状态。
- 局限：harness 环境强制 `KNOWLEDGE_VECTOR_ENABLED=false`，所以公开的检索分数实际是 BM25 + 本地重排路径的分数，不能当作完整混合检索的成绩单。

### D10. 为什么压缩摘要要声明「不要展示给学生」？

- 触发条件：历史超过 8 条时发送 `[system 摘要消息, 最近 8 条]`。摘要是确定性生成的：最近 4 条用户消息各取 80 字 + 最近 3 条助手消息各取 70 字 + 当前输入 80 字，总长上限 500 字。
- 问题在于：**被压缩掉的恰恰可能是风险信息**。摘要里出现「学生提到自伤念头」是合理的内部上下文，但它绝不能作为 assistant / 系统消息被回显给学生。
- 所以摘要消息带显式范围声明：仅供内部上下文使用、不要展示给学生、不要输出诊断或风险等级、不要暴露后端标签。这既是提示词层面的约束，也把「这条消息属于什么范围」显式写进上下文。
- 同一原则在链路其它地方一致执行：CHAT 分支的提示模板禁止输出后端元数据；harness 断言真实 SSE 输出不含禁用词。
- 局限：这是**靠声明与断言约束**，不是靠输出侧的结构化过滤；更硬的做法是在出口做字段级白名单，属于改进方向。

### D11. 工具为什么走队列而不是同步调用？

- 有两条执行路径，复用同一份实现（`ToolOrchestrationService`）：默认是数据库支撑的异步队列，`TOOL_QUEUE_ENABLED=false` 时切换为 MCP stdio 直连。
- 选这套结构的理由：**演示路径和生产路径不能行为分叉**。如果 demo 直接同步调用、线上走队列，那么限流、重试、依赖、审计在 demo 里根本不执行，等于没被验证。共用实现后，两条路径的差异只在于「谁来触发」。
- 但要说清边界：**队列治理能力只在队列路径生效**。策略门闸、审计、限流、重试、死信、依赖校验都在 `ToolQueueWorker._run_job()` 内；`TOOL_QUEUE_ENABLED=false` 的 MCP 直连路径会绕过 `ToolPolicyRegistry` 与 `tool_audit_records`。
- 队列带来的能力：限流（60 秒滑窗、默认 30 封/分钟，超限返回 `retry_after` 重新入队）、重试（最多 3 次尝试、线性退避 15s / 30s，第 3 次失败转死信）、失败转 DEAD 并记录、作业依赖、重启恢复（启动时把残留 RUNNING 重置为 PENDING）、每次执行写工具审计。
- 代价：引入最终一致性与运维面——作业可能延迟执行、可能进死信需要人工处理，调用方不能立刻拿到结果。对「发预警」这种可异步动作划算，对需要即时反馈的动作不合适。

### D12. 怎么保证预警不会在个案创建前发出？

- 靠**作业依赖 + 执行时校验**，而不是入队顺序。高危报告入队三个作业：`EXCEL_REPORT` 恒发；`CASE_CREATE` 仅中/高风险；`ALERT_SEND` 仅 HIGH，且 `depends_on_job_id` 指向对应 CASE_CREATE 作业。
- 关键点是依赖在**执行那一刻**检查，而不是入队时按顺序假定。因此即使队列乱序、即使 CASE_CREATE 先失败重试，预警也不会提前发出——它只会等待或最终失败。
- 这条路径在 Tool Queue harness 中被断言：一份 HIGH 报告恰好产生 3 个作业，且预警作业依赖关系成立。
- 一个刻意设计：AlertRecord 的去重发生在 log/smtp 分支之前，所以重复通知**按设计不会重复发送**。对同一条风险事件反复告警的场景这是防止打扰；如果需求是「每次状态变化都要通知」，需要调整去重键，这是已知取舍。

### D13. 怎么测试这种不确定的 Agent 系统？

- 思路是「**把不确定性关在边界外，把确定性行为写成断言**」：mock AI 替换模型调用、临时 SQLite 建库、内存实现替换 `RedisShortTermMemoryStore`、强制 `KNOWLEDGE_VECTOR_ENABLED=false`，因此整组测试不需要外部服务或 API Key。
- 每组 suite 独立 drop/recreate schema 并重新播种，避免依赖上一组残留；任一 suite 失败 runner 返回非零退出码，可直接接 CI。
- 断言的是行为契约而非模型输出文本：Risk Safety 断言真实 SSE 输出不含后台禁用词；Agent Routing 断言普通聊天不运行 `ContextAgent`、不触发检索，高危跑满全集；Standard Skills 断言 7 个 skill 的加载、选择与模板渲染；RAG 断言四项阈值；API 断言 403 与 SSE 事件；Tool Queue 断言三作业依赖、幂等、限流与死信。
- 单元测试与 harness 是**两套独立校验**：23 个 `unittest` 用例由 CI 单独执行，harness 不调用 unittest。
- 边界：6 组 suite 是**契约级**测试，不衡量回答质量；回答质量依赖 60 条 RAG 评测集，而评测集覆盖面有限。此外没有覆盖率门禁与 lint / type check 配置，harness 通过不等于全仓无回归。

### D14. 两个典型缺陷的根因分析

**其一：三个管理接口恒返回 500。**

- 现象：`GET /api/admin/agent-traces`、`GET /api/admin/tool-audits`、`GET /api/admin/conversations/{id}` 三条路由始终 500，管理端「会话」视图因此不可用。
- 根因：不是逻辑错误，是**缩进错误**——`app/services/report.py` 中 `agent_run_traces()`、`tool_audits()`、`conversation()` 被定义在模块级而不是 `ReportService` 类内，实例上取不到属性，调用即 `AttributeError`。
- 为什么没被发现：API harness 恰好没有覆盖这三条路由，普通单测也不经过它们。方法级测试无法发现「方法不在类里」这种问题，只有真的发一次请求才会暴露。
- 修复与防复发：移回类内；新增 `tests/test_admin_api_routes.py`，用 `TestClient` 真实请求三条路由并覆盖 403 / 404 分支。

**其二：任务认领记录从未落盘。**

- 现象：`AgentTask.status` 永远不会出现 `CLAIMED`，`claimed_by` 恒为空，trace 的 `claimedBy` 恒为 `[]`——claim-based 调度在数据上看起来从未发生过认领。
- 根因：`claim()` 返回新实例，但协调者把**未认领的旧实例**继续传给 `act()` / `apply_turn_result()`，导致 CLAIMED 状态与 `claimed_by` 被旧值覆盖丢失。不可变结构让这次丢失非常安静：没有异常、没有日志。
- 为什么没被发现：协作流程跑通、产物产出、回复正常，功能层面看起来完全正常，丢的只是审计字段；只有对比「数据应该长什么样」与「实际长什么样」才能发现。
- 修复与防复发：认领后的实例传入后续调用，并把 `claimedBy` 记入 `TASK_CLAIMED` 事件 metadata；回归测试同时断言黑板 `claimed_by` 与事件 metadata 一致，两处交叉验证。

### D15. Coordinator 为什么不自己调用 Agent？

- 这是刻意的不对称设计。协调者只做四件事：创建根任务、根据黑板状态派生缺失工作、把认领候选按 `(priority, confidence, agent name)` 降序排序、在 Agent 执行完后调用 `apply_turn_result` 合并结果。
- 它不进 `decide()` 的认领流程（`CoordinatorAgent.decide()` 恒为 False），也不持有专业能力。这样「谁决定做什么」和「谁去做」被分开：调度策略变化不会污染专业逻辑，专业 Agent 替换也不影响调度。
- 每个 Agent 有独立的 `AgentProfile`，配合 `AgentModelRegistry` 可以按角色指定不同 provider/model。
- 代价：多一层间接，链路追踪必须依赖事件与 trace 才能还原「这一轮是谁因为什么被选中」。

### D16. 每次请求都会跑满 5 个 Agent 吗？

- 不会。参与集合取决于输入：认领是能力驱动的，任务上带 `required_capabilities`，只有具备该能力的 Agent 才会成为候选。
- 两个可验证的极端：普通聊天不会运行 `ContextAgent`（不检索、不组装上下文，harness 有断言），也没有报告产出；高危输入则跑满全集，因为安全覆盖会同时抬升上下文与回复任务的优先级到 CRITICAL。
- 这也解释了为什么「Agent 数量」不是这个项目的重点——重点不是并行度，而是「该来的角色一定会来、不该来的角色不会浪费一次模型调用」。
- 边界：调度是**单线程顺序执行**的；任务板、优先级与认领是一套控制流语义，不代表真的并发执行。

### D17. 为什么把落库、报告、工具计划放在 Harness 而不是 Runtime？

- Runtime 的职责边界是「多 Agent 怎么协作」；Harness 承担 7 件与协作无关的事：输入脱敏、解析或创建会话、调用 runtime、持久化消息（MySQL + Redis）、在意图不为 CHAT 时生成心理报告、保存 agent trace、流结束后构建 `AgentToolPlan` 并派发工具。
- 收益是**变化频率不同的东西分开**：业务与合规规则（报告、台账、预警、trace 字段）跟着监管与产品需求变，协作机制相对稳定。放在同一层里会导致任何一处合规调整都要改动协作代码。
- trace 表 `agent_run_traces` 由这一层写入，字段包括：`intent` / `risk_level`（最终意图与风险）、`original_input` / `sanitized_input`（原文与脱敏后输入）、`memory_brief`（记忆摘要）、`agent_steps_json`（`agent_event` / `agent_task` / `agent_artifact` 三类步骤条目）、`retrieved_knowledge_json`（命中的知识片段）、`response_messages_json`（模型消息与服务端回复）、`assessment_json`（风险判定细节）。
- 需要如实说明：这张表保存了 `original_input` 原文，管理员 trace 接口可读取，属于敏感数据，部署时应按最小权限原则收紧或做二次审计。
- 副作用是清楚的：Harness 成了链路编排点，读代码从它入手才能看清完整请求路径。

### D18. 请求路径里 async 和 sync 混用，怎么解释？

- 事实是：20 条 HTTP 路由中只有 `/api/chat/stream` 与 `/api/admin/knowledge/file` 两条是 `async def`；Agent 流水线内部是同步的（同步 httpx、同步 SQLAlchemy），只有 token 流式输出是 await 的。
- 现实原因：最初的链路以「能一条线跑通、能同步调试」为优先，把异步化留到最后。直接后果是一次聊天请求会占用一个事件循环线程，agent 流水线越长，并发能力越受限。
- 这是**已知的技术债**，不是设计选择。改进方向是把模型调用与 DB 访问换成异步驱动（async httpx / async SQLAlchemy session），并让工具派发从「流结束后同步执行」改成提交队列后立即返回（队列本身已是异步语义）。
- 目前能被兜住的部分：风险判定有词典与启发式两级，模型不可用不会变成漏判；工具走队列后有重试与死信，失败可查。但模型调用本身没有重试与熔断，容量与稳定性仍是单机水平。

---

## 五、已知边界与取舍

| 边界 | 现状 | 改进方向 | 优先级 |
| --- | --- | --- | --- |
| 并发与多 worker | 调度循环单进程顺序执行；黑板无锁、不可变结构只在单进程内安全；多 worker 下种子数据与队列 RUNNING 抢占缺少协调 | 引入队列 lease / visibility timeout 与跨进程协调，或把调度收敛到单一消费者 | P1 |
| 事务边界 | 每条消息独立 commit，无跨步骤事务；置 RUNNING 与提交线程池之间存在崩溃窗口，靠启动复位兜底 | 用显式事务边界包住一次 run 的关键写入，并为队列加 lease 与幂等键 | P1 |
| 鉴权强度 | Basic Auth + SHA-256 无盐哈希，无 MFA、无令牌过期、无登录限流 | 换成 OIDC/JWT，口令哈希升级为 bcrypt/argon2，补限流与会话过期 | P1 |
| 脱敏覆盖面 | 只覆盖手机号、邮箱、18 位身份证号，统一替换为 `[已脱敏]`；姓名、学号、住址、银行卡、微信号未覆盖 | 扩展实体类型与规则，并在出口做字段级白名单而不是只靠正则 | P2 |
| 否定检测 | 风险词典是子串匹配，无否定作用域识别，「我不想自杀」会被判 HIGH；误报方向安全但会产生不必要干预 | 引入否定 / 假设 / 引用语境识别层，并加独立的回复安全分类器 | P1 |
| 评测集覆盖面 | 60 条评测集只引用 11 篇内置文档中的 2 篇；`Recall@K` 是二值近似 | 覆盖全部文档与更多意图分支，引入分级相关性标注替代二值 Recall | P2 |
| CHROMA 在评测中被关闭 | harness 强制 `KNOWLEDGE_VECTOR_ENABLED=false`，公开分数是 BM25 + 本地重排路径 | 增加一组带向量后端的评测运行，两组结果并列登记并说明适用条件 | P2 |
| trace 存原文 | `agent_run_traces` 存 `original_input`，管理员 trace 接口可读 | 限制原文读取（或只存哈希 / 脱敏版），并对访问做二次审计 | P1 |
| 无迁移工具与可观测性 | 没有 Alembic，建表依赖 `create_all`；没有结构化日志、请求 ID、Prometheus 指标；无覆盖率门禁、无 lint / type check | 引入迁移工具、结构化日志与指标埋点，把覆盖率与静态检查纳入 CI | P2 |
| reranker 不是模型 | 重排是确定性公式，不理解语义相似 | 知识库规模或语言复杂度上升后替换为学习型重排，保留公式作为降级路径 | P2 |
| claim-based 是单线程顺序执行 | 「actor-style」只体现在控制流语义上；协调者排序后逐个调用 `agent.act()`，没有真正并发 | 在任务粒度上引入并发执行与结果合并顺序保证，保持黑板不可变语义 | P3 |
| BLOCKED / TASK_RELEASED 为死枚举 | 两个值已定义但从未被发出，运行时不会真的把任务标记为阻塞 | 实现阻塞与释放的实际流转（含超时回收），或删除这两个枚举值以对齐文档与行为 | P3 |
| 工具治理只在队列路径 | MCP 直连绕过策略门闸、审计、限流、重试 | 两条路径统一走治理层，或在直连模式显式告警 | P2 |

其他已知项：模型调用本身没有重试与熔断；`ChatService` 中工具分派异常只记 warning，调用方不可见；缺 SMTP 配置时预警不会真正发信，只记一条 FAILED 预警并列出缺失配置项，聊天功能不受影响。

---

## 六、复现命令

```bash
# 1) 单轮多 Agent 协作演示：看任务被谁认领、产出了哪些产物、最终采纳哪一版
python scripts/demo_turn.py

# 2) 6 组工程 Harness：mock 模型 / 临时 SQLite / 内存记忆 / 关闭向量库
python -m app.harness.runner

# 3) 单元与回归测试：管理路由 200/403/404、策略门闸与审计、硬拦不调模型
python -m unittest discover -s tests

# 4) 缺依赖环境的离线验证：33 项断言，覆盖认领写回、策略审计与 60 条 RAG 评测
python scripts/verify_offline.py
```

命令细节与实测数据见 [QUALITY](./QUALITY.md) 与 [BENCHMARKS](./BENCHMARKS.md)。

---

## 七、设计原则小结

1. **控制流可审计**：协调者不调用 Agent，Agent 不互相调用，所有协作事实落在黑板与事件流里。
2. **安全不依赖模型可用性**：高危判定有词典硬拦，回复必须由第三方角色审查当前版本。
3. **降级显式、不静默**：检索、记忆、工具队列都有明确降级路径，并通过状态接口或审计记录暴露。
4. **可复现优先**：确定性的检索公式、mock 模型与临时 SQLite，让关键行为能在无 Key 环境回归。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：运行时、Harness 与数据模型
- [SAFETY](./SAFETY.md)：风险判定、版本绑定审查与隐私边界
- [RAG](./RAG.md)：混合检索与三层降级
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：记忆压缩与 Skill 选择
- [API](./API.md)：认证、路由与 SSE
- [QUALITY](./QUALITY.md)：测试策略与缺陷审计
- [BENCHMARKS](./BENCHMARKS.md)：实测数据与口径
- [LIMITATIONS](./LIMITATIONS.md)：边界与演进路线
- [DEVELOPMENT](./DEVELOPMENT.md)：时间线、环境与本地运行
