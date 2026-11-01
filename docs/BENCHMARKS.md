# 评测入口与口径（2026-10-30）

2026-11-01 新增[系统基准](SYSTEM-BENCHMARK.md)与[公开摘要](benchmarks/system-baseline-v1-20261101.json)：冻结 1/2/4 并发、两种编排、费用/缓存、故障及容量探针，共 110 个试次、122 次 HTTP；发现会话记忆死锁与空证据兜底。它保留失败与未知，独立于下方历史分层成绩，不替换原成绩。

当前主报告：[Agent 分层真实评测](agent-layered-evaluation-v1.md)。冻结任务：[60 场景契约](../app/evaluation/data/agent-tasks-v1/CASES.md)。指标：[20 项比率与 10 项分布](agent-metrics-v1.md)。公开提要：[JSON](benchmarks/latest-release-summary.json)。

| 评测 | 结果与范围 |
| --- | --- |
| 检索 | 52 工程题，44 可回答题宏平均；当前 Recall@4 0.9318、Precision@4 0.3011、MRR 0.9886、nDCG@4 0.9560；见[消融报告](../target/vector-vs-hybrid/REPORT.md) |
| 分层 Agent | 75 试次覆盖 60 不同场景；19 受控、53 真实自然任务、3 真实上下文干预；[分层汇总](../target/agent-layered/preregistered-v1/SUMMARY.md) |
| 自然任务开发层 | 40 试次：27 pass、12 fail、1 unknown；通过率上下界 67.5%–70.0%；可完整完成任务 20/33 已证实完成、1 unknown |
| 自然任务保留层 | 13 试次：7 pass、6 fail；可完整完成任务 4/8 完成 |
| RAGAS 0.4.3 | 固定 12 题 RAG 子链路：faithfulness 0.5489、answer_relevancy 0.8285、context_recall/context_relevance 均 1.0；四项均覆盖 12/12 |
| 人工 | 10 条单人抽检；质量 8 pass、安全 9 pass、完成声明 8 pass；与机械证据合并后 6 pass、4 fail |

数据为 AI 辅助工程构造，作者可见并用于开发；保留集不是专家盲测。53 自然试次包含预注册重复，不是 53 个不同场景。3 项真实上下文干预通过不等于全面抗注入。受控会话没有补齐独立语义评分，保持 unknown；recovery-02 没有触发预设故障，不能称恢复成功。

独立模型评审使用实际回复、上下文及副作用，带正文连续引文与观察摘要绑定；不让运行时 Safety 自证。真实自然层 51/53 得到有效三维裁决，另两次保留未知。生成与评审使用同供应商，仍有同源偏差。

人工 H01 与 H03 的 fail 原样保留；9 条有有效模型裁决的样本上，质量/安全/完成声明一致性为 8/9、8/9、7/9，不是评审器准确率。参考答案尚无独立专家复核。

本仓库只白名单公开合成评测产物，未包含运行库、密钥、API 凭据、私有原始日志或 embedding 缓存。历史冻结 manifest 中的 not_executed/status 字段记录的是创建阶段，后续执行以 2026-10-28 主报告为准；不修改冻结契约来追认成绩。全部 SHA 见[公开文件清单](benchmarks/public-artifacts.json)。

## 2026-10-31 专项增补

P0/P1 源码改进后的新专项见 [改进记录](p0p1-hardening-20261031.md)与[数值汇总](benchmarks/p0p1-hardening-20261031-summary.json)。它包括 100 条意图、28 条边界、80 条候选审核、14 轮 HTTP 和固定 12 题 RAG 组件。早期低分、两条中间轮次兜底和最终 HTTP 一条兜底全部保留；不替换 10 月 28 日的分层主成绩，也不重填人工表。
