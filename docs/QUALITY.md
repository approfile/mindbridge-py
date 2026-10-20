# 工程质量与测试

本文覆盖 MindBridge 的测试策略、6 组工程 Harness、23 个单元测试、CI workflow、`verify_offline.py` 与 `demo_turn.py`，以及首个公开版本前的缺陷审计记录；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md)。实测数据见 [BENCHMARKS](./BENCHMARKS.md)。

---

## 1. 测试策略：把不确定性关在边界外

Agent 系统的输出不是确定文本，无法用快照比对。这里的原则是**把不确定性关在边界外，把确定性行为写成断言**：

| 不确定性来源 | 边界替换 |
| --- | --- |
| 模型输出 | `AI_PROVIDER=mock` 的确定性桩模型 |
| 数据库 | 每组 suite 用临时 SQLite，独立 drop/recreate schema 并重新播种 |
| Redis | 内存实现替换 `RedisShortTermMemoryStore` |
| 向量检索 | 强制 `KNOWLEDGE_VECTOR_ENABLED=false`，走 BM25 + 本地重排 |

因此 6 组 harness 与单元测试都不需要外部服务或 API Key。只要有一组 suite 失败，runner 返回非零退出码，可以直接接 CI。

---

## 2. 六组工程 Harness

入口：

```bash
python -m app.harness.runner                      # 全部 6 组
python -m app.harness.runner --suite rag          # 单组：risk | routing | skills | rag | api | tool-queue
python -m app.harness.runner --json
```

| Suite | 断言要点 |
| --- | --- |
| Risk Safety | 中/英高风险、咨询、普通聊天四类输入：meta / done 事件齐全、有内容输出、报告生成符合预期、风险等级正确、Excel 任务入队、高危额外产生个案与预警任务；**并断言学生可见 SSE 输出不含** `["风险等级", "报告ID", "emotionScore", "HIGH_RISK"]` |
| Agent Routing | 三档意图与风险；普通聊天必须运行 Understanding/Safety/Response 且**不得**出现 `ContextAgent`、检索数为 0；咨询/风险必须运行 ContextAgent 且检索数 > 0 |
| Standard Skills | 7 个标准 skill 齐全且全部 `READY`、状态项暴露 `SKILL.md` 路径；咨询场景选中基线 + 转介 + 焦虑/睡眠/学业 skill；高危固定选中 `[supportive_response_baseline, high_risk_safety_plan]`；注入格式含 `应用 skill: ...`；交接摘要模板渲染出报告 ID、学生标识、「立即跟进」等要素 |
| RAG | 评测集 ≥ 50 条（实际 60），`hitRate ≥ 0.95`、`recallAtK ≥ 0.95`、`MRR ≥ 0.75`、`NDCG ≥ 0.75`，并写出 `target/harness/rag-eval-report.json` |
| API | 健康检查、学生 profile、`agent/status` 暴露 ≥ 7 个 skill 路径、管理员发起聊天 403、学生读管理接口 403、管理员读报告 200、SSE 含 meta/done、文本入库与 `knowledge/status` 正常 |
| Tool Queue | HIGH 报告恰好 3 个任务且预警 `depends_on_job_id` 指向个案任务；依赖未就绪时不可执行；Excel 与个案写入幂等；个案 SUCCESS 后依赖就绪；预警发送后个案进入 `ALERT_SENT`；限流器拒绝第二次请求并给出 `retry_after`；达到最大尝试次数的任务转 `DEAD` 且写死信 |

![MindBridge 异步工具队列：依赖门闸、重试与死信](./tool-queue.svg)

*图 1：Tool Queue harness 覆盖的完整路径。高风险报告触发台账 / 个案 / 预警三类工具，依赖门闸在每次执行时校验；失败按线性退避重试（15s / 30s，第 3 次失败转 DEAD），预警有 60 秒滑窗限流，每次执行写审计。*

产物：`target/harness/harness-report.json` 与 `target/harness/rag-eval-report.json`。

---

## 3. 单元测试（23 个用例）

使用标准库 `unittest`，无 pytest 依赖；由 CI 单独执行，harness 是另一套独立的工程校验（harness 不调用 unittest）：

```bash
python -m unittest discover -s tests
```

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_admin_api_routes.py` | 用 `TestClient` 真实请求 3 条管理路由，覆盖 200 / 403 / 404 分支 |
| `tests/test_event_driven_multi_agent.py` | 认领写回黑板、`claimed_by` 与 `TASK_CLAIMED` 事件 metadata 一致性、最终采纳门闸 |
| `tests/test_memory_compaction.py` | 压缩阈值、摘要构成、有界模型历史 |
| `tests/test_privacy_and_assessment.py` | 脱敏正则；用「一调用即抛异常」的 AI stub 证明高危词命中时不调用模型 |
| `tests/test_skills.py` | skill 加载与校验规则 |
| `tests/test_tool_governance.py` | 工具风险范围策略 |
| `tests/test_tool_governance_audit.py` | 策略拦截与审计写入 |

---

## 4. CI workflow

`.github/workflows/test.yml` 只在 **push 到 `main`** 或 **对 `main` 的 PR** 时触发，步骤为：

```text
checkout → setup-python 3.12 → pip install -r requirements.txt
        → mkdir -p target data          # SQLite 无法在不存在目录中建库
        → python -m compileall -q app tests scripts
        → python -m unittest discover -s tests -v   (env: mock / sqlite / vector=false)
        → python -m app.harness.runner               (env: mock)
        → upload-artifact: target/test-output.txt / target/harness-output.txt / target/harness/
```

因为 harness 使用 mock provider、临时 SQLite 与内存短期记忆，CI 不需要任何外部服务或 API Key。注意：CI 通过只说明**被断言的路径**是好的；未被测试覆盖的路由不会因此变得可用（见第 6 节）。

---

## 5. 离线验证与演示脚本

### `scripts/verify_offline.py`

在缺少 FastAPI / Redis / Chroma / mcp 等依赖的环境下，用最小桩模块跑通真实代码路径，**33 项断言**：

```text
1) ReportService admin methods are real class attributes         4 passed
2) EventDrivenCoordinator writes the claim back to the blackboard 8 passed
3) ToolPolicyRegistry risk scoping and audit writes              10 passed
4) AgentTask.claim() merge semantics                              4 passed
5) RAG evaluation (BM25 + local reranker degradation path)         7 passed
   knowledge chunks 34 · cases 60 · hitRate 0.9667 · MRR 0.9083 · NDCG 0.9053
```

脚本通过 stub 掉 `fastapi`、`redis`、`pypdf`、`pymysql`、`chromadb`、`mcp`、`uvicorn` 等模块，使真实业务代码（黑板认领写回、策略门闸与审计落库、`ReportService` 管理方法、60 条评测集检索）可以在受限环境中运行。运行产物写入 `.verify-tmp/`，已被 `.gitignore` 覆盖。完整 harness 与单元测试仍需先 `pip install -r requirements.txt`。

### `scripts/demo_turn.py`

零外部依赖跑三个代表性场景（普通聊天 / 心理倾诉 / 高风险），打印每个场景的完整协作事件流：谁在什么时候认领了哪个任务、发布了什么产物、安全审查是否通过、最终采纳了哪一版回复。使用 mock 模型、临时 SQLite 与内存记忆，不需要起服务：

```bash
python scripts/demo_turn.py
python -m scripts.demo_turn --message "我最近压力很大，睡不着"
```

---

## 6. 交付前缺陷审计

在首个公开版本前，按「路由维度 + 数据契约」对链路做了一次系统审计，发现并修复 3 类真实缺陷，并为每类补上回归测试（当前 23 个单测 + 6 组 harness 全部通过）：

| 缺陷 | 影响面 | 根因 | 修复 | 回归测试 |
| --- | --- | --- | --- | --- |
| 3 个管理接口恒返回 500 | 管理端会话 / trace / 审计接口不可用 | `app/services/report.py` 中三个方法缩进错误，被定义在模块级而非 `ReportService` 类内，实例上取不到属性 | 移回 `ReportService` 类内 | `tests/test_admin_api_routes.py`（含 403/404 分支） |
| 任务认领从未落盘 | `AgentTask.status` 不出现 `CLAIMED`，`claimed_by` 恒空，trace 的 `claimedBy` 恒为 `[]` | 认领结果虽写回黑板，但随后 `act()` / `apply_turn_result()` 使用的是未认领的旧实例，导致 CLAIMED 状态与 `claimed_by` 被旧值覆盖丢失 | 认领后的实例传入后续调用，并把 `claimedBy` 写入 `TASK_CLAIMED` 事件 metadata | 断言黑板 `claimed_by` 与事件 metadata 一致 |
| 工具策略与审计未接线 | `tool_audit_records` 恒为空，「低风险不应触发预警」没有执行点 | `ToolPolicyRegistry` / `ToolGovernanceService` 已完整实现但从未被调用 | 队列 worker 执行前 `require_allowed()` 做策略门闸、`start_job()` / `finish()` 写审计 | `tests/test_tool_governance_audit.py` |

同批修复的其他工程问题：

| 问题 | 修复 |
| --- | --- |
| `config.py` 中 `openai_embedding_base_url` / `openai_embedding_api_key` 被重复定义两次 | 删除重复定义（后者覆盖前者会让「第二个变量名」静默失效） |
| Dockerfile 未复制 `skills/`，容器内 skill 列表为空 | 补 `COPY skills ./skills` |
| docker-compose 用 `mysql:8.0` 但文档写 8.4 | 统一为 `mysql:8.4` |
| docker-compose 未透传 SMTP 变量，生产无法从 compose 注入发信配置 | 补全 SMTP / 收件人透传 |
| `.env.example` 中 `AGENT_FRAMEWORK=langgraph` 误导（该值只被识别为 fallback） | 改为实际支持的值并说明 |

这次审计沉淀下的经验：**「被断言的行为」才是可持续回归的资产**。管理接口的 500 之所以长期存在，是因为没有一条按路由维度的测试真正请求过它；认领字段的丢失之所以安静，是因为功能链路看起来完全正常，只有对比「数据应该长什么样」与「实际长什么样」才能发现。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：被测试的分层与 Runtime 结构
- [SAFETY](./SAFETY.md)：安全断言与审查链路
- [BENCHMARKS](./BENCHMARKS.md)：harness 实测数据与复现
- [API](./API.md)：API harness 覆盖的路由与权限
- [RAG](./RAG.md)：检索指标定义
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：Standard Skills harness
- [DEVELOPMENT](./DEVELOPMENT.md)：本地运行 harness 与单测
- [LIMITATIONS](./LIMITATIONS.md)：覆盖率、lint 与可观测性边界
