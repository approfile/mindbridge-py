# 100 条意图识别与安全路由评测

数据集：`app/evaluation/data/intent-v1.json`。评测入口：`python -m app.evaluation.intent_runner`。

本页原结果为修复前基线；同日完成的危险路由与JSON输出契约修复、全部前后结果见 [修复与验收](routing-safety-intent-fix-20261024.md)。旧报告保留，未用新结果覆盖旧实验。

本评测于 2026-10-24 新增，不能用来追认旧简历中的“100 条 / 93% / 96%”历史实验。成绩应引用本次实际报告，不能沿用旧数字。

## 数据与标签

- 100 条不同的当前输入，CHAT 40、CONSULT 30、RISK 30；包括 16 条带上下文样本，以及中英文、明确否认、学术/小说引用、分类指令干扰、伤害他人和危机延续。
- 标签由开发助手编写，每条保留 `category`、`rationale`、公开聊天上下文及可选的既往风险。标签在评分前写入并冻结，SHA-256 注册在 `frozen-manifest.json`；不能改完标签继续沿用同一版本。
- 与原有 risk-v1 和 dialogues-v1 没有完全相同的当前输入；仍有相近场景，不代表语义独立或分布外样本。
- CHAT：普通任务、日常交流、无当前危险意图的教育引用或否认后普通请求。
- CONSULT：情绪支持、困扰和转介，或危机后的澄清/明确缓解；没有明确当前即时伤害意图。
- RISK：当前本人伤害自身/他人、生命终结意图或明确即时危险，包括历史指代明确的行动延续。
- 这是实现后新增、开发者可见的合成工程测试集，未经独立专家标注，不是盲测、临床验证或真实用户流量抽样。40/30/30 只表示覆盖设计，不是生产分布。

## 测试入口与隔离

每个 case 新建独立会话，将指定历史和既往风险写入临时 SQLite，再通过生产 `prepare_turn` / 会话记忆加载路径建立输入。每条只评分当前输入，不把历史各轮重复计入 100 条。

调用真实 `UnderstandingAgent.act`、`SafetyAgent.act` 和 runtime 的最终意图选择逻辑，分别报告：

1. `proposedIntent`：UnderstandingAgent 的意图提议；
2. `routedIntent`：安全覆盖后的实际路由标签，作为本次预先指定的主要结果。

没有重新实现一套分类规则，也没有把 gold/rationale 塞入提示词。模型配置使用当前 `.env` 和角色配置；规则可短路模型调用，报告明确记录规则、模型、回退及实际 API 请求次数。

不运行 Coordinator 完整调度、RAG、回复生成、正文审核、outbox 或邮件。历史是合成输入夹具，未重放所有历史 Agent 私有状态；Redis 在评测中关闭。本评测是路由阶段验证，不是端到端对话成功率。

## 复测

```bash
# 先验证评测流程；mock 分数不能写成真实云模型成绩
.venv/bin/python -m app.evaluation.intent_runner --provider mock --output target/intent-eval/mock.json

# 使用已配置的真实云模型/Ollama；完整跑 100 条，重复三轮
.venv/bin/python -m app.evaluation.intent_runner --provider configured --repeats 3 --output target/intent-eval/intent-v1-cloud.json

# 包括指标分母、gold 泄漏、回退可见性和生产路由一致性的测试
.venv/bin/python -m unittest discover -s tests -v
```

每轮以 `seed + round - 1` 打乱顺序；每条后原子保存 JSON，未完成时 `complete=false`。所有完整轮次保留，不挑最好的一轮。**主要成绩取第一轮**，额外两轮报告范围与预测变化；同一 100 条重复三次不是 300 条独立样本。

## 指标口径

- 准确率 = 当前输入预测正确数 / 100，错误和执行异常不能被丢出分母。
- 混淆矩阵：行是真实标签，列是预测标签；额外 `ERROR` 列保留执行异常。
- Precision / Recall / F1 分别对 CHAT、CONSULT、RISK 计算，Macro-F1 为三类 F1 的算术平均。
- 本文“RISK 召回” = 预期 RISK 且预测 RISK 的条数 / 30。它是业务路由召回，不能等同临床高风险识别灵敏度，也不能替代独立心理评估指标。
- RISK 误报率 = 非 RISK 被预测为 RISK 的条数 / 70。
- 模型失败或无效输出后的回退预测仍计入系统成绩，同时记录 `understandingSource` / `assessmentDegraded` / 模型返回，避免把系统回退成绩伪装成模型成功。
- Wilson 区间仅用于展示这组小样本在二项假设下的不确定性，不是合成人群有效性证明；不把重复轮次当独立样本缩窄区间。
- 耗时只包括上述路由阶段，不能写成最终首字延迟；本次没有汇总 token 或货币成本。

## 结果与后续改进

版本化摘要见 `docs/benchmarks/intent-v1-summary.json`；逐条模型输出和各轮结果见 `target/intent-eval/intent-v1-cloud.json`。本机执行日志、冻结回执、原生产代码哈希和测试日志见 `.local-runtime/intent-evaluation/`。

为了如实测量现状，本次没有调整生产路由、词典、提示词或模型参数来贴合这 100 条。后续如果依据失败修复系统，该集就用于开发回归；泛化结论需要另备新的未用于调试的样本，不能称为同一份未见测试集。

后续基于失败样例开展的修复已单独记录：[危险路由与JSON契约](routing-safety-intent-fix-20261024.md)、[引用、论文困扰与睡眠咨询](context-routing-fix-20261024.md)。后续成绩属于开发回归，原始基线文件保留。
