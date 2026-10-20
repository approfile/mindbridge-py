# 检索增强生成（RAG）

本文覆盖 MindBridge 的路由式检索触发条件、查询改写、向量 + BM25 混合召回、分数归一化与融合、确定性重排公式及取舍、上下文扩展、三层降级、知识库与评测指标定义；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md) 的 Runtime 与数据模型一节。实测数值见 [BENCHMARKS](./BENCHMARKS.md)。

![MindBridge 路由式 RAG：混合检索链路与三层降级](./rag-pipeline.svg)

*图 1：路由式 RAG。普通聊天不检索，咨询 / 风险才进入「改写 → 向量 + BM25 召回 → 归一化 → 加权融合 → 确定性重排 → 邻接扩展」；降级路径显式暴露在 `/api/admin/knowledge/status`。*

---

## 1. 为什么是「路由式 RAG」

普通聊天（例如「解释一下 Python 字典推导式」）检索校园心理知识库只会引入噪声并增加延迟。因此检索只在下面条件成立时执行：

```text
intent ∈ {CONSULT, RISK}  或  risk ∈ {MEDIUM, HIGH}
```

触发点位于 `ContextAgent.decide()` / `ContextAgent.act()`（`app/agents/autonomous.py`）与协调者的 `_derive_missing_work()`（`app/agents/coordinator.py`）——不是 `harness.py`。这也被工程 harness 固化为断言：普通聊天 `retrieved_knowledge == 0`，咨询 / 风险必须 `> 0`。

---

## 2. 混合检索链路

```text
query
 ├─ 查询改写      ContextAgent._rewrite_query()  用 LLM 改写成适合知识库的中文检索词（截断 60 字）
 ├─ 向量召回      ChromaKnowledgeStore            cosine 空间，score = 1 / (1 + distance)
 ├─ BM25 召回     自研实现，k1=1.5, b=0.75        英文词 + 汉字单字 + 汉字 bigram 混合分词
 ├─ 分数归一化    normalize_scores()             按来源各自 min-max
 ├─ 加权融合      (向量 × 0.65 + BM25 × 0.35) / 总权重；向量为空时向量权重自动归零
 ├─ 本地重排      rerank_score()                 base×0.55 + 词面相似×0.25 + 覆盖率×0.15 + 短语×0.05
 └─ 上下文扩展    _expand_best()                 对 top-1 拼接同源相邻 chunk（±1）
```

### 2.1 各段参数与实现

| 阶段 | 关键实现 | 参数 / 行为 |
| --- | --- | --- |
| 查询改写 | `ContextAgent._rewrite_query()` 调 LLM，异常时回退为原始输入 | 结果截断 60 字 |
| 向量召回 | Chroma `hnsw:space=cosine`，距离转分数 `1/(1+d)` | 候选数 `KNOWLEDGE_CANDIDATE_K=16` |
| BM25 召回 | 自研 `bm25_scores()`：idf、词频饱和、文档长度归一 | `k1=1.5`、`b=0.75`；分词为英文/数字词 + 汉字单字 + 汉字二元组 |
| 归一化 | 两个来源分别 min-max；全等分数时正例置 1 | `normalize_scores()` |
| 融合 | 加权平均后按分数排序，取 `candidate_k` | 默认向量 0.65 / BM25 0.35；向量候选为空时向量权重强制 0 |
| 重排 | 确定性公式，可选关闭（`KNOWLEDGE_RERANK_ENABLED`） | 见下节公式 |
| 扩展 | 只扩 top-1：把同源的 `source_index ±1` chunk 拼进正文 | 结果数仍保持 top-K |

检索主流程 `KnowledgeService.retrieve()`：先分别取向量候选与 BM25 候选（各 `candidate_k`），再走 `_fuse_and_rerank()`，最后 `_expand_best()`。`knowledge_top_k` 默认 4。

实现补充：

- 候选数 `candidate_k = max(top_k, KNOWLEDGE_CANDIDATE_K=16)`；融合后先截到 `candidate_k` 再重排取 `top_k`；
- BM25 每次检索从数据库全量载入 chunk（当前 34 个），规模上量后需要倒排索引与缓存；
- `SearchResult` 携带 `chunk_id` / `source` / `content` / `score`，扩展阶段按同源 `source_index ±1` 找回邻居。

---

## 3. 确定性重排

重排**不是**交叉编码器模型，而是显式公式（`app/services/knowledge.py`）：

```text
rerank_score = base_score × 0.55
             + lexical      × 0.25     # hybrid_score = token cosine × 0.75 + keyword × 0.25
             + coverage     × 0.15     # 查询 token 覆盖率
             + phrase       × 0.05     # 查询整体作为短语命中则 1.0，否则回退 keyword 分数
```

**取舍**：

- **规模**：校园心理知识库只有 11 篇、34 个 chunk（512/64 切块），几十到几百量级的候选引入重排模型，收益不足以抵消部署成本与推理延迟；
- **可解释**：每一分的来源都能拆开（融合分、词面重叠、覆盖率、短语命中）；
- **可回归**：确定性公式能在评测集上稳定复现，不会因重排模型换版本导致分数漂移——安全敏感场景里这比多挣一点 NDCG 重要。

代价同样明确：公式无法理解语义相似但用词完全不同的表述，知识库规模或语言复杂度上升后需要替换为学习型重排。公式中的常量都留有语义，便于按评测集调参。

可对比性：设置 `KNOWLEDGE_RERANK_ENABLED=false` 会跳过重排、直接截断融合结果，便于在评测集上观察公式带来的边际收益。

---

## 4. 三层降级（四类失败时机）

演示环境与生产环境的依赖可用性差异很大，检索链路因此逐层降级，且**降级行为显式暴露**而不静默：

| 层级 | 触发条件 | 行为 |
| --- | --- | --- |
| 构造期软降级 | 缺 `OPENAI_API_KEY` / embedding Key，或 `chromadb` 未安装 | `can_embed=False`，记录可读原因 |
| 构造期硬失败 | 上述情况 + `KNOWLEDGE_VECTOR_REQUIRED=true` | 抛 `VectorStoreUnavailable`，**快速失败**（验收要求走向量链路时使用） |
| 检索期降级 | embedding / Chroma 调用异常 | `KNOWLEDGE_VECTOR_REQUIRED=false` → 记 warning 并退回纯 BM25；`=true` → 原样抛出 |
| 融合期降级 | 向量候选为空 | 向量权重自动归零，等价于纯 BM25 + 本地重排 |

当前实际链路可通过 `GET /api/admin/knowledge/status` 查看，返回 `primaryRetrieval`、`fallbackRetrieval`、`vectorAvailable`、`vectorError`、`databaseChunks`、`hybridVectorWeight` 等字段。注意：该接口只暴露检索链路状态，**不返回 skill 状态**（skill 就绪状态只在 `GET /api/agent/status` 返回）。

---

## 5. 知识库与向量缓存

- 内置知识库 11 篇 Markdown，位于 `app/knowledge/`，启动时由 `seed_data` 逐篇 `ensure_source` 入库；
- 切块参数 `KNOWLEDGE_CHUNK_SIZE=512`、`KNOWLEDGE_CHUNK_OVERLAP=64`，实测切块数为 **34 个 chunk**；
- 向量通过 OpenAI 兼容 `/embeddings` 接口批量调用，**每批 20 条**，超时 `EMBEDDING_TIMEOUT_SECONDS=30`；
- `knowledge_chunks.embedding_json` 缓存已算出的向量，`_embeddings_for_chunks()` 只对缺失项重新请求，因此临时故障恢复后不需要全量重建；
- Chroma 持久化目录 `data/chroma`，每次 upsert 同时生成快照到 `data/chroma-snapshots`，默认保留最近 5 份，便于回滚到上一个可用状态；
- 管理员可通过 `POST /api/admin/knowledge`（文本）、`POST /api/admin/knowledge/file`（md/txt/pdf）、`POST /api/admin/knowledge/rebuild-vector`（全量重建）、`POST /api/admin/knowledge/backup`（生成快照）维护知识库。

---

## 6. 评测指标定义

`app/rag_eval/runner.py` 在 60 条人工构造的评测集上计算五个指标（top-K = 4）：

| 指标 | 定义 |
| --- | --- |
| `HitRate@K` | top-K 中是否至少有一条相关 |
| `Recall@K` | 命中即 1.0 的二值近似（非分级召回） |
| `Precision@K` | 相关条数 / K |
| `MRR` | 1 / 首个相关结果的排名 |
| `NDCG@K` | 二值增益的归一化折损累计增益 |

相关性判定为 `expectedSources` 命中 **或** `expectedTerms` 命中（词长 ≥ 2）。评测集覆盖风险等级判定、焦虑 / 低落 / 孤独 / 转介、咨询边界三类主题。实际数值、未命中案例分析、harness 阈值与复现命令见 [BENCHMARKS](./BENCHMARKS.md)。

需要注意的口径：当前 harness 与离线验证脚本都在**向量检索关闭**的环境下运行（无 embedding Key），因此公开的 RAG 指标对应「BM25 + 本地重排」降级路径；开启 Chroma 向量召回后的指标尚未实测，不应把基线数值当作完整混合检索的成绩单。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：ContextAgent 在 runtime 中的位置
- [BENCHMARKS](./BENCHMARKS.md)：RAG 实测数据、阈值与复现方法
- [SAFETY](./SAFETY.md)：检索证据进入回复前的安全约束
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：上下文组装与 skill 注入
- [API](./API.md)：知识库管理接口与 `knowledge/status`
- [QUALITY](./QUALITY.md)：RAG harness 的断言方式
- [LIMITATIONS](./LIMITATIONS.md)：检索质量边界与改进方向
- [DESIGN-NOTES](./DESIGN-NOTES.md)：D8–D9 等检索设计取舍
