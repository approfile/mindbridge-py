# 当前 RAG

最近真实实验配置：11 篇知识文档，34 个 chunk；512 字符切块、64 字符重叠（不是 token）；qwen3.7-text-embedding，1024 维；Chroma cosine，候选 K=16，最终 TopK=4。

查询先改写，再分别取向量与 BM25 候选。BM25 使用本地分词及词项统计，不调用 embedding 模型。两路候选按 chunk ID 求并集；归一化各路分数，缺失分量按实现处理，再以向量 0.65 / BM25 0.35 融合，随后本地词面重排。该重排不是训练过的 cross-encoder。线上可做邻居扩展；检索实验固定 expand=False。

向量不可用且 KNOWLEDGE_VECTOR_REQUIRED=false 时回退 BM25 与词面评分。显式改写失败保留 degraded/reason；2026-10-31 起正常查询不再截取前 60 字符，默认 1024 字符；模型超长输出显式回退，超长原输入按首尾窗口选取并记录省略。生成/审核也增加逐句来源约束，见 [本轮改进](p0p1-hardening-20261031.md)。[日志与降级](observability-20261025.md)。

34 块小集合在 Chroma 0.5.23 的默认 batch 条件下可能仍处于 brute-force buffer，不能据此宣称 HNSW/ANN 的大库性能。索引大小、图参数与吞吐应以实际集合状态测量。

[真实向量消融](../target/vector-vs-hybrid/REPORT.md)与[生成侧 RAGAS](agent-layered-evaluation-v1.md)分别报告。当前 52 题检索分数不沿用旧 60 题结果；ContextRecall 也不与 Recall@4 混称。可配置 OpenAI-compatible embedding，不要求生成与向量模型来自同一供应商。
