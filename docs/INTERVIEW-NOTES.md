# MindBridge 面试准备笔记

> 本文是仓库内部的技术自述，用于回答"这个项目是怎么做的、为什么这么做、边界在哪里"这三类问题。
> 所有数字、路径、行为描述均以仓库内代码与 `README.md` 为准；需要现场取数的指标一律给出可复现命令，不在此处复述数值。
> 项目定位是**可运行、可审计、可复现的心理场景多 Agent 后端**，不是生产系统，本文不会声称生产可用性。

---

## 一、30 秒项目介绍

### 口语版（约 200 字）

"MindBridge 是一个面向高校心理支持场景的 Python / FastAPI 后端。它没有套现成的 Agent 框架，而是自己实现了一套基于任务认领的多 Agent 运行时：协调者只维护任务板、不直接指挥 Agent，理解、安全、上下文、回复四个专业 Agent 按自己声明的能力去认领任务，把结果作为产物发布到共享黑板，最后由协调者按五道条件统一验收。安全上是三级判定，最前面是词典硬拦，高危时安全 Agent 会审查这一版回复，不通过就打回重做。检索是路由式 RAG，带三层降级。工具走异步队列，有限流、重试、死信和审计。每一步都落 trace，出事能回放。"

### 书面版（三句话）

1. MindBridge 是面向高校心理支持场景的 Python/FastAPI 后端，用共享黑板 + 任务认领的协作模型自建多 Agent 运行时：协调者只负责派生任务、排序认领、验收产物，Agent 之间不直接互相调用。
2. 它在链路前端放了三级风险判定（词典硬拦 / 严格 JSON 模型判定 / 启发式兜底）作为安全门闸，在链路后端对"当前这一版回复"做安全审查与五条件验收，保证高危场景既不会漏判、也不会把未审回复发给学生。
3. 检索是可降级的混合检索、工具走带限流重试死信的异步队列、每一步协作都落成结构化 trace，并用六组工程 Harness 在无外部依赖的条件下端到端复现这些行为。

---

## 二、技术栈与规模速查

| 维度 | 数值 / 内容 |
| --- | --- |
| 语言与框架 | Python 3.12、FastAPI 0.115、uvicorn、SQLAlchemy 2.0 ORM、pydantic-settings |
| ORM 数据表 | 13 张 |
| HTTP 路由 | 20 条 |
| MCP 工具 | 6 个（`mindbridge_excel_report` / `mindbridge_case_create` / `mindbridge_alert_send` / `mindbridge_alert_ack` / `mindbridge_case_note_add` / `mindbridge_alert_notify`） |
| 标准 Skill | 7 个（`skills/*/SKILL.md`） |
| 内置知识文档 | 11 篇 |
| 知识切块 | 34 个 chunk，切块参数 512 / 64 |
| RAG 评测集 | 60 条 |
| Agent 角色 | 5 个（1 个协调 + 4 个专业：Understanding / Safety / Context / Response） |
| 协作事件类型 | 13 种 |
| 产物类型 | 5 种（intent / risk / context / response_proposal / safety_review） |
| 工程 Harness | 6 组（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue） |
| Runtime 预算 | `agent_max_rounds=8`、`agent_max_claims_per_round=4`、`agent_max_claims_per_agent=3` |
| 验收置信度门槛 | `agent_final_acceptance_min_confidence=0.6` |
| 词典硬拦置信度 | 0.95（且不调用模型） |
| 检索权重 | 向量 0.65 / BM25 0.35；BM25 `k1=1.5`、`b=0.75`；Embedding 批量 20 |
| 重排公式权重 | `base*0.55 + lexical*0.25 + query coverage*0.15 + phrase*0.05` |
| 记忆 | 短窗口 40 条消息、TTL 86400s；模型历史 `chat_history_limit*2 = 20` 条；摘要上限 500 字 |
| 工具队列 | 最多 3 次尝试；线性退避 15 / 30 / 45 秒；邮件限流 30 封/分钟（60 秒滑窗） |

### 端到端链路速查

| 步骤 | 位置 | 说明 |
| --- | --- | --- |
| 1. 入口 | `POST /api/chat/stream` | 路由本身是 async |
| 2. 服务层 | `ChatService.stream_chat`（`app/services/chat.py`） | 组织一次对话调用 |
| 3. Harness | `MindBridgeAgentHarness.run`（`app/agents/harness.py`） | 脱敏、会话、runtime、落库、报告、trace、工具计划 |
| 4. 运行时 | `create_agent_runtime` → `EventDrivenAgentRuntimeService.run`（`app/agents/event_driven_runtime.py`） | 装配运行时 |
| 5. 协调 | `EventDrivenCoordinator.run`（`app/agents/coordinator.py`） | 派生任务、排序认领、调用 `act()`、合并结果 |
| 6. 执行 | 各 Agent 的 `decide()` / `act()` | 发布 intent / risk / context / response_proposal / safety_review 产物 |
| 7. 结果 | `AgentRunResult` → `AgentTraceService.save_run`（`app/services/trace.py`） | 落结构化 trace |
| 8. 输出 | `AiClient.stream`（`app/services/ai.py`）SSE token 流 | 链路上唯一被 await 的环节 |
| 9. 收尾 | `dispatch_tools` | 流结束后构建 `AgentToolPlan` 并派发工具 |

诚实说明：整条流水线是同步的（同步 httpx、同步 SQLAlchemy），只有第 8 步的 token 流式输出是 async 的。

### Agent 角色与置信度速查

| Agent | 能力 | 产出 | 置信度取值 | 是否认领任务 |
| --- | --- | --- | --- | --- |
| CoordinatorAgent | 调度 | 无（不产出产物） | - | 否，`decide()` 恒为 False |
| UnderstandingAgent | 理解意图与主题 | intent | 0.78 / 0.92 | 是 |
| SafetyAgent | 风险判定 + 回复安全审查 | risk、safety_review | 0.84 / 0.95 / 0.98 | 是 |
| ContextAgent | 记忆 + RAG + Skill | context | 0.82 / 0.86 / 0.88 | 是 |
| ResponseAgent | 生成回复方案 | response_proposal | 0.78 / 0.84 | 是 |

每个 Agent 都有独立的 `AgentProfile`（system_prompt、memory_policy、model_profile、tool_permissions）；`AgentModelRegistry` 允许按角色指定不同 provider/model（如理解用便宜模型、安全判定用严格模型），通过 `AGENT_MODEL_*_PROVIDER` / `AGENT_MODEL_*_MODEL` 环境变量配置。

### 检索与工具策略速查

RAG 流水线（仅在意图为 CONSULT/RISK 或风险为中/高时执行）：

1. LLM 查询改写，截断到 60 字；
2. Chroma 向量召回（余弦，`score = 1/(1+distance)`）+ 自实现 BM25（`k1=1.5`、`b=0.75`，分词混合英文词、单个汉字与汉字二元组）；
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

## 三、核心概念一句话解释

| 概念 | 一句话解释 | 代码位置 |
| --- | --- | --- |
| claim-based runtime | 协调者不派活，只把待办拆成任务放到板上，Agent 按能力和优先级自己认领，每轮每个 Agent 最多认领一个任务 | `app/agents/coordinator.py` |
| 共享黑板 | 全流程唯一的协作状态载体，任务、消息、产物、事件都存在里面，Agent 不互相调用只读写黑板 | `app/agents/events.py` |
| append-only 不可变状态 | 黑板是 frozen dataclass，所有更新都走 `dataclasses.replace` 重建新实例，旧版本不会被就地改写 | `app/agents/events.py` |
| budget | 对轮数和认领次数的硬上限；轮次之间出现空档即发出 `BUDGET_EXHAUSTED`，强制收敛 | `app/agents/coordinator.py` |
| SAFETY_OVERRIDE | 安全 Agent 判定 `risk == HIGH` 时发出的信号，被四处消费：改意图、改风险等级、抬任务优先级、抬自主运行风险级别 | `app/agents/events.py`、`app/agents/coordinator.py`、`app/agents/autonomous.py` |
| acceptance gate | 最终采纳必须同时满足五条件：有回复提案、有安全审查、审查对象是当前这版提案、审查通过、置信度达标 | `app/agents/coordinator.py` |
| artifact（产物） | Agent 唯一被承认的输出形态，共 5 种，带 id / 类型 / 内容 / 置信度 / 元数据 | `app/agents/events.py` |
| Harness 分层 | Runtime 只管协作，脱敏/落库/trace/工具计划这些业务与合规规则放在外层 Harness，两层可独立演进 | `app/agents/harness.py` |
| 路由式 RAG | 不是每轮都检索，只有意图属于咨询/风险或风险等级达到中/高才走检索 | `app/agents/harness.py`、`app/services/knowledge.py` |
| 混合检索 | 向量召回与自实现 BM25 各出一份结果，按来源做 min-max 归一化后加权融合 | `app/services/knowledge.py` |
| 降级 | 三层可降级设计：构建期缺 Key 关向量、显式要求时改为报错、检索期异常退回纯 BM25 | `app/services/vector_store.py`、`app/services/knowledge.py` |
| 记忆分层 | 全量历史在 MySQL、最近 40 条热窗口在 Redis List、每个 Agent 还有自己的私有记忆命名空间 | `app/services/memory.py` |
| 上下文压缩 | 历史不超过 8 条直接用；超过则发"摘要消息 + 最近 8 条"，摘要确定性生成且限长 500 字 | `app/services/memory.py` |
| Skill 加载校验 | 自解析扁平 YAML 前置元数据，并校验目录名、必需小节、描述长度、模板块，状态在状态接口上暴露 | `app/services/skills.py` |
| 三级风险判定 | 词典硬拦（不调用模型）→ 严格 JSON 模型判定（含一致性纠正）→ 模型失败时的启发式兜底 | `app/services/assessment.py` |
| 工具策略门闸 | 每个工具声明允许的风险范围，执行前必须过 `require_allowed()`，每次执行都写审计记录 | `app/services/tool_governance.py`、`app/services/tool_queue.py` |
| 死信队列 | 重试到上限仍失败的作业转为 DEAD，并把类型、原因、载荷记入死信表供人工处理 | `app/services/tool_queue.py` |
| 埋点 trace | 每次运行把意图、风险、输入、记忆摘要、Agent 步骤、检索结果、回复、评估等落成结构化记录 | `app/services/trace.py` |

---

## 四、高频问题与回答要点

### Q1. 为什么不用 LangChain / LangGraph？

要点：

- 这个项目的核心诉求是**控制流可解释、可审计**，而不是快速拼装。现成框架会对"谁在什么时候能做什么"做一层封装，出问题时排查要穿过框架内部；自建 runtime 的每一行调度逻辑都在 `app/agents/coordinator.py` 里，能逐行讲清。
- 安全要求反向约束了架构：词典硬拦必须在任何模型调用**之前**发生（有单测用会抛异常的 AI stub 证明高危词命中时模型确实没被调用），这要求对调用顺序有完全控制权。
- 每一步协作都要落成领域事件与产物，最终写进 `agent_run_traces`。自建的黑板与事件模型可以直接是领域模型，不需要再做一层"框架概念到业务概念"的翻译。
- 代价要说清楚：自建意味着没有现成的持久化执行、断点续跑、分布式调度和生态集成，这些在本项目里目前是靠单进程顺序执行和启动时复位兜底的（见第五节"并发与多 worker"）。所以这不是"框架不好"，而是"当前需求下把复杂度换成了可解释性和可测试性"。

### Q2. 为什么不用固定 workflow？

要点：

- 固定 workflow 的问题是**参与角色和执行顺序在编码期就定死了**。这个项目里"这一轮需要跑哪些 Agent"本质上取决于输入：普通聊天不会触发 ContextAgent（Harness 里有断言），高危输入则必然跑满全集。
- 用任务板做运行时派生：协调者根据当前黑板状态派生缺失的工作（understand、assess-safety、gather-context、propose-response、review-response、revise-response），再由 Agent 认领，因此执行集合是输入驱动的，而不是分支驱动的。
- 副产品是可扩展性：新增一个能力只需要一个新 Agent 在 profile 里声明能力，加上它认领的任务类型，不需要改一条主流程的 if/else 链。
- 代价：链路不再是"看一眼代码就知道这次跑了什么"，必须看 trace 才能确定实际参与集合；调试难度和不确定性都更高，这也是把 Harness 断言（高危必跑全集、普通聊天不检索）做成自动化测试的原因。

### Q3. 黑板为什么用不可变数据结构？

要点：

- `CollaborationBlackboard` 是 frozen dataclass，字段是 `tasks` dict、`messages` / `artifacts` / `events` 三个 tuple 与 `final_artifact_id`，所有写入都通过 `dataclasses.replace` 产生新实例。
- 直接好处是**没有就地修改**：任何一次任务状态变更、产物发布、事件追加都是整块重建，读者拿到的那一份永远不会在自己读取过程中被改掉，顺序执行的调度循环因此不需要任何锁去防"读到半个状态"。
- 第二重好处是审计语义：状态演进是一串快照，事后的 trace 与事件序列能对上，出问题可以按事件回放而不是猜。
- 代价必须承认：整块重建对大数据量不划算，而且这只解决**单进程内**的可见性问题。多 worker 部署时，跨进程的并发协调（比如队列 RUNNING 任务抢占）并没有被这个设计覆盖，需要额外的分布式协调（见第五节）。

### Q4. 怎么防止 Agent 无限循环？

要点：

- 三层约束，一层比一层硬：
  1. **轮数上限**：`agent_max_rounds=8`，整个协作最多跑 8 轮；轮与轮之间如果出现需求空档，直接发 `BUDGET_EXHAUSTED`，不再空转。
  2. **认领上限**：`agent_max_claims_per_round=4`（单轮总认领数）与 `agent_max_claims_per_agent=3`（单个 Agent 的总认领数），防止某个 Agent 反复接活。
  3. **收敛条件**：最终采纳必须满足五条件，其中"有回复提案 + 有指向当前版本的安全审查 + 审查通过 + 置信度 ≥ 0.6"这几条本身就是终止判据；协调者不会为了"再想一轮"而继续。
- 另外，协调者自身 `decide()` 恒为 False，它永远不认领任务、不产出产物，所以不存在"协调者把自己的任务再拆一遍"的自激循环。
- 需要诚实的地方：预算是**硬截断**而不是优雅收敛。预算耗尽时可能拿不到最终产物，这类运行在 trace 里表现为没有最终采纳产物，属于已知行为，而不是异常。

### Q5. 怎么保证安全审查不会被绕过？

要点：

- 关键设计是"**采纳权不在回复方手里**"。ResponseAgent 只能发布 `response_proposal` 产物，它没有能力把回复标记为最终结果；只有协调者的 `_try_accept_final` 能设置 `final_artifact_id`。
- 门闸是五条件同时成立：存在回复提案；存在安全审查；审查的 `metadata.responseArtifactId` 等于当前提案 id；`review.approved` 为真；提案置信度 ≥ 0.6。任何一条不成立就不采纳。
- 高危时 SAFETY_OVERRIDE 会被四种消费者分别消费：强制意图为 RISK、强制风险等级为 HIGH、把上下文与回复任务优先级抬到 CRITICAL、抬升自主运行的风险级别。也就是说高危既提高了"必须审"的优先级，也提高了"必须优先做"的调度优先级。
- 还有一条端到端断言兜底：风险安全 harness 会检查真实 SSE 输出中不包含 `["风险等级","报告ID","emotionScore","HIGH_RISK"]`，防止后端元数据泄漏给学生。
- 边界：审查本身是**基于要素检查的规则审查**，不是回复安全分类器。它能稳定拦住"缺少即时安全要素"的回复，但不等价于语义层面的安全判别，这是明确的局限。

### Q6. 为什么要审查"当前这一版"回复？

要点：

- 因为审查和回复之间会插入**修订循环**。高危回复如果缺少即时安全要素，安全 Agent 会发布批评意见并发出 `REVISION_REQUESTED`，协调者据此派生 `revise-response:{id}` 任务，ResponseAgent 重做一版。
- 如果门闸只检查"存在一次通过审查"，那么第一版被审、第二版被改坏，仍然能通过。所以验收条件写的是 `safety_review.metadata.responseArtifactId == response_proposal.id`：审查必须指向**即将被采纳的那一版**。
- 换句话说，修订会让旧的审查自动失效，必须重新审查新版本。这是"审查对象可追溯"而不是"审查动作发生过"。
- 代价是成本：每次修订都要再走一轮安全审查，高危路径的延迟和模型调用次数都会增加。这里选择了安全优先。

### Q7. 为什么高危必须保留词典硬拦？

要点：

- 直接原因是失败模式不对称：在危机场景里，"模型不可用"绝不能被解释成"没有风险"。如果风险判定完全依赖模型，那么超时、限流、返回格式错误这些工程故障会直接转化为**漏判**，后果不可接受。
- 所以链路最前面是硬词典：命中 `HIGH_RISK_WORDS` 立即判定 HIGH、置信度 0.95，并且完全不调用模型。这一点有单测证明——测试用一个"一旦被调用就抛异常"的 AI stub，验证命中高危词时调用路径根本没走到模型。
- 后面的两级是补充而不是替代：严格 JSON 的模型判定负责开放式表达，并做一致性纠正（`emotionScore >= 4` 强制 HIGH、`>= 3` 强制 MEDIUM、模型自报高风险情绪强制 HIGH）；第三级是模型失败时的启发式兜底。
- 代价很明确，也不掩饰：词典是子串匹配、**没有否定检测**，"我不想自杀"会被判成 HIGH。方向上是"宁可多干预"，但仍会产生不必要的干预，这是当前已知缺陷，改进方向是引入否定作用域识别。

### Q8. 为什么重排不用模型？

要点：

- 重排是确定性公式：`base*0.55 + lexical*0.25 + query coverage*0.15 + phrase*0.05`，输入是融合后的分数、词法重叠、查询覆盖率与短语命中。它不依赖任何额外模型。
- 理由有三条：一是**规模**，知识库只有 34 个 chunk（11 篇文档按 512/64 切块），几十到几百量级的候选用一个大模型做重排，收益远小于部署成本与延迟；二是**可解释**，每一分的来源都能拆开看；三是**可回归**，确定性公式能在评测集上稳定复现，不会因为重排模型换版本导致分数漂移——在安全敏感域里这比多挣一点 NDCG 重要。
- 代价：公式无法理解语义相似但用词完全不同的表述，也无法做真正的相关性建模。当知识库规模或语言复杂度上升时，这一步是需要替换的。仓库的局限章节把"重排是确定性公式而非学习型模型"列为已知边界。

### Q9. 向量库挂了怎么办？

要点：

- 降级是分层的，共三层，分别处理不同的失败时机：
  1. **构建期软降级**：缺少 API Key 或 chromadb 不可用，直接 `can_embed=False`，系统以纯词法方式继续工作。
  2. **构建期硬失败（可选）**：配置 `KNOWLEDGE_VECTOR_REQUIRED=true` 时，同样的条件下改为直接抛错而不是降级——用于"我希望部署时就知道检索能力不完整"的场景。
  3. **检索期降级**：embedding 或 chroma 在查询时抛异常，记 warning 后回退到纯 BM25；同样，处于 required 模式时改为抛错。
- 融合阶段还有一个细节：向量权重虽然默认 0.65，但当向量召回为空时会被强制置 0，避免"没有向量结果却仍按 0.65 权重拉低词法命中"的错位。
- Embedding 通过 OpenAI 兼容的 `/embeddings` 接口批量调用（每批 20 条），并且 `knowledge_chunks.embedding_json` 会缓存向量，只重算缺失的部分，所以临时故障恢复后不需要全量重建。
- Chroma 是持久化集合，每次 upsert 同时用快照目录留档（保留最近 5 份），便于回滚到上一个可用状态。
- 需要说清的局限：harness 环境里向量检索是关闭的（强制 `KNOWLEDGE_VECTOR_ENABLED=false`），所以**harness 里跑出的检索分数实际上是 BM25 路径的分数**，不能当作完整混合检索的成绩单。

### Q10. 为什么压缩摘要要声明"不要展示给学生"？

要点：

- 触发条件是历史长度超过 8 条，此时发送 `[system 摘要消息, 最近 8 条消息]`。摘要是确定性生成的：最近 4 条用户消息各取 80 字 + 最近 3 条助手消息各取 70 字 + 当前输入 80 字，总长上限 500 字。
- 问题在于：**被压缩掉的恰恰可能是风险信息**。摘要里出现"学生提到自伤念头"这类内容是合理的内部上下文，但它绝不能作为一条 assistant/系统消息被回显给学生。
- 所以摘要消息带一个显式的范围声明：仅供内部上下文使用、不要展示给学生、不要输出诊断或风险等级、不要暴露后端标签。这既是提示词层面的约束，也是把"这条消息属于什么范围"显式写进上下文，便于后续审查。
- 同一原则在链路其它地方也一致执行：CHAT 分支的提示模板禁止输出后端元数据；Harness 断言真实 SSE 输出不含风险等级、报告 ID、emotionScore 等字段。
- 局限：这是**靠声明与断言约束**，不是靠输出侧的结构化过滤。若要更硬，应该在出口做字段级白名单，这一点属于改进方向。

### Q11. 工具为什么走队列而不是同步调用？

要点：

- 有两条执行路径，但共用同一份实现（`ToolOrchestrationService`）：默认是数据库支撑的异步队列，配置 `TOOL_QUEUE_ENABLED=false` 时切换为直接的 MCP stdio 客户端。
- 选这套结构的理由很直接：**演示路径和生产路径不能行为分叉**。如果 demo 直接同步调用、线上走队列，那么限流、重试、依赖、审计这些逻辑在 demo 里根本不执行，等于没被验证。共用实现后，两条路径的差异只在于"谁来触发"。
- 队列带来的能力是同步调用给不了的：限流（60 秒滑窗、默认 30 封/分钟，超限返回 `retry_after` 并重新入队）、重试（最多 3 次、线性退避 15/30/45 秒）、失败转死信、作业依赖、重启恢复（启动时把残留 RUNNING 重置为 PENDING）、以及每次执行都写工具审计记录。
- 代价：引入了最终一致性与运维面——作业可能延迟执行、可能进死信需要人工看，调用方不能立刻拿到结果，需要额外的状态查询。对于"发预警"这种可以异步的动作这很划算，对于需要即时反馈的动作就不合适。

### Q12. 怎么保证预警不会在个案创建前发出？

要点：

- 靠**作业依赖 + 执行时校验**，而不是靠入队顺序。高危报告入队三个作业：`EXCEL_REPORT` 恒发；`CASE_CREATE` 仅在中/高风险；`ALERT_SEND` 仅在 HIGH，且它的 `depends_on_job_id` 指向对应的 CASE_CREATE 作业。
- 关键点是依赖在**执行那一刻**检查，而不是在入队时按顺序假定。因此即使队列乱序、即使 CASE_CREATE 先失败重试，预警也不会提前发出——它只会等，或者最终失败。
- 这条路径在工程 Harness 的 Tool Queue 组里被断言：一份 HIGH 报告恰好产生 3 个作业，并且预警作业的依赖关系成立。
- 还要说明一个刻意的设计：AlertRecord 的去重发生在 log/smtp 分支之前，所以重复通知**按设计不会重复发送**。对于"同一条风险事件反复告警"的场景这是防止打扰，但如果需求是"每次状态变化都要通知"，就需要调整去重键，这是已知的取舍而不是 bug。

### Q13. 怎么测试这种不确定的 Agent 系统？

要点：

- 思路是"**把不确定性关在边界外，把确定性行为写成断言**"。`python -m app.harness.runner` 跑 6 组 suite。运行时用 mock AI 替换模型调用、临时 SQLite 建库、内存实现替换 `RedisShortTermMemoryStore`，并强制 `KNOWLEDGE_VECTOR_ENABLED=false`，因此整组测试不需要任何外部服务或 API Key。
- 每组 suite 独立 drop/recreate schema 并重新播种数据，避免依赖上一组的残留状态；只要有一组失败，runner 返回非零退出码，可以直接接 CI。
- 断言的是行为契约而不是模型输出文本。各组覆盖的重点：

| Suite | 断言要点 |
| --- | --- |
| Risk Safety | 三级判定行为与高危优先干预路径；真实 SSE 输出不含 `["风险等级","报告ID","emotionScore","HIGH_RISK"]` 中的任何一项（"高危词命中时不调用模型"另由一条使用会抛异常的 AI stub 的单元测试证明） |
| Agent Routing | 普通聊天不运行 ContextAgent、不触发检索；高危必然跑满全集 |
| Standard Skills | Skill 的加载、校验与选择链路（具体断言见 `app/harness/runner.py`） |
| RAG | 评测集至少 50 条，命中率 ≥ 0.95、Recall@K ≥ 0.95、MRR ≥ 0.75、NDCG ≥ 0.75 |
| API | 学生读管理路由返回 403，管理员发起聊天返回 403 |
| Tool Queue | HIGH 报告恰好 3 个作业、预警依赖关系成立、Excel 与个案写入幂等、限流器拒绝第二次请求、达到最大尝试次数的作业转 DEAD 且有死信记录 |

- 另外补了针对性的回归测试：`tests/test_admin_api_routes.py` 用 TestClient 真实请求三条管理路由并覆盖 403/404 分支；`tests/test_tool_governance_audit.py` 覆盖策略拦截与审计写入；安全侧用"会抛异常的 AI stub"证明高危词命中时不调用模型。
- 诚实的边界：这 6 组 suite 是**契约级**测试，不衡量回答质量；回答质量依赖 60 条 RAG 评测集，而评测集本身覆盖面有限（见第五节）。此外没有覆盖率门禁、没有 lint/type check 配置，harness 通过不等于全仓无回归。

### Q14. 你在这个项目里遇到最难查的 bug 是什么？

要点（两个故事，都讲清根因、为什么 CI 没发现、怎么修、怎么防复发）：

**故事一：三个管理接口必然 500，而 CI 一直是绿的。**

- 现象：`GET /api/admin/agent-traces`、`GET /api/admin/tool-audits`、`GET /api/admin/conversations/{id}` 三条路由永远返回 500。管理端前端正好调用了其中之一，所以后台"查看会话"功能完全不可用。
- 根因：不是逻辑错，是**缩进错误**——`app/services/report.py` 里 `agent_run_traces()`、`tool_audits()`、`conversation()` 三个方法被定义在了模块级，而不是定义在 `ReportService` 类内部。于是实例上没有这三个属性，调用即 `AttributeError`，被框架转成 500。
- 为什么 CI 没发现：API harness 恰好没有覆盖这三条路由，普通单测也不经过它们。请求路径本身没有任何单元测试，所以"代码能 import、能启动、能跑通被测的其它路由"，绿得毫无破绽。这也解释了为什么它长期存在：**没有测试覆盖的功能等于不存在**。
- 怎么修：把三个方法移回 `ReportService` 类内。
- 怎么防复发：新增 `tests/test_admin_api_routes.py`，用 `TestClient` 真实请求这三条路由，并顺带覆盖 403 越权与 404 分支。教训是"按路由维度补测试"——方法级测试永远发现不了"方法不在类里"这种问题，只有真的发一次请求才会暴露。

**故事二：任务认领记录从未落盘，审计语义名存实亡。**

- 现象：`AgentTask.status` 永远不会出现 `CLAIMED`，`claimed_by` 恒为空，`agent_run_traces` 里的 `claimedBy` 字段永远是 `[]`。换句话说，整个 claim-based 调度**在数据上看起来从没发生过认领**。
- 根因：`EventDrivenCoordinator.run()` 里调用了 `task.claim(agent_name)`，但**认领后的任务实例没有写回黑板**；紧跟着的 `apply_turn_result()` 用的还是那个未认领的旧实例。因为黑板是不可变结构、`claim()` 返回的是新实例，忽略返回值就等于丢弃了这次认领。
- 为什么 CI 没发现：协作流程本身跑通了、产物也产出了、回复也正常返回，功能层面"看起来完全正常"。丢的只是审计字段，而不变量重建的语义又让这个丢失非常安静——没有任何异常、没有日志。这类 bug 只有对比"数据应该长什么样"和"数据实际长什么样"才能发现。
- 怎么修：把认领结果写回黑板；同时把 `claimedBy` 记进 `TASK_CLAIMED` 事件的 metadata，让事件流也能独立还原认领事实。
- 怎么防复发：新增回归测试，同时断言黑板上的 `claimed_by` 与事件 metadata 一致——两处交叉验证，任一处再次静默失效都会被测出来。

### Q15. Coordinator 为什么不自己调用 Agent？

要点：

- 这是刻意的不对称设计。协调者只做四件事：创建根任务、根据黑板状态派生缺失工作、把认领候选按 `(priority, confidence, agent name)` 降序排序、在 Agent 执行完后调用 `apply_turn_result` 合并结果。
- 它不进 `decide()` 的认领流程（`CoordinatorAgent.decide()` 恒为 False），也不持有任何专业能力。这样"谁决定做什么"和"谁去做"被分开：调度策略的变化不会污染专业逻辑，专业 Agent 的替换也不会影响调度。
- 每个 Agent 有独立的 `AgentProfile`（system_prompt、memory_policy、model_profile、tool_permissions），配合 `AgentModelRegistry` 可以按角色指定不同 provider/model，例如理解任务用便宜模型、安全判定用更严格的模型（通过 `AGENT_MODEL_*_PROVIDER/MODEL` 环境变量配置）。
- 代价：多一层间接，链路追踪必须依赖事件与 trace 才能还原"这一轮是谁因为什么被选中"。

### Q16. 每次请求都会跑满 5 个 Agent 吗？

要点：

- 不会，**参与集合取决于输入**。认领是能力驱动的：任务上带 `required_capabilities`，只有具备该能力的 Agent 才会成为候选。
- 两个可验证的极端：普通聊天不会运行 ContextAgent（不检索、不组装上下文，Harness 里有断言），也没有报告产出（意图为 CHAT 时不生成心理报告）；高危输入则必然跑满全集，因为安全覆盖会同时抬升上下文与回复任务的优先级到 CRITICAL。
- 这也解释了为什么"Agent 数量"不是这个项目的卖点——重点不是并行度，而是"该来的角色一定会来、不该来的角色不会浪费一次模型调用"。
- 需要承认的边界：调度是**单线程顺序执行**的。任务板、优先级与认领是一套控制流语义，不代表真的有并发执行；多 worker 场景下的分布式协调尚未实现。

### Q17. 为什么把落库、报告、工具计划放在 Harness 而不是 Runtime？

要点：

- Runtime 的职责边界是"多 Agent 怎么协作"；Harness（`MindBridgeAgentHarness`）承担 7 件与协作无关的事：输入脱敏、解析或创建会话、调用 runtime、持久化消息（MySQL + Redis）、在意图不为 CHAT 时生成心理报告、保存 agent trace、以及流结束后构建 `AgentToolPlan` 并派发工具。
- 这样拆的收益是**变化频率不同的东西分开**：业务与合规规则（报告、台账、预警、trace 字段）通常跟着监管与产品需求变，协作机制则相对稳定。放在同一层里会导致任何一处合规调整都要改动协作代码。
- trace 表 `agent_run_traces` 由这一层负责写入，字段包括：

| 字段 | 内容 |
| --- | --- |
| `intent` / `risk_level` | 最终意图与风险等级 |
| `original_input` / `sanitized_input` | 原始输入与脱敏后输入 |
| `memory_brief` | 记忆摘要 |
| `agent_steps_json` | `agent_event` / `agent_task` / `agent_artifact` 三类步骤条目 |
| `retrieved_knowledge_json` | 命中的知识片段 |
| `response_messages_json` | 模型消息与服务端回复 |
| `assessment_json` | 风险判定细节 |

  需要如实说明：这张表把 `original_input` 原文也存了下来，管理员 trace 接口可以读到它，这属于过度授权，应当按最小权限原则收紧或做二次审计（见第五节）。
- 副作用是清楚的：Harness 成了链路的编排点，读代码时必须从它入手才能看清完整请求路径（`POST /api/chat/stream` → `ChatService.stream_chat` → Harness → runtime → coordinator → agents → `AgentRunResult` → trace 落库 → SSE token 流 → 工具派发）。

### Q18. 请求路径里 async 和 sync 混用，怎么解释？

要点：

- 事实是：HTTP 路由是 async 的，但 Agent 流水线内部是同步的——同步 httpx、同步 SQLAlchemy，只有最后的 token 流式输出是 await 的。
- 这么做的现实原因是最初的链路以"能一条线跑通、能同步调试"为优先，把异步化留在最后，结果最后没有做。它的直接后果是：一次聊天请求会占用一个事件循环线程，agent 流水线越长，并发能力越受限。
- 我不会把它说成设计选择，它是**已知的技术债**。改进方向是把模型调用与 DB 访问换成异步驱动（async httpx / async SQLAlchemy session），并让工具派发从"流结束后同步执行"改成提交到队列后立即返回（队列本身已经是异步的）。
- 目前它能被兜住的部分：风险判定有词典与启发式两级，模型不可用不会变成漏判；工具走队列后有重试与死信，失败可查。但模型调用本身没有重试与熔断，容量与稳定性都还是单机水平，这一点在讨论并发时应当主动承认。

---

## 五、被追问时的诚实回答

| 追问 | 你该承认的现状 | 改进方向 |
| --- | --- | --- |
| 并发与多 worker | 调度循环是单进程顺序执行，黑板无锁、不可变结构只在单进程内安全；多 worker 下种子数据与工具队列的 RUNNING 抢占都缺少分布式协调 | 引入队列 lease / visibility timeout 与跨进程协调，或把调度收敛到单一消费者 |
| 事务边界 | 每条消息独立 `commit()`，没有跨步骤事务；置 RUNNING 与提交线程池之间存在崩溃窗口，目前只靠启动时把残留 RUNNING 复位兜底 | 用显式事务边界包住一次 run 的关键写入，并为队列加 lease 与幂等键 |
| 鉴权强度 | HTTP Basic Auth + SHA-256 无盐口令哈希，无 MFA、无令牌过期、无登录限流 | 换成 OIDC/JWT，口令哈希升级为 bcrypt/argon2，补限流与会话过期 |
| 脱敏覆盖面 | `PrivacySanitizer` 只覆盖手机号、邮箱、18 位身份证号，统一替换为 `[已脱敏]`；姓名、学号、住址、银行卡、微信号未覆盖 | 扩展实体类型与规则，并在出口做字段级白名单而不是只靠正则 |
| 否定检测 | 风险词典是子串匹配，无否定作用域识别，"我不想自杀"会被判 HIGH；误报方向安全但会产生不必要干预 | 引入否定/假设/引用语境的识别层，并加一个独立的回复安全分类器 |
| 评测集覆盖面 | 60 条评测集只引用 11 篇内置文档中的 2 篇；`Recall@K` 是二值近似（有任意相关命中即 1.0），无法反映真实排序质量 | 评测集覆盖全部文档与更多意图分支，引入分级相关性标注替代二值 Recall |
| CHROMA 在评测中被关闭 | Harness 强制 `KNOWLEDGE_VECTOR_ENABLED=false`，所以 harness 产出的检索分数是 BM25（+ 本地重排）路径的分数，不代表完整混合检索的真实上限 | 增加一组带向量后端的评测运行，两组结果并列登记，明确各自适用条件 |
| trace 里存了原文 | `agent_run_traces` 存 `original_input` 原文，且管理员 trace 接口可读；这属于过度授权，应当在数据与权限两侧收紧 | 按最小权限原则限制原文读取（或只存哈希/脱敏版），并对访问做二次审计 |
| 没有迁移工具和可观测性 | 没有 Alembic，建表依赖 `create_all`；没有结构化日志、请求 ID、Prometheus 指标；无覆盖率门禁、无 lint/type check 配置 | 引入迁移工具、结构化日志与指标埋点，把覆盖率与静态检查纳入 CI |
| reranker 不是模型 | 重排是确定性公式 `base*0.55 + lexical*0.25 + coverage*0.15 + phrase*0.05`，不理解语义相似 | 知识库规模或语言复杂度上升后替换为学习型重排，并保留公式作为降级路径 |
| Claim-based 是单线程顺序执行 | "actor-style" 只体现在控制流语义上，实际是单线程顺序执行；协调者排序后逐个调用 `agent.act()`，没有真正的并发执行 | 在任务粒度上引入并发执行与结果合并顺序保证，同时保持黑板不可变语义 |
| BLOCKED / TASK_RELEASED 是死枚举 | `TaskStatus.BLOCKED` 与 `AgentEventType.TASK_RELEASED` 已定义但从未被发出；任务可以表达阻塞，但运行时不会真的把任务标记为阻塞 | 要么实现阻塞与释放的实际流转（含超时回收），要么删除这两个枚举值，避免文档与行为不一致 |

补充可承认的点（不展开成表）：

- 模型调用本身没有重试与熔断。
- `ChatService` 中工具分派异常只记 warning，调用方不可见。
- 六个 MCP 工具虽然都注册了，但缺 SMTP 配置时预警不会真正发信，只会记一条 FAILED 预警并列出缺失的配置项——聊天功能不受影响。

---

## 六、可现场演示的 3 个命令

1. `python -m scripts.demo_turn`
   演示单轮对话的完整协作过程：能看到黑板上的任务被谁认领、产出了哪些产物、安全审查是否通过、最终采纳的是哪一版回复。
2. `python -m app.harness.runner`
   一次性跑完 6 组工程 Harness（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue），全程 mock 模型、临时 SQLite、内存记忆、关闭向量库，任意一组失败即返回非零退出码。
3. `python -m unittest discover -s tests`
   跑单元与回归测试，包括三条管理路由的 403/404/200 分支、工具策略门闸与审计写入、以及"高危词命中时不调用模型"的证明。

---

## 七、简历描述与项目内证据对照表

| 简历表述 | 代码证据 | 可以现场打开的位置 |
| --- | --- | --- |
| 设计并实现 `MindBridgeAgentHarness`，统一处理输入脱敏、会话解析、消息落库（MySQL + Redis）、心理报告生成、trace 保存与工具计划派发 | Harness 承担 7 项职责，runtime 只负责协作；链路为 `POST /api/chat/stream` → `ChatService.stream_chat` → `MindBridgeAgentHarness.run` → runtime → coordinator → agents → `AgentRunResult` → trace 落库 → SSE token → 工具派发 | `app/agents/harness.py`、`app/services/chat.py` |
| 基于 `CollaborationBlackboard` 的事件驱动协作：Coordinator 维护任务板，多 Agent 按能力 claim 并发布 artifact，SafetyAgent 审查后由 Coordinator 最终采纳 | 黑板为 frozen dataclass + `dataclasses.replace`；任务带 `required_capabilities`；协调者按 `(priority, confidence, agent name)` 排序认领候选，每 Agent 每轮一次认领；`_try_accept_final` 五条件门闸 | `app/agents/events.py`、`app/agents/coordinator.py` |
| 六大核心链路的一键工程 harness（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue） | `app/harness/runner.py` 跑 6 组 suite，mock AI、临时 SQLite、内存短期记忆、强制关闭向量库；每组独立重建并重播种，失败返回非零 | `app/harness/runner.py` |
| 分层记忆与上下文压缩：Redis 短窗口 + MySQL 回填 + 摘要压缩 + 有界模型历史 | 短期窗口为 Redis List（最近 40 条，TTL 86400s，写入前脱敏）；Redis miss 时从 MySQL 按时间回填；历史 ≤ 8 条直用，否则 `[摘要消息, 最近 8 条]`，摘要限长 500 字；模型历史限制为 `chat_history_limit*2 = 20` 条并保留首条 system 消息 | `app/services/memory.py` |
| 通过 `SkillRegistry` 动态加载 `SKILL.md`，高风险强制叠加安全 skill | 自解析扁平 YAML 前置元数据（不依赖 PyYAML）；`validation_issues()` 校验目录名、`## Workflow`、描述长度、`counselor_handoff_summary` 模板；状态经 `/api/agent/status` 与 `/api/admin/knowledge/status` 暴露；HIGH 风险固定选择 `[supportive_response_baseline, high_risk_safety_plan]` | `app/services/skills.py`、`skills/*/SKILL.md` |
| 封装 MCP 工具与异步工具队列（限流、重试、死信） | 6 个 MCP 工具；队列与直连共享 `ToolOrchestrationService`；重试最多 3 次、退避 15/30/45 秒、超限转 DEAD 并写 `dead_letter_records`；60 秒滑窗限流 30 封/分钟并返回 `retry_after`；启动时复位残留 RUNNING；`ToolPolicyRegistry` 按风险范围授权并写 `tool_audit_records` | `app/services/tool_queue.py`、`app/services/tool_governance.py`、`app/mcp_tools/server.py` |
| RAG 混合检索与评测指标 | 路由式检索（仅 CONSULT/RISK 意图或中/高风险触发）；LLM 查询改写（截断 60 字）→ 向量召回 + 自实现 BM25 → 按来源 min-max 归一化 → 加权融合（向量 0.65 / BM25 0.35）→ 确定性本地重排 → top-1 同源邻居扩展；指标为 HitRate@K、Recall@K、Precision@K、MRR、NDCG@K | `app/services/knowledge.py`、`app/services/vector_store.py`、`app/rag_eval/runner.py` |

> RAG 指标说明：本表不写具体数值。指标由 `python -m app.harness.runner --suite rag` 现场产出，README §12（实测数据）记录实测值。另需说明，harness 环境关闭了向量检索，因此该组分数对应 BM25 + 本地重排路径。

---

## 八、一句话收尾

这个项目真正想证明的不是"我用了多少 Agent"，而是"在一个出错代价很高的场景里，我怎么让系统的每一步都可解释、可追溯、可降级"——风险判定有词典兜底、回复必须由第三方审查当前版本、预警不会早于个案创建、每一步协作都落成结构化 trace，而它现在做不到什么，我也能一条条说清楚。
