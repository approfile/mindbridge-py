# 敏感数据治理：正文、读取与保留

日期：2026-10-25。对应原缺陷清单第12条。本次修改主项目，未同步覆盖 `dist/`、`github-repo/` 副本。目标是减少心理正文的明文副本与默认暴露，并让读取和清理有可验证的边界；不是合规认证、完整匿名化或全库透明加密。

## 原先的旁路

原文不只在 `agent_run_traces.original_input`：聊天消息、标题、报告、记忆状态、trace内嵌产物/历史/提示词、个案交接及部分工具异常也会复制正文。原管理员接口直接返回多个副本；仅把originalInput换成sanitizedInput不能解决旁路。账号显示名还会被自动拼进模型提示词，邮件会复制学生身份和交接全文。

## 已实现的边界

| 位置 | 当前行为 |
| --- | --- |
| SQL正文 | 24个自由文本字段使用Fernet认证加密；包括会话标题/消息/派生记忆、报告正文/摘要、trace全部正文JSON、个案交接/备注、工具错误/审计载荷、预警记录收件人与消息等 |
| ORM内部 | 可信服务解密后继续原规划、记忆和工具逻辑；字段加密不意味着模型在密文上推理 |
| 默认后台读取 | 保留状态、风险、关联ID、技术事件和耗时；正文隐藏，学生显示为去标识编号，文件路径/收件人/内部异常载荷隐藏 |
| 单条正文读取 | 需ADMIN＋独立SENSITIVE_READER、显式includeSensitive=true及枚举用途；仍做电话/邮箱/身份证掩码，originalInput始终是固定占位值 |
| 本人报告 | 只能查当前学生自己的报告，内容经过同一基础掩码；不能访问其他学生的后台接口 |
| Redis | 主会话和各Agent私有记忆的content加密，保留TTL及版本；旧列表在启动时用WATCH/CAS迁移，未迁移明文不作为读取兜底 |
| 模型输入 | 保留原脱敏输入管道，停止自动把账号显示名拼进ResponseAgent提示词，使用通用称呼“学生” |
| 邮件 | 只发送个案/报告ID、风险、时间和受控后台查看提示，不再复制学生姓名、原文、摘要或交接全文 |
| Excel | 不写原文，摘要做基础掩码及公式单元格保护，文件0600；其风险标签/摘要仍是应受管理的导出资料 |

数据库的用户名查询索引、外键、风险/状态等结构化元数据仍可明文存在。这里是应用字段加密和接口去标识，不是不可关联匿名化；持有数据库及运行密钥的可信运维仍能解密。普通HTTP管理员不能通过新增接口读取原始全文。

## 密钥与旧数据迁移

`PRIVACY_KEY_FILE`默认`data/privacy.key`，实际本机容器挂载在持久data卷内。新数据库初始化时生成随机密钥，原子发布并限制为0600；不写源码、数据库或镜像。应用读写不制造替代密钥；密钥丢失、权限过宽、符号链接、内容损坏或运行中被更换会失败关闭。进程固定已观察到的密钥标识，防止运行中混写两套密文。

首次旧库迁移必须停止旧写入者。MySQL命名锁串行化迁移，扩展列容量后分批加密；原字段值、主键、外键、标签、confidence及原时间不变。数据事务可回滚，DDL及重复迁移可重入；迁移状态保存密钥指纹。SQLite也已验证。新schema通过原bootstrap创建独立读取审计和服务端清理标记表。不是支持旧明文写入者与新密文版本混跑的滚动升级。

本次停写后制作了0600加密SQL备份、加密台账及密钥备份；没有把真实原文快照写入新的明文文件。使用HMAC在内存逐行核对升级前后语义，避免留下可读正文或简单字典哈希。备份已在仅root可访问的临时库恢复核对，验证后清理；回滚镜像由原源码快照重建并比对105个原app文件。未对主库执行恢复。恢复需停写、恢复到干净库、恢复对应台账、清理派生缓存并切回匹配源码，不能把原明文备份直接叠进仍标记迁移完成的密文库。

本机文件密钥不是KMS/HSM；密钥轮换流程、主机整体被攻陷、数据库物理页/binlog和历史备份的全生命周期不因此解决。密钥及备份解密密钥需要另行安全保管，不应随公开项目包发布。

## 读取契约与审计

后台列表始终只返回必要信息，不能通过Reader角色批量导出正文。以下单条接口支持受控脱敏详情：

```text
GET /api/admin/reports/{report_id}?includeSensitive=true
GET /api/admin/agent-traces/{trace_id}?includeSensitive=true
GET /api/admin/conversations/{session_id}?includeSensitive=true
GET /api/admin/cases/{case_id}?includeSensitive=true
GET /api/admin/cases/{case_id}/notes?includeSensitive=true
X-Access-Purpose: case_followup | incident_review | data_subject_request
```

缺少Reader权限403，缺少/无效用途400，列表请求正文400。未知raw=true等参数不能绕过投影。所有详情仍不提供originalInput原文。默认管理员可看统计和去标识档案；Reader在界面选用途后点击单条读取。用途是受控枚举，不让操作者把新的敏感说明自由写入审计。

`data_access_audits`在查询前保存STARTED，结束后保存ALLOWED/FAILED/NOT_FOUND；拒绝权限和匿名认证失败也记录。记录账号ID、服务器request ID、资源/受约束资源ID、用途、结果与行数，不记录正文、账号显示名、任意Header或异常全文。独立事务防止业务回滚抹掉读取尝试；审计不可写时不返回正文。只读审计接口`GET /api/admin/data-access-audits`还要求`ROLE_PRIVACY_AUDITOR`，不因能审计而自动获得正文权限。

新增角色只能用受信任本机运维CLI授予/撤销，不是LLM工具或普通后台授权API；操作记录系统UID与角色。以下示例在应用容器内、使用同一数据库及密钥配置执行，本次**没有**给主演示admin自动授权：

```bash
python -m app.privacy_cli grant --username admin --role sensitive-reader
python -m app.privacy_cli revoke --username admin --role sensitive-reader
python -m app.privacy_cli grant --username admin --role privacy-auditor
```

角色每次认证重新读取，撤销在下一次请求生效。当前Reader是本机演示的全局详情角色，尚无学校/组织/个案分配范围的授权，也不是审批工作流。审计是应用数据库记录，不是运维无法篡改的WORM日志。API增加no-store及基础安全响应头。

## 保留清理：默认预览，显式离线执行

| 配置 | 工程默认 | 边界 |
| --- | --- | --- |
| PRIVACY_TRACE_RETENTION_DAYS | 30天 | 清除到期调试正文，保留受限技术事件/关联元数据 |
| PRIVACY_CONTENT_RETENTION_DAYS | 90天 | 会话最后活动超过期限，才考虑正文/报告/派生记忆清理 |
| PRIVACY_ACCESS_AUDIT_RETENTION_DAYS | 365天 | 清理旧访问审计；最小服务端清理标记独立保留用于幂等，不含正文 |

这些是可配置工程默认值，未替代实际业务对保留期限的决定。没有启用后台自动删除任务。默认预览：

```bash
python -m app.privacy_cli retention --dry-run
```

停掉应用和MCP/Worker写入后，运维明确执行：

```bash
python -m app.privacy_cli retention --apply --offline
```

清理用固定占位值替换SQL正文，清空派生状态及过期载荷，并删除该会话和所有Agent的Redis列表/版本，保留元数据行与业务关联。Redis不可用时回滚SQL清理，避免缓存把旧正文重新恢复。应用拒绝用已到期的普通会话重新加载旧记忆。独立`privacy_erasure_markers`记录处理状态，不能用用户也能输入的“已清除”字样判断已完成；已补对应攻击测试。

未关闭个案和PENDING/RUNNING任务保护其会话内容，不因通用期限清除临床跟进/在途任务；调试trace仍按短期策略清正文。当前个案只有原跟进流程，正式结案状态的业务规则仍需另行确定。`--offline`是可信运维前提声明，不是跨实例停写协议。Excel、已发送邮件、历史备份和其他目录的人工副本不由SQL清理自动撤回。

**本次主库只预览，0条到期，没有执行正文清理或删除既有成果。**实际清理、重入、活动个案豁免及缓存故障回滚均在隔离库验证。

## 实际验证与结果口径

- 最终195项单元/回归通过，覆盖24个字段密文、随机化/防篡改、错误/丢失密钥、旧库重入、默认读取旁路、独立角色/用途/禁批量、本人范围、读取留痕及故障关闭、保留预览/执行/重入、活动个案/在途任务、缓存故障回滚、用户伪造清理标记、账号名称不自动外发、脱敏不得吞掉危险语义。原6组工程Harness及JS语法检查通过。
- 隔离MySQL、Redis10、真实HTTP、Excel、Mailpit及实际MCP：6组治理验收通过，9项持久任务完成；旧明文原值解密后相同，默认接口隐藏，受控详情保留电话/邮箱/身份证掩码，角色撤销生效。真实保留清理移除普通过期正文及缓存，保留活动个案；缺密钥启动失败且不生成替代密钥。
- 100条真实模型意图/安全路由100/100，32风险32/32。首次8组多轮6/8、11/13轮正文接受：危机承接因UNSUPPORTED_RESOURCE、伤人案例因DANGEROUS_GUIDANCE/CRISIS_SUPPORT_MISSING走原安全兜底，未从分母移除。停止自动外发账号显示名后，另一次最终回复链路8/8、13/13轮接受；两个版本的完整结果均保留，不能归因于隐私改动提升质量，也不称为稳定100%。没有重复挑选同版本最好结果。
- 最终源码中UnderstandingAgent/SafetyAgent及到期判断函数AST与100条评测时一致。后续变化为通用显示名、独立清理标记和移除宽泛标签掩码；已保存不同源码清单并核验冻结数据及最终对话记录的全部字符串经旧/新脱敏后相同，另补标签危险表达攻击回归。原100条报告的来源哈希没有被改写成最终版本哈希。
- 8080最终四类真实模型演示、主库旧行保护、台账、实际Redis密文/TTL、Chroma34段及最小化邮件，以公开机器摘要的最终凭证为准。现有admin角色保持原样，不能读取正文。

本次曾加入过宽泛“姓名/地址标签后到标点”的掩码，最终检查发现它可能连危险表达一起吞掉，已撤掉并增加真实Harness高危/outbox测试。因此当前基础掩码只承诺已验证的电话、邮箱、18位身份证；不声称识别所有姓名、地址或非标准写法。自由文本可能仍含未识别个人信息，受控Reader必须按用途使用；字段密文与默认正文隐藏是主要边界。外部模型的完整PII识别、用户授权与供应商数据条款仍需实际业务补充。

## 证据与复测

公开机器摘要：`docs/benchmarks/privacy-governance-v1-summary.json`。逐条合成模型评测：`target/privacy-governance/`。私有源码快照、加密备份及密钥、主库HMAC、迁移/恢复/拒绝读取/清理/MCP/部署记录：`.local-runtime/privacy-governance-20261025/`，严禁公开打包。

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m app.harness.runner
node --check app/static/admin.js
```

旧任务目录中的历史明文备份/调试文件未擅自重写，已将`.local-runtime/`及密钥/加密备份加入忽略规则；它们仍需运维按实际保留要求处理。现有演示认证、粗粒度角色、独立质量/临床评测等边界也不会因这次治理自动消失。
