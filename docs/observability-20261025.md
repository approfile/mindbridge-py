# 统一关联日志、显式查询降级与在线耗时分解

日期：2026-10-25。对应原缺陷清单第11、13、17条。最终版本已运行于本机8080；本轮未改变意图分类、风险分级及正文审核规则，7份冻结数据标签均保持原样。

## 可用能力

- API为每次HTTP请求生成request_id，返回X-Request-ID；每轮对话生成独立run_id，并与黑板turn_id一致。同会话不同轮次保持session_id，使用不同request/run标识。客户端传入的同名ID不作为主关联标识。
- 业务日志统一为JSON，关联request_id/run_id/session_id/trace_id/report_id/job_id。trace_id在SQL落库前可为空，以run_id串联早期事件；持久工具任务保存关联信息，Worker及MCP实际执行时恢复，工具审计也保存来源关联。历史任务保持NULL，不伪造补齐。
- 查询改写返回结构化结果。source=model或original_input，degraded、reasonCode、errorType、输入/输出长度和truncated随context artifact保存；同时进入observability.degradations及operation.degraded日志。失败继续使用原输入检索，不再静默。
- REWRITE_TIMEOUT、REWRITE_EMPTY_OUTPUT、REWRITE_INVALID_OUTPUT、REWRITE_CALL_FAILED分别表达超时、空串、无效类型及调用异常。成功仍保持原有60字符查询限制；元数据明确记录截断，不假装解决长输入语义丢失。
- 对话trace新增observability：单调时钟span、父子ID、inclusive durationMs及exclusiveMs。覆盖memory.load、agent.act（附角色）、query.rewrite、retrieval/embedding/BM25/rerank、reply.generate/rules/review、persistence和sse.emit；模型调用另记角色、provider、model和调用内首chunk耗时。
- response_review事件保留原elapsedMs，新增generationMs、ruleReviewMs、modelReviewMs（含返回解析）。规则直接拒绝而未调用模型时modelReviewMs为null，不能当作一次零耗时模型审核。
- HTTP完成日志覆盖流式响应生命周期，并记录stream_status；HTTP200不再被当作生成成功，safe_fallback/cancelled/planning_failed等状态另行表达。
- 新增管理员详情入口GET /api/admin/agent-traces/{trace_id}，按主键查找，学生403、未知ID404。旧列表接口兼容，旧记录的新字段为空。

## 日志位置与保留

API业务JSON同时写stderr和可选本机JSONL；本次配置的文件为.local-runtime/container-data/logs/application.jsonl，容器内/app/data/logs/application.jsonl，权限0600。

OBSERVABILITY_LOG_FILE控制文件位置；默认每文件10MiB、3个备份（当前文件加备份最多约40MiB），OBSERVABILITY_LOG_MAX_BYTES及OBSERVABILITY_LOG_BACKUPS可配置。Docker json-file另设10m×3文件。MCP子进程输出结构化stderr，不与父进程竞争同一滚动文件。

日志只输出允许的元数据，不新增学生原文、查询文本、候选正文、提示词、Authorization或异常消息；错误只记类型与原因码。原有SQL trace的原文访问治理属于另一项问题，本次未扩展学生权限。

Uvicorn与第三方库日志可保留自身格式；统一JSON及关联承诺针对本项目业务事件。多进程/多实例宜采集stderr，文件滚动方案按单父进程部署。

## 一次真实睡眠咨询的耗时例子

该次request_id=56985fdb9a484622a1dff026079d5fb3，trace_id=55，模型DeepSeek，实际向量检索使用qwen3.7-text-embedding与Chroma 34段。

| 测量项 | 毫秒 |
| --- | ---: |
| 规划总阶段 planning.total | 5825.3 |
| 查询改写（规划子阶段） | 630.1 |
| 检索总阶段（规划子阶段） | 4176.6 |
| embedding请求（检索子阶段） | 4102.7 |
| 正文生成 | 1634.9 |
| 正文规则审核 | 0.7 |
| 正文模型审核 | 875.0 |
| 模型调用内首chunk | 689.1 |
| 请求开始→服务端提交首段已审核文本 | 8389.2 |
| 客户端实际收到首段文本 | 8391.3 |

父阶段已包含子阶段，不能把上表所有行相加。exclusiveMs减去直接子span覆盖区间的并集。模型首chunk仍被缓冲，不是学生看到的首字时间；服务端首文本时间在ASGI send调用前打点，客户端时间另含传输。HTTP完整耗时在http.completed日志，包含流的结束和观测写入；turn.total是服务逻辑阶段，二者起止边界不同。异步工具的job_age_ms/eligible_wait_ms及执行span单独记录，不加到请求总耗时。

以上是单次样例；4次真实请求不构成线上P50/P95，不据此宣称速度提升。logicalModelCalls是逻辑LLM客户端调用数；embedding有独立span。tokenUsage/currencyCost保留null，不捏造token或费用。

## 存储边界

第一候选的两轮真实对话trace达到56,859字节，接近MySQL TEXT的65,535字节上限。最终完成时替换临时规划快照，只保留一份最终观测；同时将agent_steps_json无损扩容为MEDIUMTEXT。新库ORM与旧库启动迁移保持一致，MySQL命名锁保护迁移；SQLite继续使用TEXT。

独立MySQL实测保存并读回120,733字节的合成记录，重复迁移及重复快照写入通过；旧11条探针trace内容哈希未变。主库扩容前53条trace内容哈希也逐项保持一致。两次升级前均已生成私有数据库备份。

span收集上限512，超出会计入droppedSpans，不能把丢失后计数当完整指标。观测落库失败记录日志并保留planning阶段快照，不冒充complete；业务报告与工具outbox仍沿用原事务、幂等键、租约及审计边界。

## 实际验证

- 141项单元/回归、原6组工程Harness通过。覆盖请求隔离、取消、日志脱敏与滚动、真实MCP协议、迁移、快照去重和原可靠投递故障路径。
- 独立真实MySQL/Redis/Excel/Mailpit环境，用受控mock模型完成11次HTTP：改写超时/空串/异常/无效输出、正常、生成失败、审核失败、实际断开流、并发请求、实际MCP。19个任务SUCCESS，相关request/run标识与审计一致；探针端口已关闭。这不是11次真实云模型评测。
- 真实模型原100条意图：100%，危险召回100%；原32风险100%；8组完整对话8/8，13轮均通过正文审核。各只复跑一轮，非新盲测。
- 真实模型80条固定安全候选：正常误拦0/40、应拒绝漏放0/40，审核不可用0；12次坏稿修订11次被接受、1次固定安全兜底，未放宽正文规则。
- 最终8080四次真实模型HTTP均完成；三路及高风险后的安全更新路由正确，5个工具任务SUCCESS。响应头、SSE meta、trace、JSONL、工具任务与审计关联一致；容器66个app源码文件与主机最终源码匹配。
- 原8类后台旧字段保留、43个旧会话可查询；扩容前的增量验收记录也逐项保留。Redis消息/TTL、Chroma/embedding、Excel以及新风险邮件在本机Mailpit中核验通过。

这些数据用于开发可见的合成工程回归，不能当作独立临床验证；重复或相关候选不能合并宣称更多独立样本。当前是本地span与显式持久关联，并未接入OpenTelemetry exporter、上游traceparent或ELK/Loki，也未做全链路并发优化。

## 排查与复测入口

1. 从响应头X-Request-ID或SSE meta.requestId取编号。
2. 在JSONL按request_id检索；agent.run.persisted事件包含SQL trace_id，后续事件可按run_id串起Worker。
3. 用管理员身份查询/api/admin/agent-traces/{trace_id}，查看observability.stageTotalsMs、spans、degradations和streamStatus，再按report_id/job_id查工具任务及审计。

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner
.venv/bin/python -m app.evaluation.intent_runner --provider configured --repeats 1 --output target/observability/recheck-intent.json
.venv/bin/python -m app.evaluation.runner --suite all --provider configured --architecture event --output target/observability/recheck-dialogues.json
.venv/bin/python -m app.evaluation.reply_safety_runner --provider configured --mode all --repeats 1 --output target/observability/recheck-replies.json
```

公开汇总：docs/benchmarks/observability-v1-summary.json。真实评测逐条结果：target/observability/。本机完整证据及未删除的第一候选：.local-runtime/observability-20261025/；其中包含私有数据库备份，不用于公开发布。
## 版本说明

本文保留 10 月 25 日的阶段耗时验收记录；2026-11-01 已新增真实 token/费用与缓存账本，当前实现与验收见 [模型用量与缓存观测](MODEL-USAGE.md)。下文 tokenUsage/currencyCost 为 null 的描述仅指当时版本。
