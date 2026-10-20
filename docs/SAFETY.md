# 安全工程

本文覆盖 MindBridge 的风险判定链路、版本绑定的安全审查与 revise 循环、学生可见内容的元数据隔离、输入脱敏范围、工具风险策略与 `SAFETY_OVERRIDE` 的消费点；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md) 的 Agent Runtime 一节。所有参数以 `app/services/assessment.py`、`app/services/ai.py`、`app/services/tool_governance.py` 等源码为准。

![MindBridge claim-based 多 Agent Runtime：共享黑板、控制面与四个 worker](./agent-runtime.svg)

*图 1：安全门闸在 runtime 中的位置。SafetyAgent 独立发布 `risk` / `safety_review` / `critique` 产物，协调者只有在五条件同时满足时才设置 `final_artifact_id`；高危时 `SAFETY_OVERRIDE` 会把本轮全局升级。*

---

## 1. 三级风险判定

`PsychologicalAssessmentService.assess()`（`app/services/assessment.py`）按固定顺序执行三级判定：

| 级别 | 机制 | 行为 | 失败/边界 |
| --- | --- | --- | --- |
| 第一级 | 硬规则词典 | 命中 `HIGH_RISK_WORDS` → 立即返回 `risk=HIGH`、`emotion=HIGH_RISK`、`emotionScore=4.0`、`confidence=0.95`，**完全不调用模型** | 子串匹配，无否定检测 |
| 第二级 | LLM 结构化判定 | 严格 JSON：`{emotion, emotionScore, risk, confidence, summary}`，解析后做分数-等级一致性校正 | 超时、限流、非法 JSON 都会落入第三级 |
| 第三级 | 启发式兜底 | 模型异常时按词面信号给保守结论，绝不放行 | 咨询信号 → LOW/MEDIUM；无信号 → LOW |

`HIGH_RISK_WORDS` 当前词表：`自杀`、`自残`、`不想活`、`结束生命`、`伤害自己`、`轻生`、`suicide`、`kill myself`、`self harm`；`CONSULT_WORDS` 覆盖焦虑、抑郁、压力、失眠、难过、崩溃、痛苦、无助、心理、咨询及若干英文词。分数-等级一致性校正同样覆盖模型给出的情绪标签（`emotion == HIGH_RISK` 强制 `risk = HIGH`）；词典硬拦命中时直接返回，不进入模型调用与校正流程。

**为什么第一级必须存在**：模型可能超时、限流或返回非法 JSON。在危机场景里，「模型不可用」不能等于「没有风险」。词典硬拦截保证即使整条模型链路不可用，明确的高风险表达仍会被判为 HIGH 并触发后续流程。这一点有单元测试固化：测试用具「一调用即抛异常」的 AI stub 证明高危词命中时模型调用路径没有被走到。

### 分数-等级一致性校正

LLM 输出可能自相矛盾，因此解析后会做两级校正（`risk_from_score` 取更高等级）：

```text
emotionScore >= 4  → risk 至少 HIGH
emotionScore >= 3  → risk 至少 MEDIUM
emotion == HIGH_RISK → risk 强制 HIGH
```

第三级兜底的置信度为 0.66 / 0.72 / 0.75（无信号/焦虑/低落），低于词典硬拦的 0.95，属于可观测的保守信号而非硬结论。

---

## 2. 安全审查与 revise 循环

`SafetyAgent._review_response()` 审查的是**候选回复方案**（`response_proposal` 的 prompt messages），不是最终文本。审查要点：

- 高风险场景要求候选回复包含即时安全指引要素：当前安全、可信任的人、紧急资源等（实现中以 `高风险处理规则` / `当前安全` / `可信任的人` / `紧急` 等要素词判断）；
- 通过时发布 `safety_review` 产物，`payload.approved=true`，并带 `metadata.responseArtifactId` 指向被审查的提案；
- 不通过时发布 `critique` 产物（`approved=false`）、发出 `REVISION_REQUESTED` 事件，并派生 `task:revise-response:{id}` 任务；
- `ResponseAgent` 重做一版新提案后，旧审查因 `responseArtifactId` 不再匹配而自动失效，必须重新审查。

这条 revise 循环的代价是：高危路径每次修订都会多一轮安全审查，延迟和模型调用次数增加。这里选择安全优先，并把「审查针对哪一版」写进产物元数据，保证每一次否决都有原因和对应版本可追溯。

**审查是规则级而不是语义分类**：设计目标是拦住「缺少即时安全要素」的回复；实现上以要素词判断，接近格式检查而不等价于理解回复语义的安全判别，这是明确的实现边界（见 [LIMITATIONS](./LIMITATIONS.md)）。

---

## 3. 五条件采纳门闸

最终采纳由协调者的 `_try_accept_final()` 判断，要求五条同时成立：

```text
1. 存在 response_proposal
2. 存在 safety_review
3. safety_review.metadata.responseArtifactId == 当前 response_proposal.id
4. safety_review.payload.approved is True
5. response_proposal.confidence >= AGENT_FINAL_ACCEPTANCE_MIN_CONFIDENCE(0.6)
```

关键设计是**采纳权不在回复方手里**：`ResponseAgent` 只能发布提案，没有能力把回复标记为最终结果；只有协调者能设置 `final_artifact_id`。第 3 条把审查绑定到「即将被采纳的那一版」，防止「审查了旧版本、采纳了新版本」。

---

## 4. 学生可见内容的元数据隔离

学生可见的回复绝不含风险等级、心理标签、情绪分数、报告 ID。这条规则由三层保障：

1. `PromptTemplates.answer_system_prompt()` 在 CHAT 分支明确禁止主动测评与输出标签；支持分支同样要求「不要向学生输出风险等级、报告分数或后台标签」；
2. 压缩摘要消息自带作用域声明：仅供内部上下文使用、不要展示给学生、不要据此输出诊断或风险等级；
3. **工程 harness 对真实 SSE 输出做禁用词断言**：`["风险等级", "报告ID", "emotionScore", "HIGH_RISK"]` 任一出现即失败。

这是「声明 + 断言」层面的约束，不是出口的结构化字段白名单；改进方向是在响应出口做字段级过滤（见 [LIMITATIONS](./LIMITATIONS.md)）。

---

## 5. 输入脱敏

`PrivacySanitizer`（`app/services/privacy.py`）在三个位置生效：

```text
进模型前        harness.run() 中 model_input = sanitize(original_input)
写入 Redis 前   RedisShortTermMemoryStore._serialize() 对 role/content 脱敏
从 Redis 读出后 _read() 再次脱敏（防止历史数据未脱敏）
```

覆盖三类信息，统一替换为 `[已脱敏]`：

| 类型 | 规则 |
| --- | --- |
| 手机号 | `1[3-9]\d{9}` |
| 邮箱 | `[\w.+-]+@[\w-]+(?:\.[\w-]+)+` |
| 身份证号 | 18 位数字（末位可为 X/x） |

**边界要写清楚**：这是最小可用实现，不覆盖姓名、学号、住址、银行卡、微信号等；MySQL 中保存的是原文（出于合规审计需要），因此数据库侧的访问控制与审计同样重要。脱敏只是「不把标识信息送进模型和热存储」，不是完整的匿名化方案。

---

## 6. 工具风险策略与审计

`ToolPolicyRegistry` 为每个工具定义允许的风险等级范围，`ToolGovernanceService` 在队列 worker 执行前强制校验并写审计：

| 工具 | 允许的风险等级 |
| --- | --- |
| `EXCEL_REPORT` | LOW / MEDIUM / HIGH |
| `CASE_CREATE` | MEDIUM / HIGH |
| `ALERT_SEND` | HIGH |
| `RISK_ALERT`（兼容保留） | HIGH |

每次执行写入 `tool_audit_records`（`allowed`、`policy`、`status`、`reason`、`payload`），可通过 `GET /api/admin/tool-audits` 查询。这是「低风险消息不应触发辅导员预警」这条策略的执行点。

**生效范围**：策略门闸、审计、限流、重试、死信、依赖校验都只在 `TOOL_QUEUE_ENABLED=true` 的队列 worker 路径内生效；`TOOL_QUEUE_ENABLED=false` 的 MCP stdio 直连路径会直接调用 `ToolOrchestrationService`，绕过 `ToolPolicyRegistry` 与 `tool_audit_records`。两条路径复用同一套工具实现，但治理能力只属于队列路径。

---

## 7. `SAFETY_OVERRIDE` 的四个消费点

一旦判定 `risk == HIGH`，`SafetyAgent` 发布 `SAFETY_OVERRIDE` 事件。它被四个位置消费，实现「本轮全局强制升级」：

```text
EventDrivenAgentRuntimeService._select_intent()  → intent 直接变为 RISK
EventDrivenAgentRuntimeService._select_risk()    → risk 直接变为 HIGH
EventDrivenCoordinator._risk_value()             → context / response 任务优先级提升到 CRITICAL
autonomous._risk_level()                         → ResponseAgent 走 support / high-risk 分支
```

效果是：高危既提高「必须审」的优先级（上下文与回复任务被抬到 CRITICAL），也提高「必须优先产出」的调度优先级。

---

## 8. 已知边界与改进方向

| 边界 | 现状 | 改进方向 |
| --- | --- | --- |
| 否定检测 | 词典为子串匹配，「我不想自杀」会被判 HIGH；误报方向安全，但会产生不必要的干预 | 引入否定 / 假设 / 引用语境识别层 |
| 回复安全审查 | 规则要素检查，不是回复安全分类器 | 增加独立的安全分类器与「危险细节」检测 |
| 元数据隔离 | 依赖提示词声明与 SSE 禁用词断言 | 出口做字段级白名单，替代纯断言 |
| 脱敏覆盖 | 仅手机号 / 邮箱 / 身份证 | 扩展实体类型，并做数据侧的权限与审计 |
| 直连路径治理 | MCP 直连绕过队列的策略门闸与审计 | 两条路径统一走治理层，或在直连模式显式告警 |

各项的风险等级与路线图见 [LIMITATIONS](./LIMITATIONS.md)；审查链路的运行时上下文见 [ARCHITECTURE](./ARCHITECTURE.md)。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：分层、Runtime、五条件门闸与 `SAFETY_OVERRIDE`
- [RAG](./RAG.md)：检索链路与降级
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：记忆压缩的脱敏与作用域声明
- [API](./API.md)：认证、角色隔离与错误码
- [QUALITY](./QUALITY.md)：安全断言的测试覆盖
- [BENCHMARKS](./BENCHMARKS.md)：RAG 实测数据
- [LIMITATIONS](./LIMITATIONS.md)：安全策略深度与演进路线
- [DESIGN-NOTES](./DESIGN-NOTES.md)：安全相关设计决策（D5–D7、D10）
