# confidence语义与结构化计划验收

日期：2026-10-25。对应原TXT第2、3条的工程修正。最终版本已部署本机8080，未使用手工加权分冒充质量概率，也未宣称完成统计校准。

## 三种含义分开

| 用途 | 新表示 | 决策用途 |
| --- | --- | --- |
| 任务认领 | AgentDecision.claim_priority；事件metadata.claimPriority | 有界相对调度偏好，只用于候选排序 |
| 风险评估自报 | 可空confidence＋confidenceInfo来源/校验/校准状态 | 仅记录参考，不参与风险升级、降级或计划验收 |
| 回复计划准备 | plan-contract-v1布尔检查＋证据产物ID | 所有必需准备条件通过才进入正文生成 |

AgentArtifact不再含通用confidence字段。Understanding/Context/Response/plan_review原先固定的0.78/0.88/0.86/0.95产物分数已移除；部分调度偏好仍是确定性常量，明确不表示正确率。风险严重度仍由原规则、模型标签及0–4工程口径判定。

新增规则、启发式与兜底评估的confidence为null，不再编造0.95/0.75/0.4。模型正常给出有限0–1 JSON数值时保存为model_self_report，标not_calibrated；缺失/无效时只丢弃该可选字段，记录missing/invalid及原因，不抹掉已有有效HIGH判断。无效严重度/风险结构仍走原评估失败策略。

旧报告confidence数值原样保留；没有来源元数据时标legacy_unknown，不把旧规则常量反推为模型概率。Report API保留confidence兼容字段，新增confidenceInfo。客户端应按confidenceInfo解释值；null不等于0。历史trace结构也保留，不把旧字段当成本版契约。

Excel保留已有六列和旧值，在后面增加confidenceKind、confidenceCalibration；新规则报告confidence单元格为空。辅导员交接摘要与预警邮件明确“模型自报、未校准”“规则/兜底不适用”或“历史来源未标注”。

## 删除无效门槛，使用8项准备条件

1. 当前意图、风险及有效评估存在。
2. 计划绑定当前intent/risk/context产物ID，防止使用过期或错轮证据。
3. 路由、风险与响应模式匹配当前评估。
4. 消息结构、当前用户输入及服务器安全策略匹配。
5. 上下文证据结构与general_chat/retrieved_support/generic_support模式声明一致。
6. 需要澄清时已加入服务器的澄清消息。
7. 独立SafetyAgent准备检查绑定当前方案及scope。
8. 准备检查严格approved=true且必要结构检查通过。

PLAN_ACCEPTED/PLAN_REJECTED事件记录每项检查、失败项、证据产物ID和confidenceUsed=false。此处只验收prompt计划的结构准备；最终正文仍须通过原reply-safety-v2规则及模型审核后才发SSE。空检索可声明generic_support，不把缺少检索结果作为阻止即时安全支持的理由。

旧AGENT_FINAL_ACCEPTANCE_MIN_CONFIDENCE配置已退役；显式配置会输出LEGACY_CONFIDENCE_THRESHOLD_IGNORED告警，不再控制放行。兼容字段默认null且不导出到MCP环境。事件驱动与顺序对照编排共用同一个准备契约。

## 可复现的门槛对照

| 受控准备场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 合法计划，旧阈值0.6 | 接受 | 接受 |
| 同一合法计划，旧阈值1.0 | 因固定0.86被挡 | 契约通过后接受，记录弃用告警 |
| 同一方案清空messages，保留匹配且approved的预检 | 旧采纳函数接受 | 计划契约拒绝 |

该对照是协调器准备合同测试，不代表旧系统会把不安全正文实际发给学生；旧版已存在后置正文审核。修复提升的是前置准备验收的真实性与可诊断性。

## 迁移与验证

- 报告confidence改为可空，新增可空confidence_metadata_json。MySQL在命名锁下增量修改，不重写旧值。SQLite对旧非空列做事务性重建，保留原行、外键状态、索引、触发器与自增序列，并检查外键完整性；已测试重复迁移。
- 158项单元/回归及原6组工程Harness通过。覆盖每类准备条件的破坏、过期/伪造审核、无证据通用支持、计划拒绝但高危outbox保留，以及低/缺失/无效自报仍触发高危动作。
- 独立真实MySQL/Redis/Excel/本机SMTP与实际MCP，受控mock模型6类HTTP验证全部通过，18个工具任务SUCCESS；旧合成报告0.75保留，新规则报告及Excel为NULL/空，来源明确。
- 首次探针中，0.01自报的HIGH计划已正确接受，但原词面mock对含蓄输入生成了缺危机要素的正文，被原安全门闸拒绝。失败记录已保留；后续只为控制变量注入已知安全正文，未改生产生成器或降低正文审核标准。
- 真实模型原100条意图100%、危险召回100%；32风险100%；原8组完整对话8/8，13轮正文均被接受。原7份冻结数据未改。
- 真实模型80条固定安全候选：误拦0/40、漏放0/40；12次固定坏稿均被拒，真实再生成10次接受、2次因UNSUPPORTED_RESOURCE安全兜底。并非12次全部生成成功，完整结果保留。
- 事件驱动与顺序编排的mock诊断修复前后均7/8：crisis-continuation第二轮的词面mock缺危机要素，正文正确兜底；没有隐去该限制或改gold使其全绿。真实模型对话8/8。
- 最终8080四次真实模型请求（CHAT/CONSULT/HIGH/危机承接）正文均接受，7任务SUCCESS；容器69个app文件匹配最终源码。
- 旧40份报告、57条trace和原8类后台旧字段保留；49个旧会话可查，台账原36行的旧列值未改。新风险邮件在本机Mailpit中核验，Redis TTL及Chroma34段正常。

## 解释边界与复测

本次解决了字段混义、假概率及无效验收门槛，没有把模型自报校准为正确率。ECE/可靠性分桶需要另备适合该预测任务的独立标注数据；现有100条意图回归也不能替代心理分级置信度校准。准备检查的全true不证明模型标签、检索资源真实性或最终正文必然正确，后置审核与安全兜底仍必要。

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner
.venv/bin/python -m app.evaluation.intent_runner --provider configured --repeats 1 --output target/confidence/recheck-intent.json
.venv/bin/python -m app.evaluation.runner --suite all --provider configured --architecture event --output target/confidence/recheck-dialogues.json
.venv/bin/python -m app.evaluation.reply_safety_runner --provider configured --mode all --repeats 1 --output target/confidence/recheck-reply.json
```

公开汇总：docs/benchmarks/confidence-contract-v1-summary.json。逐条真实结果：target/confidence/。本机源码快照、SQLite/MySQL迁移验证、受控对照、首轮失败、构建/部署、旧数据保护及0600数据库备份：.local-runtime/confidence-contract-20261025/（含私有业务数据，不公开发布）。
