# 本机安装与验证记录（2026-11-01）

## 项目理解

MindBridge 是校园心理支持场景的多 Agent 后端，包含学生对话入口和管理后台。
请求经过 Basic Auth、整轮预算与并发准入，再由共享黑板上的 Coordinator、Understanding、Safety、Context、Response 协作。
风险结果可以覆盖初始意图；Context 组合 Redis 近期消息、SQL 长期记忆、Skills 与按需 RAG；prompt 计划先验收，生成正文再经规则和模型审核，最后通过 SSE 发送。

咨询/风险报告与 outbox 任务同事务提交。独立 Worker 负责 Excel、个案与预警，使用租约、claim token、幂等键、依赖、重试、死信及工具审计；MCP stdio 提供另一种受控执行通道。
这是有明确策略约束的工程系统，不应描述为任意自主工具规划或临床服务。

## 本机环境

| 组件 | 安装与运行结果 |
| --- | --- |
| Python | 3.12.4；业务 `.venv` 的 108 个包通过一致性检查 |
| Windows C++ 编译工具 | Microsoft Visual Studio Build Tools 2022、Windows SDK 安装完成；Chroma 原生扩展编译成功 |
| Docker | Desktop Linux 引擎；镜像构建成功 |
| MySQL | 8.0；独立卷；宿主 `127.0.0.1:23306` |
| Redis | 7.2；AOF 与独立卷；宿主 `127.0.0.1:16379`；实际 PONG |
| Mailpit | 独立卷；Web `127.0.0.1:8025`、SMTP `127.0.0.1:1025`；实际收到合成高风险邮件 |
| 应用 | `127.0.0.1:8080`；真实 DeepSeek 生成 + DashScope embedding，向量必须可用 |
| RAGAS | 独立 `.venv-ragas`，0.4.3，89 个包通过一致性检查；四个指标类和配置导入成功 |

完整业务使用 Linux 容器，因为 Excel 使用 `fcntl`，密钥检查要求 POSIX 文件权限。
应用 data 与 Harness 使用独立 Linux 卷；未修改或删除旧 `mindbridge-py` 业务容器的数据。
端口 13306 位于本机 Windows 保留范围，已在本机配置中改为 23306。

## 实际验证

| 项目 | 结果与证据 |
| --- | --- |
| 编译与依赖检查 | 应用、测试和新增验证脚本编译成功；容器 `pip check` 通过 |
| 单测 | **290/290 通过**；日志 `.local-runtime/verification/offline-linux-final.log` |
| 6 组 Harness | 风险、路由、Skills、RAG、API、工具队列全部通过；导出报告 `.local-runtime/verification/harness-report-final.json` |
| 冻结任务契约 | 60 任务、64 轮、52 family 结构与哈希校验通过；不等于执行过全部自然任务 |
| 真实生成业务 | CHAT、CONSULT、HIGH RISK 三类输入均完成 SSE；三次均 `FINAL_ACCEPTED / ACCEPTED`，未采用固定兜底 |
| 实际副作用 | SQL 报告、运行 trace、加密 Redis、同会话四条消息、outbox SUCCESS、Excel reportId 去重、本地 SMTP 收件均核验 |
| 权限与存储 | 未认证拒绝、学生拒绝管理端、普通管理员拒绝敏感正文、SQL 与 Redis 密文检查通过 |
| Chroma 原生后端 | 独立临时目录中的持久化和向量查询通过；该探测使用合成向量，不能当作真实 embedding 结果 |
| 真实 embedding 与混合检索 | DashScope HTTP 200，Qwen 1024 维；SQL 与 Chroma 各 34 片段；`vectorRequired=true`，检索通过且没有向量错误 |
| 向量备份与任务收尾 | 备份接口 HTTP 200，快照已创建；待执行/运行中任务 0，失败/死信任务 0 |
| 浏览器 | 实际登录学生端并完成真实对话，页面显示 DONE；管理端显示对应报告与个案，正文隐藏 |
| RAGAS 实际调用 | 一条明确标记的合成依赖样本完成 Faithfulness 评分；仅证明评分环境可调用模型，不作为项目质量成绩 |

真实生成业务证据：`target/local-verification/20261031T161633862277Z/report.json`。
该路径使用 UTC；对应新加坡时间 2026-11-01 00:16:33。
完整真实生成 + 向量业务证据：`target/local-verification/20261031T162259916082Z/report.json`（新加坡时间 2026-11-01 00:22:59）。
完整模式的 CHAT、CONSULT、HIGH RISK 三次回复也均为 `FINAL_ACCEPTED / ACCEPTED`。
建索引前已用 GitHub API 核对公开仓库、11 个文档 blob SHA 与数据库切分结果，确认只有 34 个公开内置片段；证据 `.local-runtime/verification/public-corpus-proof.json`。
Excel 导出：`.local-runtime/verification/mindbridge-risk-ledger.xlsx`。
学生端截图：`.local-runtime/verification/student-real.jpg`。
完整模式管理端截图：`.local-runtime/verification/admin-full-real.jpg`。
RAGAS 安装探测：`.local-runtime/verification/ragas-runtime-smoke.json`。

## 本轮修复

1. 修复加密密钥缓存仅按 inode、mtime、size 判断身份的问题。密钥内容在每次使用时校验；即使原地替换并恢复时间戳，也会拒绝读取或写入。新增确定性回归测试，完整 290 项单测通过。
2. 为 Chroma 0.5.23 增加 `posthog>=2.4.0,<6.0.0` 兼容约束，消除旧 capture 调用与新版 PostHog API 冲突；本机关闭匿名遥测。上游说明：[Chroma #4966](https://github.com/chroma-core/chroma/issues/4966)。
3. 修复 RAGAS 安装说明：业务固定 pydantic-settings 2.7.1，而 langchain-community 0.3.31 需要 >=2.10.1。评分环境单独安装评分依赖，业务环境保持原版本。
4. 新增 Windows 启动脚本、独立本机 Compose 和业务物理证据验证脚本，支持 mock、仅真实生成、真实生成与向量三种模式。

## 复现与边界

运行命令、配置项及数据卷说明见 [Windows 本机运行](LOCAL-WINDOWS.md)。
Mailpit 仅验证本地 SMTP 收件，不证明外网邮箱投递。
Ollama/GGUF 是可选替代后端，不是已验证的 DeepSeek API 模式前置条件。
历史分层评测仍依赖未公开 embedding 缓存，本轮没有改写历史报告或将依赖探测分数当作历史质量成绩。
