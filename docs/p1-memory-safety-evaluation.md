# P1：会话记忆、正文审核与可复现评测

本次范围是原提升清单的 ③④⑤。保留已有的报告与 outbox 原子提交、工具授权审计、Worker 重试和幂等机制。

## ③ 有来源的会话记忆

- CHAT、CONSULT、RISK 在路由之前读取同一份历史快照，普通闲聊也能引用前文。
- MySQL 新增 `session_memories`，保存处理游标、最近用户要点和白名单事实。首次加载旧会话时可从原始消息重建；不修改旧消息和报告。
- 事实只从用户消息提取，保留 `sourceMessageId`，明确更正覆盖旧值；助手输出不能变成事实来源。支持项目代号、项目名称、论文主题、目标岗位、回复语言、每天可用时间、称呼，以及两个英文别名。它是受限的会话事实记忆，不是任意知识抽取或跨会话长期画像。
- Redis 仍是有 TTL 的近期缓存，附加消息版本；版本缺失、不一致、失效或不可用时回读 MySQL 并修复缓存。原始窗口、提示词历史和摘要各自有上限。
- 先验证用户所属会话；摘要脱敏；父会话行锁串行化派生状态写入。SQL 原始消息是事实来源，JSON 损坏或来源失效会触发重建。
- 用户可说“忘记项目代号”等删除事实。此操作删除派生事实，不删除原始聊天记录；没有增加数据删除或跨会话检索产品能力。

## ④ 风险上下文与实际回复审核

- 区分明确否认、学术/小说引用、当前本人意图、伤害他人、混合陈述和近期危机延续。持续困扰/含糊表达进入澄清路径；明确表示目前安全可更新本轮状态。
- 规则与模型共同决定业务路由，不是临床诊断工具。模型解析失败会记录 `degraded/source/reasonCodes`，不会把不可用当成正常置信评估。
- 原有“审核候选提示词”明确叫 `plan_review` / `PLAN_ACCEPTED`；最终正文生成后另经规则检查和独立 Safety 模型审核。
- 正文审核必须通过严格 schema；`approved` 只能是布尔值，与违规列表一致。兼容 OpenAI 协议的审核请求使用 `response_format=json_object`，Ollama 使用 `format=json`。仅支持此参数的服务应保持 `REPLY_REVIEW_JSON_MODE=true`；不支持时可显式关闭，仍保留严格解析和失败兜底。参考 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。
- `OPENAI_THINKING_MODE=auto` 对 `api.deepseek.com` 显式关闭思考模式，其他域名保留服务默认值。DeepSeek 当前默认启用高强度思考，会消耗有限输出预算；本项目低延迟、512 token 的交互采用非思考模式。可显式设为 `enabled` 并自行提高输出预算。探测仅记录用量、长度和结束原因，推理内容不作为回复展示。参考 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
- OpenAI 协议流式和非流式返回 `finish_reason=length` 时视作未完成，不把被截断的正文或 JSON 当成成功。
- 最多修订一次，字符上限默认 8000、生成及正文审核总限时默认 45 秒。审核服务失败、无效 JSON、空生成、修订耗尽或规划未通过时，返回固定通用支持信息及 SSE `error`，不发成功 `done`。
- 不合格正文不落为助手消息、不发送给前端；Trace 保留候选哈希、字符数、违规代码、耗时和最终状态。只有实际正文通过才产生 `FINAL_ACCEPTED`。
- **延迟取舍**：先收集完整候选并审核，再分片输出，首字延迟会上升。当前同步规划阶段不受 45 秒正文预算约束，仍使用既有模型请求超时；不能把它描述成整个请求的总超时。
- 规则和模型审核都可能误判、漏判，固定兜底也不等同人工处置；后台工具是否完成应以任务审计为准。

## ⑤ 固定评测与消融

数据与语料在评测前冻结，SHA-256 记录在 `app/evaluation/data/frozen-manifest.json`。如果标签、分块或语料改变，应创建新版本，不得覆盖本版本后继续比较旧成绩。

| 数据集 | 规模 | 检查内容 |
| --- | --- | --- |
| retrieval-v1 | 52 题：44 可回答、8 不可回答；11 文档、34 段 | 显式段落 qrels，固定 512/64 分块 |
| risk-v1 | 32 条 | 合成业务路由标签，含否定、引用、混合、延续和持续困扰 |
| dialogues-v1 | 8 个场景、13 轮 | 短期回忆、更正、岗位、危机延续、引用、他伤、睡眠和未知资源 |

这些是开发者编写的工程回归集，未经过独立临床专家标注或盲评；并不是独立分布外测试。代码实现过程中能看到标签，成绩不能代表真实学生人群的准确率。对话检查包含精确字符串/子串和路由约束，不能代表总体回答质量。

RAG 的 Recall@K = 命中相关段数 / 全部已标注相关段数；Precision@K 分母固定 K；MRR 使用首个相关段排名；NDCG 的 IDCG 来自全部 gold 的理想排序，重复段不能加分。没有 gold 的题不混入正例均值，单列“空检索率”，它也不是拒答正确率。当前 qrels 的相关性覆盖并不穷尽，precision 受标注覆盖影响。旧的 source/关键词命中率不能与新 Recall 直接比较。

```bash
# 单元回归和原六组工程 Harness
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner

# mock 验证流程；configured 才调用 .env 和角色配置中的真实模型
.venv/bin/python -m app.evaluation.runner --provider mock --output target/p1-mock.json
.venv/bin/python -m app.evaluation.runner --provider configured --architecture both --output target/p1-cloud.json

# 三种检索使用同一冻结集、独立临时库，查询时不展开相邻段
RAG_EVAL_OUTPUT=target/rag-bm25.json .venv/bin/python -m app.rag_eval.runner --mode bm25 --baseline app/rag_eval/data/baseline-bm25-v1.json
RAG_EVAL_OUTPUT=target/rag-rerank.json .venv/bin/python -m app.rag_eval.runner --mode bm25-rerank --baseline app/rag_eval/data/baseline-bm25-rerank-v1.json
RAG_EVAL_OUTPUT=target/rag-hybrid.json .venv/bin/python -m app.rag_eval.runner --mode hybrid
```

混合模式强制实际向量与 embedding 可用，不能偷偷降级 BM25 后报向量成绩。报告包含实际 embedding 请求次数、模型、语料和标签哈希。

架构对照使用相同角色配置、知识库、记忆服务和正文审核，比较固定顺序工作流与当前黑板调度。它不是单次 LLM 对照。固定顺序最多执行 `agent_max_rounds` 个阶段；事件系统最多调度同数轮、每轮受认领数限制，两者不是严格计算量匹配实验。报告分别给出调用次数、耗时、通过率；不能据此宣称多 Agent 必然优于单 Agent。

离线对话评测仅持久化工具计划，不启动 Worker、不发送邮件；线上工具链路另做真实 MySQL/Redis/Chroma/本地 Mailpit 冒烟。调用量只计请求尝试，不把它伪装成 token 或人民币成本。每次完整运行均保存结果；修复后复跑应保留之前失败，不能只挑最佳轮次。

本机实际验收证据位于 `.local-runtime/p1-improvements/`，包含升级前源码快照、主库备份、冻结清单、失败首轮及修复后的评测、真实基础设施验收。该目录含本机运行数据，不随发布包打包。
