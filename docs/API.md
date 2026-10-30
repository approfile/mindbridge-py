# 当前 HTTP / MCP 入口

基础认证为 Basic Auth；初始 student/student123 与 admin/admin123 仅供本机演示。公网部署必须使用 TLS 并更换演示凭据。

| 接口 | 用途 |
| --- | --- |
| GET /actuator/health | 存活检查 |
| GET /api/profile | 当前用户 |
| GET /api/agent/status | 实际 runtime、模型与 Skills 状态 |
| POST /api/chat/stream | message、可选 sessionId；SSE meta/token/done 或错误/兜底 |
| GET /api/reports/me | 本人报告的受控投影 |
| GET /api/admin/reports、/cases、/alerts | 管理元数据列表 |
| GET /api/admin/reports/{id}、/cases/{id} | 受用途与权限控制的详情 |
| GET /api/admin/agent-traces、/agent-traces/{id} | 运行观测 |
| GET /api/admin/tool-jobs、/tool-audits、/dead-letters | 后处理与审计 |
| GET /api/admin/conversations/{session_id} | 权限控制的会话查看 |
| GET /api/admin/data-access-audits | 独立隐私审计权限 |
| GET /api/admin/knowledge/status | 知识库状态 |
| POST /api/admin/knowledge、/knowledge/file | 追加知识/上传文件 |
| POST /api/admin/knowledge/rebuild-vector、/knowledge/backup | 重建/备份向量 |

管理列表默认隐藏敏感正文；不是凭 ROLE_ADMIN 就能读取全部聊天。准确参数与模式以 app/api/routes.py、app/schemas/dtos.py 和启动后的 /docs 为准。

MCP stdio：python -m app.mcp_tools.server，暴露 Excel、个案创建、预警、确认及备注等受策略控制的入口。stdio 是受信本机通道，不提供远程身份隔离或进程沙箱。工具业务失败会返回协议错误；队列始终先持久化，TOOL_QUEUE_ENABLED=false 选择 MCP 执行通道，而不是跳过持久任务。
