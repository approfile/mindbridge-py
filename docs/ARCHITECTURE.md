# 当前架构（2026-10-30）

HTTP 认证与请求预算 → MindBridgeAgentHarness → 事件驱动共享黑板 → 计划验收 → 持久化报告与任务 → 正文生成与审核 → SSE 分块输出。工具 Worker 独立消费持久任务。

| Agent | 职责与边界 |
| --- | --- |
| Coordinator | 管理任务依赖、能力认领、预算、风险覆盖和最终计划验收；不是通用 LLM 自主规划器 |
| Understanding | 提议 CHAT / CONSULT / RISK；最终路由还受独立风险评估覆盖 |
| Safety | 风险评估、SAFETY_OVERRIDE 和绑定 proposal 的计划审查；实际正文随后另行审核 |
| Context | 读取会话记忆、选择 Skills，咨询/风险场景按需 RAG |
| Response | 发布 response_proposal 消息/prompt 计划，不是最终生成正文 |

当前计划验收包含 assessmentsPresent、currentArtifactBindings、routeMatchesAssessment、messagesAndPolicyPrepared、evidenceModeDeclared、clarificationPrepared、independentReviewBound、reviewPassed 共 8 项。验收不使用未校准 confidence 门槛；claim_priority 只是调度偏好。[具体契约](confidence-contract-20261025.md)。

实际正文由 Response 模型生成，经过规则和模型双重审核，最多一次修订，失败进入固定安全兜底；审核通过后才开始发送正文块。因此 SSE 首块时间包含生成和审核，不应称原始模型 token 直出。

报告与 tool_jobs 在同一事务中提交。CONSULT/LOW 建报告与 Excel；MEDIUM 增加个案；HIGH 增加预警，CASE_CREATE 必须先于 ALERT_SEND。CHAT/LOW 不建报告。Worker 使用条件认领、租约、claim token、依赖、重试、死信与工具审计。工具由服务端策略选择，不等于模型原生自主 Function Calling。

并发准入默认单进程 4、用户 2、会话 1，整轮 60 秒；不是分布式锁。持久任务不会因 SSE 断连被取消。SMTP 收件与数据库提交不构成原子事务，仍可能重复发送。

主要源码：app/agents/{coordinator,event_driven_runtime,harness,plan_validation}.py、app/services/{chat,response_review,tool_queue,tool_execution,turn_execution}.py。[全部边界](LIMITATIONS.md)。
