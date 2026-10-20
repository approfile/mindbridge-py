# HTTP API

本文覆盖 MindBridge 的认证模型、20 条路由、SSE 事件格式、调用示例、错误码约定与管理端安全边界；前置阅读：[ARCHITECTURE](./ARCHITECTURE.md) 的端到端链路一节。路由与参数以 `app/api/routes.py`、`app/core/security.py` 为准。

---

## 1. 认证模型

认证为 **HTTP Basic Auth**（`Authorization: Basic base64(username:password)`），角色为 `ROLE_USER` / `ROLE_ADMIN`：

| 规则 | 行为 |
| --- | --- |
| 缺少或格式错误的 `Authorization` | `401`（`Missing / Invalid Basic authorization`） |
| 用户名不存在或口令错误 | `401`（`Bad credentials`） |
| 学生访问 `/api/admin/*` | `403`（`Admin role required`） |
| 管理员发起学生对话 `/api/chat/stream` | `403`（管理员账号只查看后台记录，不能发起学生对话） |
| 其余登录接口 | 任意已认证角色可访问（`/api/profile`、`/api/agent/status`、`/api/reports/me`） |

启动时 `seed_data` 写入两个演示账号：`student / student123`（`ROLE_USER`）与 `admin / admin123`（`ROLE_ADMIN` + `ROLE_USER`）。口令使用 SHA-256 无盐哈希；这是演示级实现，生产部署应替换为 OIDC/JWT 与 bcrypt/argon2（见 [LIMITATIONS](./LIMITATIONS.md)）。

## 2. 路由总表（20 条）

仅第 3、20 条是 `async def`，其余 18 条为同步 `def`（FastAPI 放入线程池）。

| # | 方法 | 路径 | 权限 | 用途 | 同步/异步 |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `/actuator/health` | 公开 | 健康检查 | 同步 |
| 2 | GET | `/api/profile` | 登录 | 当前身份与角色 | 同步 |
| 3 | POST | `/api/chat/stream` | 学生 | SSE 流式对话 | **异步** |
| 4 | GET | `/api/agent/status` | 登录 | 模型 provider、runtime 配置、5 个 Agent、Skill 就绪状态 | 同步 |
| 5 | GET | `/api/reports/me` | 登录 | 自己的报告历史 | 同步 |
| 6 | GET | `/api/admin/reports` | 管理员 | 全部心理风险与咨询报告 | 同步 |
| 7 | GET | `/api/admin/excel-records` | 管理员 | Excel 台账记录 | 同步 |
| 8 | GET | `/api/admin/alerts` | 管理员 | 预警记录 | 同步 |
| 9 | GET | `/api/admin/cases` | 管理员 | 风险个案 | 同步 |
| 10 | GET | `/api/admin/cases/{caseId}/notes` | 管理员 | 个案备注 | 同步 |
| 11 | GET | `/api/admin/tool-jobs` | 管理员 | 工具任务 | 同步 |
| 12 | GET | `/api/admin/dead-letters` | 管理员 | 死信记录 | 同步 |
| 13 | GET | `/api/admin/agent-traces` | 管理员 | Agent 协作 trace | 同步 |
| 14 | GET | `/api/admin/tool-audits` | 管理员 | 工具策略审计 | 同步 |
| 15 | GET | `/api/admin/conversations/{sessionId}` | 管理员 | 会话全文（不存在时 404） | 同步 |
| 16 | POST | `/api/admin/knowledge` | 管理员 | 文本入库 | 同步 |
| 17 | GET | `/api/admin/knowledge/status` | 管理员 | 检索链路与向量库状态 | 同步 |
| 18 | POST | `/api/admin/knowledge/rebuild-vector` | 管理员 | 全量重建向量索引（不可用时 503） | 同步 |
| 19 | POST | `/api/admin/knowledge/backup` | 管理员 | 生成 Chroma 快照（不可用时 503） | 同步 |
| 20 | POST | `/api/admin/knowledge/file` | 管理员 | 上传 md / txt / pdf 入库 | **异步** |

---


## 3. SSE 事件格式

`POST /api/chat/stream` 返回 `text/event-stream`，事件块格式为：

```text
event: meta
data: {"sessionId": "<publicId>", "content": null, "message": null, "type": "meta"}
event: token
data: {"sessionId": "<publicId>", "content": "你", "type": "token"}
...（逐 token 追加）
event: done
data: {"sessionId": "<publicId>", "content": null, "type": "done"}
```

顺序：`meta` 先返回会话 `sessionId`（`chat_sessions.public_id`，UUID，不是自增主键）→ `token` 逐段返回模型输出 → token 流结束后保存 assistant 消息并派发工具计划（队列或 MCP 直连）→ 最后返回 `done`。工具派发异常只记服务端 warning，不改变已输出 token，也不阻断 `done`。

学生可见的 token 内容不包含风险等级、心理标签、情绪分数、报告 ID；工程 harness 对真实 SSE 输出做禁用词断言（见 [SAFETY](./SAFETY.md)）。

---

## 4. 调用示例

```bash
# 普通聊天：不触发检索和报告
curl -N -u student:student123 -H 'Content-Type: application/json' \
  -d '{"message":"帮我解释一下 Python 字典推导式"}' \
  http://127.0.0.1:8080/api/chat/stream

# 咨询：触发检索，写入报告，但不会通知辅导员
curl -N -u student:student123 -H 'Content-Type: application/json' \
  -d '{"message":"我最近压力很大，连续几天失眠"}' \
  http://127.0.0.1:8080/api/chat/stream

# 高风险：触发检索 + 报告 + Excel 台账 + 个案 + 预警（取决于工具队列开关）
curl -N -u student:student123 -H 'Content-Type: application/json' \
  -d '{"message":"我不想活了，感觉撑不下去了"}' \
  http://127.0.0.1:8080/api/chat/stream

# 管理员：查看一次协作的完整决策路径（只读 API）
curl -u admin:admin123 http://127.0.0.1:8080/api/admin/agent-traces

```

---

## 5. 错误码约定

| 状态码 | 场景 | 响应形态 |
| --- | --- | --- |
| `401` | 未提供 Basic Auth、格式错误、账号或口令错误 | `{"detail": "..."}` |
| `403` | 角色不满足（学生访问管理接口 / 管理员发起对话） | `{"detail": "..."}` |
| `404` | `/api/admin/conversations/{sessionId}` 会话不存在 | `{"detail": "..."}` |
| `422` | 请求体校验失败（FastAPI 默认可读 JSON 校验错误） | FastAPI 标准校验结构 |
| `503` | 向量索引重建 / 快照时 Chroma 不可用 | `{"detail": "..."}` |

---

## 6. 管理端前端与只读 API

管理端是原生 HTML/CSS/JS（`app/static/admin.html`、`admin.js`），实际提供 **4 个面板**：

- **风险个案**：`GET /api/admin/cases`、`GET /api/admin/cases/{caseId}/notes`；
- **心理风险与咨询报告**：`GET /api/admin/reports`；
- **RAG 知识库维护**：`POST /api/admin/knowledge`、`POST /api/admin/knowledge/file`、`GET /api/admin/knowledge/status`、`POST /api/admin/knowledge/rebuild-vector`、`POST /api/admin/knowledge/backup`；
- **会话档案**：`GET /api/admin/conversations/{sessionId}`。

任务、死信、工具审计、Agent trace 与 Excel 台账是**只读 HTTP API**，没有对应的管理端 UI 入口；需要时用 curl 或自建面板读取。学生端为 SSE 打字机对话界面。

---

## 7. 安全边界

- 学生只能读取自己的报告（`/api/reports/me` 按当前用户过滤），全部管理接口要求 `ROLE_ADMIN`；
- 管理员 trace 接口可读取 `original_input` 原文与完整协作步骤，部署时应按最小权限原则收紧或做二次审计；
- 演示账号与口令是种子数据，正式环境必须替换；`knowledge/status` 只暴露检索链路状态，skill 就绪状态只在 `agent/status` 暴露；
- 响应中不返回自增主键（会话以 `public_id` 对外），但管理端列表接口会返回内部 ID，用于管理操作。

完整认证授权边界见 [LIMITATIONS](./LIMITATIONS.md)，SSE 内容约束见 [SAFETY](./SAFETY.md)。

---

## 相关文档

- [ARCHITECTURE](./ARCHITECTURE.md)：端到端链路与 Harness 分层
- [SAFETY](./SAFETY.md)：风险判定、安全审查与元数据隔离
- [RAG](./RAG.md)：知识库管理接口背后的检索链路
- [MEMORY-SKILLS](./MEMORY-SKILLS.md)：`agent/status` 中的 skill 状态
- [QUALITY](./QUALITY.md)：API harness 的权限断言
- [DEVELOPMENT](./DEVELOPMENT.md)：本地启动与账号
- [LIMITATIONS](./LIMITATIONS.md)：认证授权与产品化边界
