# 请求并发与整轮预算

日期：2026-10-25。本次只修改主项目，未同步改写 `dist/` 和 `github-repo/` 副本。对应原缺陷中的同步阻塞、请求无并发边界、整轮耗时没有共同截止时间。

## 修正的实际问题

原先 SSE 异步生成器直接调用同步 Harness；规划中的同步模型请求、Redis、SQL 会占住事件循环。单次模型超时、Agent 最大轮数和回复45秒超时分别存在，但没有覆盖认证、规划、回复审核、落库、发送及慢客户端的共同预算。请求断开后同步函数也无法自动停止，直接释放限流名额会让真实后台工作不断叠加。

现在使用以下路径：

```text
Observation → 全局准入 → HTTP单调时钟截止时间
  → 认证线程独立Session → 用户/会话准入
  → 有界专用线程池：独立Session完成规划、报告和outbox
  → 异步生成+正文审核，受剩余时间约束
  → 有界线程池：独立Session保存已审核回复
  → SSE token/done → 被跟踪的trace收尾 → 释放名额
```

`OwnedTurnWork` 每次进入阻塞阶段都创建、使用并关闭自己的 SQLAlchemy Session，只把用户ID、会话引用及普通数据交给下一阶段。认证也拥有独立 Session；请求取消不会提前关闭另一个线程仍在使用的连接。事件循环不再执行同步规划、报告保存和回复落库。

## 准入和预算约定

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| TURN_MAX_INFLIGHT | 4 | 单进程已准入轮次上限，含仍在收尾的工作；专用线程池同样有界 |
| TURN_MAX_INFLIGHT_PER_USER | 2 | 已认证用户的并发上限 |
| 同会话上限 | 1 | 对同一用户、同一会话的未结束轮次互斥 |
| TURN_TIMEOUT_SECONDS | 60 | 从HTTP入口开始的单调时钟预算，含读请求、认证、规划、生成/审核、落库、发送 |
| TURN_IO_TIMEOUT_SECONDS | 15 | httpx各阶段timeout取原设置、此值与剩余预算的最小值；并非单个HTTP调用总耗时的独立硬截止时间 |
| TURN_CLEANUP_WAIT_SECONDS | 0.2 | 正常结束时等待日志收尾的上限；并发名额仍等真实工作结束才释放 |
| TURN_MAX_REQUEST_BYTES | 65536 | 在解析JSON前累计读取的字节上限 |
| ChatRequest.message | 8000字符 | 解析后的消息长度上限，超限422 |
| DATABASE_POOL_TIMEOUT_SECONDS | 5 | MySQL连接池等待上限 |
| DATABASE_SOCKET_TIMEOUT_SECONDS | 10 | PyMySQL连接、读、写超时 |

全局容量在认证及JSON解析前检查。超过全局/用户容量返回429；同会话冲突返回409；字节超限返回413。请求尚未发送响应头时，整轮超时返回504；已开始SSE时发送 `error` / `TURN_TIMEOUT` 并结束，不再发送 `done`。错误发送最多再等待0.1秒，避免慢接收端再次拖住请求。浏览器只有收到明确的 `done` 才显示DONE，EOF/断连不能冒充完成。

停止信号使用不能被普通模型异常兜底吞掉的 `TurnStopped`。规划、工具前准备、记忆访问、模型/embedding调用前后及SSE发送均检查截止时间；超时返回的模型结果不会继续被采纳。异步生成/审核由剩余预算取消，原回复45秒限制仍在整轮预算内部生效。

## 取消与持久任务的边界

Python线程不能安全强杀。请求超时或客户端断开时，HTTP可以先结束，已在执行的SQL、网络调用或提交仍允许返回；对应名额、用户计数及会话互斥一直保留到工作和trace收尾实际结束，防止后台线程突破限额。StreamingResponse显式关闭生产者，避免慢客户端取消发送时把生成器留给GC而泄漏名额。

已观察到的风险不会因为停止信号降级：运行时保存最近黑板，停止后只做规则判定和必要持久化，拒绝继续规划/生成；已识别HIGH仍保存报告及三项outbox。已提交的工具任务由原独立Worker执行、重试和恢复，不受聊天断开影响。尚未认证、被拒绝准入、或尚未识别的请求不能声称已经生成风险报告。

如果 `done/error` 已在预算内成功发送，后续日志写入较慢不会反向把已完成回复标成超时；收尾仍占名额。trace新增 `turn_control`，包括budgetSeconds、elapsedMs、remainingMs、reasonCode、riskKnown和不可强杀工作的说明；原关联ID与阶段耗时继续保留。可结合 `turn.deadline_exceeded`、`turn.rejected`、`turn.capacity_released`、`chat.completed` 日志排障。

## 实际验证

以下均保留完整失败与降级记录；原7份冻结数据没有改动，不能解释成临床有效性或独立盲测结果。

- 最终173项单元/回归通过；原6组工程Harness通过；前端JavaScript语法检查通过。新增覆盖慢规划不阻塞健康检查及其他请求、全局/用户/会话限制、认证超时与连接所有权、收尾期间继续拒绝、请求字节上限、危险持久任务、客户端断开、发送背压、已完成回复不会被慢收尾改判。
- 独立MySQL、Redis11、真实TCP HTTP、Excel和Mailpit探针：规划/检索/生成三个不同注入点均约0.253–0.254秒结束0.25秒预算；HTTP已开始，返回SSE错误而非done。检索阶段HIGH仍完成三项任务。全局429、会话409、恢复及413通过。
- 实际TCP socket中断HIGH检索：未完成工作期间同会话409，随后trace为CLIENT_DISCONNECTED，三项持久任务SUCCESS，无正文审核/后续生成，结束后恢复。
- 本机慢OpenAI兼容HTTP stub通过5项检查：真实同步补全、异步审核、流式模型、embedding各阶段超时生效；0.06秒剩余整轮预算约0.064秒停止，停止后不产生新HTTP请求。此项没有调用云模型，不冒充真实模型质量测试。
- 真实模型：原100条意图识别及最终路由100/100，包含1次模型请求失败后规则兜底，未从分母剔除；32条风险32/32；8组多轮对话7/8、13轮中12轮接受。失败为crisis-continuation第二轮GENERATION_FAILED后安全模板，HIGH/RISK分流仍正确，未算通过，也未重复挑最好结果；现有记录不能精确区分该次生成失败的底层异常。
- 真实模型80条固定正文候选：误拦0/40、漏放0/40；12条坏稿首轮均拒，真实修订10条接受、2条因无来源资源走安全兜底。意图和固定正文runner不经过HTTP入口，不能拿它们证明整轮60秒预算；整轮由多轮服务、专门HTTP及故障注入验证。
- 本机8080四类真实模型演示及历史保护以公开摘要中的最终验收为准；真实embedding/Chroma、Redis、Excel和本机Mailpit一起核对，未向真实辅导员发送邮件。

### 慢请求阻塞对照

同机、独立数据库、同mock策略，对一条规划注入0.8秒同步等待；等待已开始后并发发健康检查与另一条普通聊天，各3轮。不是云模型性能对比，也不是负载容量或线上SLA。

| 观测 | 修复前，毫秒 | 修复后，毫秒 |
| --- | --- | --- |
| 健康检查 | 888.039 / 904.242 / 914.020 | 1.520 / 2.506 / 2.176 |
| 另一条聊天 | 947.906 / 990.152 / 978.729 | 52.514 / 72.041 / 59.184 |

## 复测与证据

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner
node --check app/static/student.js
.venv/bin/python -m app.evaluation.intent_runner --provider configured --repeats 1 --output target/turn-budget/recheck-intent.json
.venv/bin/python -m app.evaluation.runner --provider configured --architecture event --output target/turn-budget/recheck-dialogues.json
.venv/bin/python -m app.evaluation.reply_safety_runner --provider configured --repeats 1 --output target/turn-budget/recheck-reply.json
```

公开汇总：`docs/benchmarks/turn-budget-v1-summary.json`；逐条合成评测：`target/turn-budget/`。私有证据：`.local-runtime/turn-budget-20261025/`，包括原代码快照、冻结数据哈希、0600数据库备份、旧后台/台账快照、受控服务与transport探针、失败首轮、最终173项日志、构建及部署记录、容器文件哈希。不得把该私有目录打包发布。

首次探针等待Mailpit任务的时间不足，观察到RUNNING即失败；保留记录并延长观察后确认成功。另一次断连探针错误使用调用方自带request ID查trace，而服务器会自行生成ID；改为按唯一会话查找后通过。最初“生成超时”注入标记会同时命中规划延迟，后续收紧并断言到达实际生成阶段；中间结果保留，不用它证明生成阶段覆盖。首次Docker构建因Hub TLS超时失败，原样重试成功。

## 尚未解决的范围

这是单进程准入和协作式取消，不是跨实例Redis信号量、网关限速、硬隔离进程或可强杀计算任务。多worker/多实例会分别计算名额；客户端断开也不能保证上游模型供应商停止计费。既有SMTP重复窗口、原文隐私/保留期限/读审计、真实token/成本采集、独立质量与临床评测均不在本次范围。云模型仍会失败或触发安全兜底，不能把受控并发测试或四次成功演示表述成“所有回答永远成功”。
