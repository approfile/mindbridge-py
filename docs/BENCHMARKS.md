# 实测数据与复现

本文覆盖 MindBridge 的工程规模、RAG 评测结果、未命中案例分析、harness 阈值与复现命令；前置阅读：[RAG](./RAG.md) 的指标定义与 [QUALITY](./QUALITY.md) 的测试策略。所有数字均来自仓库内代码与数据文件，并可用本文命令复现。

---

## 1. 评测环境与口径

评测使用 `AI_PROVIDER=mock` 确定性桩模型、临时 SQLite 与内存版短期记忆，并强制 `KNOWLEDGE_VECTOR_ENABLED=false`（无 embedding API Key）。因此下表 RAG 指标对应**「自研 BM25 + 本地 `hybrid_score` 重排」的降级路径**，不是开启 Chroma 向量召回后的完整混合检索成绩。开启向量链路后的指标尚未在无 Key 环境实测，不应把基线数值当作其预期值。

---

## 2. 工程规模

| 指标 | 数值 |
| --- | --- |
| 后端 Python 模块 | 44 个（`app/` 下；全仓 53 个 `.py`，含 7 个测试文件与 2 个脚本） |
| 后端 Python 代码量 | 约 5.9k 行（`app/` 下 5,922 行） |
| 内置知识库 | 11 篇（校园心理总则、风险等级策略、焦虑恐慌、情绪低落、睡眠、学业压力、考试季、人际关系、新生适应、咨询转介、隐私边界） |
| 知识库切块（512 / 64） | 34 个 chunk |
| 标准 Skill | 7 个 |
| 多 Agent 角色 | 5 个（1 协调 + 4 专业） |
| 协作事件类型 | 13 种 |
| 产物类型 | 6 种（5 种核心产物 + 1 种审查产物 `critique`：`intent` / `risk` / `context` / `response_proposal` / `safety_review` / `critique`） |
| ORM 数据表 | 13 张 |
| HTTP 路由 | 20 条（其中 2 条 `async def`） |
| MCP 工具 | 6 个 |
| RAG 评测集 | 60 条 |
| 工程 Harness Suite | 6 组 |
| 单元测试 | 23 个用例（7 个测试文件） |

口径说明：旧文案中的后端 Python 文件数与实际不符，已按 `find` 实测改为 44（`app/`）/ 53（全仓）；产物类型按代码改为 6 种（含 `critique`）。

---

## 3. RAG 评测（60 条评测集，top-K = 4）

| 指标 | 实测值 |
| --- | --- |
| 评测集规模 | 60 条 |
| 知识库切块 | 34 个 chunk |
| `HitRate@4` | **0.9667**（58 / 60） |
| `Recall@4` | **0.9667** |
| `Precision@4` | **0.6458** |
| `MRR` | **0.9083** |
| `NDCG@4` | **0.9053** |
| 首个相关结果平均排名 | **1.1552** |

指标定义见 [RAG](./RAG.md) 第 6 节；相关性判定为 `expectedSources` 命中或 `expectedTerms` 命中（词长 ≥ 2）。

---

## 4. 未命中的 2 条

`support-anxiety-overwhelmed`、`support-risk-signals-human-help` 两条未命中。两者都是「多要素组合」问句（同时要求 grounding、呼吸、规律作息等多个词面线索）。初步分析是：纯 BM25 路径下它们被其他高词面重叠的 chunk 挤出了 top-4，属于词面检索的典型失败模式，也是引入向量召回的动机之一。

需要说明：审计只复现了命中 / 未命中的 ID 与分数，**未逐条导出 top-4 对照证明该归因**，因此这里把它标记为初步分析而非定论。要确认原因，需要补充 retriever 的候选输出对比。

---

## 5. Harness 阈值与结果

RAG suite 对指标做阈值断言，指标回退会被 CI 拦住：

```text
评测集 >= 50 条（实际 60）
hitRate   >= 0.95
recallAtK >= 0.95
MRR       >= 0.75
NDCG      >= 0.75
```

`python -m app.harness.runner` 在 mock 模型 + 临时 SQLite 环境下运行 6 组 suite（Risk Safety / Agent Routing / Standard Skills / RAG / API / Tool Queue），当前快照实测 **6/6 PASS**；任一组失败进程返回非 0。产物写入 `target/harness/harness-report.json` 与 `target/harness/rag-eval-report.json`。

`scripts/verify_offline.py` 在缺少 FastAPI / Redis / Chroma 的环境下另测出同一组 RAG 数字（33 项断言全过）：`knowledge chunks 34 · cases 60 · hitRate 0.9667 · MRR 0.9083 · NDCG 0.9053`。

---

## 6. 复现命令

```bash
python -m venv .venv && source .venv/bin/activate   # 完整 harness / 单测需要
pip install -r requirements.txt

python -m unittest discover -s tests          # 23 个单元测试
python -m app.harness.runner                  # 全部 6 组 harness
python -m app.harness.runner --suite rag      # 只跑 RAG 组（含阈值断言）
python -m app.harness.runner --json           # JSON 格式报告
python scripts/verify_offline.py              # 缺依赖环境下的 33 项离线断言
python scripts/demo_turn.py                   # 单轮多 Agent 协作演示（零外部服务）
```

RAG 数字的复现不依赖向量链路：`--suite rag` 与 `verify_offline.py` 都在 `KNOWLEDGE_VECTOR_ENABLED=false` 下运行。若要在完整依赖下复测向量路径，需要配置 `OPENAI_EMBEDDING_API_KEY`（或 `OPENAI_API_KEY`）并安装 `chromadb`，再对比两组结果；两组结果应分别登记，不能混用。

---

## 相关文档

- [RAG](./RAG.md)：检索链路、指标定义与降级
- [QUALITY](./QUALITY.md)：harness、单测、CI 与缺陷审计
- [ARCHITECTURE](./ARCHITECTURE.md)：工程规模对应的结构
- [LIMITATIONS](./LIMITATIONS.md)：评测覆盖面与检索质量边界
- [DEVELOPMENT](./DEVELOPMENT.md)：本地环境与运行方式
- [DESIGN-NOTES](./DESIGN-NOTES.md)：指标取舍背后的设计决策
