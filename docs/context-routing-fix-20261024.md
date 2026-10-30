# 引用、论文困扰与睡眠咨询路由修复

本次修复原100条意图回归中的英文文学引用误报、论文困扰被当作普通任务、睡眠求助随风险评估波动的问题。已用配置中的真实DeepSeek模型验证，并更新本机8080服务。

## 改动与理由

- 文学引用按每段引号的归属识别，支持英文文学语境、单双引号和中文引号。仅有论文/小说关键词不授予豁免；裸引号、声明本人真实感受、引用外的当前危险仍升级。
- 引用规则要求实际讨论心理/危险主题，不能因为出现论文二字就把本人受挫判成CHAT。明确本人困扰先于普通任务快捷规则。
- 给我、帮我、解释、如何等通用动词不再单独触发CHAT快捷判断；仍保留有领域主题、无困扰信号的普通任务捷径。
- 睡不好、心慌等当前支持需求可以是CONSULT且LOW，分类不再依靠Safety恰好返回MEDIUM来纠正。
- 评估prompt明确0–4工程口径：短期睡眠困扰本身不触发MEDIUM，持续显著困扰或严重生活受损可升级。没有在代码中把几天睡不好强制压为LOW；模型的MEDIUM/HIGH及明确危险规则仍有效。
- 正文审核、最多一次修订、安全兜底与可靠工具投递源码未变，其他Agent类AST未变。

## 原100条：同一标签、顺序、模型配置与评分公式

| 轮次 | 修复前最终准确率 | 修复后理解准确率 | 修复后最终准确率 | RISK召回 | 格式/降级案例 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 98% | 100% | 100% | 100% | 0 |
| 2 | 97% | 100% | 100% | 100% | 0 |
| 3 | 97% | 100% | 100% | 100% | 0 |

主结果预先指定为第一轮；三轮都报告，不挑最好的一轮。同100条重复三次不代表300条独立样本。原有6份冻结数据（含100条）均未改标签。

焦点样例：intent-029文学引用应CHAT；intent-042论文受挫应CONSULT；intent-047/048睡眠支持应CONSULT。每轮逐条来源、风险和模型原始输出见原始JSON。

## 边界与完整链路

- 改代码前冻结28条对照案例，联合要求意图和风险均正确：修复前18/28；修复后三轮为28/28、28/28、28/28。包括学术普通任务、引用后本人危险、真实感受声明、短期严重受损、危机承接与否认。
- 原32条风险回归：100%；原8组完整对话：8/8；13轮正文审核接受率100%。consult-sleep原先MEDIUM/LOW不符，本轮通过。
- 125项单元/回归、原6组工程Harness通过。
- 本机6次针对性HTTP验收：路由与持久工具全部符合预期，5/6回复通过正文审核，1/6安全兜底；9个工具任务SUCCESS。含英文引用、论文困扰、两类睡眠求助、引用外本人危险和下一轮危机承接。
- 原CHAT/CONSULT/RISK三路冒烟通过；MySQL、Redis TTL、Chroma 34段、Excel和Mailpit本机邮件验证通过。
- 升级前8类后台旧行逐项保留，35个旧会话查询通过；容器关键文件与最终评测源码哈希一致。数据库已私有备份。

## 中间失败与解释边界

六次本机针对性验收中，四次目标普通/咨询请求及危机承接回复均被接受；引用外本人危险反例的两稿均触发UNSUPPORTED_RESOURCE（无检索来源的资源信息），最终走固定模板并发出error，未泄露被拒正文。该次HIGH路由及3个持久任务正确，不能写成六次生成全部成功；没有降低资源校验门槛。详情见本机live-fallback-details.json。

第一候选曾在intent-052暴露同源缺陷：想哭的表述因解释一词被快捷判CHAT；第1轮99/100，第2轮未结束时中止。其源码、部分JSON（complete=false）和日志保存在.local-runtime/context-routing-fix-20261024/candidate1/。随后收紧通用动词捷径，最终版本重新完整测三轮。中间结果没有冒充最终结果。

这些数据是开发可见的合成工程回归集，已用于修复，不能视为盲测、临床敏感度或专家标注成绩。引用和情绪规则仍可能在新表达上误判；LOW仅表示现有输入未达到升级口径，不能证明个人安全。睡眠分级是工程策略，不是诊断量表。模型与正文审核的盲点仍需外部独立样本验证。

最终其他错误：

- 第1轮：无。
- 第2轮：无。
- 第3轮：无。

## 复测入口与证据

```bash
.venv/bin/python -m app.evaluation.intent_runner --provider configured --repeats 3 --output target/intent-eval/recheck-context.json
.venv/bin/python -m app.evaluation.context_routing_runner --provider configured --repeats 3 --output target/intent-eval/recheck-boundaries.json
.venv/bin/python -m app.evaluation.runner --suite all --provider configured --architecture event --output target/intent-eval/recheck-dialogues.json
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner
```

- 原100条新结果：target/intent-eval/intent-v1-cloud-context-fix.json；旧结果仍为intent-v1-cloud-routing-fix.json。
- 新28条数据：app/evaluation/data/context-routing-v1.json；新结果：target/intent-eval/context-routing-after-cloud.json。
- 可发布汇总：docs/benchmarks/context-routing-fix-summary.json。
- 本机完整证据：.local-runtime/context-routing-fix-20261024/（源码快照、冻结凭据、全部中间记录、真实模型/HTTP/SQL/构建验收；含私有数据库备份，不用于公开发布）。
