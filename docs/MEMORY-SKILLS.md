# 分层记忆与 Skill 体系

本文覆盖 MindBridge 的三层记忆（MySQL 全量历史 / Redis 短期窗口 / Agent 私有记忆）、Redis 回填、上下文压缩参数、Skill 的加载校验与注入方式；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md) 的 Runtime 与数据模型一节，[SAFETY](./SAFETY.md) 的输入脱敏一节。

---

## 1. 三层记忆

长对话会拖慢推理、抬高成本，并且会把早期无关内容带进心理支持语境。这里采用三层记忆：

| 层 | 存储 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| 完整历史 | MySQL `chat_messages` | 原文全量 | 永久 |
| 短期窗口 | Redis List `mindbridge:short-term-memory:{sessionId}` | 最近 `REDIS_MEMORY_MAX_MESSAGES=40` 条（写入前脱敏） | TTL `REDIS_MEMORY_TTL_SECONDS=86400`，每次追加续期 |
| Agent 私有记忆 | Redis `agent:{AgentName}:{sessionId}` | 各 Agent 自己的中间结论（意图历史、风险台账、审查结论） | 同上 |

约束与降级：

- 短期窗口写入时 `RedisShortTermMemoryStore._serialize()` 脱敏，读出时 `_read()` 再脱敏一次，避免历史数据绕过脱敏（见 [SAFETY](./SAFETY.md)）；
- Redis 不可达或未安装 `redis` 依赖时，`RedisShortTermMemoryStore` 返回空历史并记录 warning，请求不会失败——只是失去短期记忆；
- 每个 Agent 的私有记忆 key 独立，`AgentProfile.memory_policy` 标记用途（如 `private_intent_history`、`private_safety_ledger`）；`ResponseAgent` 组装 prompt 时私有记忆只取最近 5 条。

---

## 2. Redis 回填

Redis 未命中时，`ContextAgent._load_history()` 从 MySQL 按 `created_at desc` 取最近 `REDIS_MEMORY_MAX_MESSAGES` 条，反转成时间正序后 `replace()` 回填 Redis：

```text
history = redis.load_recent(sessionId)
if not history:
    rows = MySQL.query(chat_messages).filter(session_id).order_by(created_at desc).limit(40)
    rows.reverse()
    history = memory.messages_from_rows(rows)   # 读出时脱敏
    redis.replace(sessionId, history)
```

因此 Redis 重启或长期未使用不会丢历史，只是多一次数据库读。回填路径的脱敏是「读出后」补的一道保险。

---

## 3. 上下文压缩

历史超过阈值时，`compact_history_for_prompt()`（`app/services/memory.py`）把历史压成「一条系统摘要消息 + 最近 8 条」：

```text
history 长度 ≤ MEMORY_COMPACTION_RECENT_MESSAGES(8)  →  原样使用
否则  →  [ 系统摘要消息, *最近 8 条 ]
摘要 = 学生近期关注(user 末 4 条, 各 80 字)
     + 已给过的支持(assistant 末 3 条, 各 70 字)
     + 本轮输入关注(80 字)                 总长 ≤ MEMORY_SUMMARY_MAX_CHARS(500)
```

实现细节：

- `MEMORY_COMPACTION_ENABLED=true` 默认开启，最近窗口 `MEMORY_COMPACTION_RECENT_MESSAGES=8`，摘要上限 `MEMORY_SUMMARY_MAX_CHARS=500`；
- 确定性摘要是**兜底路径**；`ContextAgent._summarize_memory()` 会先尝试用 LLM 生成 1–3 条记忆要点（上限 500 字），失败或为空时回退到确定性摘要；
- 摘要消息带显式作用域声明：「仅供 MindBridge 内部上下文使用；不要向学生展示；不要据此输出诊断、风险等级或后台标签」。被压缩掉的恰恰可能是风险信息，必须防止摘要被模型回显给学生；
- 进入模型的窗口另有硬上限：`_bounded_model_history()` 限制为 `chat_history_limit × 2 = 20` 条，且若首条是 system 消息则保留它。

代价同样清楚：压缩是有损的，摘要是字符串拼接而非结构化记忆；这是当前「控制延迟与成本」的取舍，不是语义记忆方案。

---

## 4. Skill 体系

`skills/*/SKILL.md` 以 YAML front-matter + Markdown 正文描述一类专业处置规范，由 `MindBridgeSkillRegistry` 在运行时从文件系统加载（Docker 镜像中也必须 `COPY skills`，否则 skill 列表为空）：

```markdown
---
name: high_risk_safety_plan
description: 高风险场景下优先完成短期安全计划的回复规范
---
## Workflow
...
```

### 4.1 七个标准 Skill

| Skill | 触发场景 |
| --- | --- |
| `supportive_response_baseline` | 所有心理咨询 / 风险回复的基础共情与边界规则 |
| `high_risk_safety_plan` | 高风险：优先完成短期安全计划 |
| `anxiety_grounding_support` | 焦虑、惊恐、崩溃的稳定化与 grounding |
| `sleep_routine_support` | 失眠、睡眠节律紊乱 |
| `academic_stress_planning` | 考试、作业、论文、绩点压力 |
| `referral_resource_guidance` | 校内心理中心、辅导员、紧急资源转介 |
| `counselor_handoff_summary` | 生成给辅导员看的个案交接摘要（含 ` ```text ` 模板） |

### 4.2 加载即校验

`MindBridgeSkill.validation_issues()` 在加载时逐项检查：

| 检查项 | 不通过时的状态 |
| --- | --- |
| 目录名与 front-matter `name` 一致 | `WARN` |
| 正文包含 `## Workflow` 小节 | `WARN` |
| `description` 长度 ≥ 20 | `WARN` |
| `counselor_handoff_summary` 提供 ` ```text ` 模板 | `ERROR`（`FAILED`） |
| 文件缺失 front-matter 或 description 为空 | 加载异常（`FAILED`） |

每个 skill 的 `READY / WARN / FAILED` 与问题列表通过 **`GET /api/agent/status`** 的 `skills` 字段暴露；`/api/admin/knowledge/status` 只返回检索链路状态，不包含 skill。工程 harness 的 Standard Skills 组会断言 7 个标准 skill 全部 `READY`。

### 4.3 选择与注入

- **选择**：`response_skill_names(intent, risk, text)` 决定候选集合。CHAT 返回空；HIGH 风险**固定**返回 `["supportive_response_baseline", "high_risk_safety_plan"]`（不走关键词匹配，避免漏选）；其余支持路径以 `supportive_response_baseline` + `referral_resource_guidance` 为基线，再按文本关键词追加焦虑 / 睡眠 / 学业 skill；
- **注入**：选中的 skill 正文以 `应用 skill: {name}\n{body}` 拼接，作为 `ResponseAgent` 回复用 system prompt 的「可用 skill 指引」段落（`MindBridgeSkill.prompt_context()`）；
- **交接摘要**：`counselor_handoff_summary` 的 `text` 模板由 `counselor_handoff_summary(report, user)` 渲染，包含报告 ID、学生标识、风险等级、情绪、置信度、摘要、下一步建议与原文节选（截断 700 字），用于个案创建时的辅导员交接。

---

## 5. 已知边界

| 边界 | 现状 | 改进方向 |
| --- | --- | --- |
| 记忆语义 | 摘要是确定性字符串拼接，不是结构化语义记忆 | 引入结构化事实槽位与可检索的长期记忆 |
| 压缩触发 | 按消息条数触发，不按 token 预算 | 改为 token 级预算控制，避免长消息被过度压缩 |
| 私有记忆 | 各 Agent 独立 key，但共享同一 Redis List 语义 | 增加作用域与过期策略，防止跨会话串读 |
| Skill 校验 | 格式校验为主，不校验内容质量 | 增加安全内容审查与版本化 skill 清单 |

完整边界清单见 [LIMITATIONS](./LIMITATIONS.md)；相关设计决策见 [DESIGN-NOTES](./DESIGN-NOTES.md) 的 D10、D13。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：Agent Runtime 与数据模型
- [SAFETY](./SAFETY.md)：输入脱敏、元数据隔离与安全审查
- [RAG](./RAG.md)：检索证据与上下文组装
- [API](./API.md)：`agent/status` 与 `knowledge/status`
- [QUALITY](./QUALITY.md)：Standard Skills harness 的断言
- [BENCHMARKS](./BENCHMARKS.md)：规模数字与复现命令
- [LIMITATIONS](./LIMITATIONS.md)：记忆与 Skill 的边界
- [DESIGN-NOTES](./DESIGN-NOTES.md)：记忆压缩与 Skill 选择的设计取舍
