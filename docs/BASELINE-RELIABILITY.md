# 双模式产品化之前：基准可靠性修复

日期：2026-11-01。保留原始 [阶段 2 基准](SYSTEM-BENCHMARK.md)，在同一数据集上修复已暴露的问题。
本轮仍在 `codex/project-improvements-20261101` 分支，`main` 保持原状。真实自部署/GPU 平台选择按用户要求留到这些修复之后。

## 会话记忆事务

原来在持有会话父行锁之后，对尚不存在的 `session_memories` 行执行 `SELECT ... FOR UPDATE`，再插入派生记忆。不同新会话可以落在同一个索引间隙，形成相互等待。

修复只改变 MySQL 的初始化顺序：先在原事务中用唯一 session ID 原子创建/保留记忆行，再执行锁定的当前读。保留父会话锁、消息来源验证和当前读，不把数据库全局降成其他隔离等级，也不重试整轮模型或工具副作用。已有记忆内容不会被空状态覆盖，初始化可随事务回滚。

依据：[MySQL 锁定规则](https://dev.mysql.com/doc/refman/8.0/en/innodb-locks-set.html)、[SQLAlchemy MySQL upsert](https://docs.sqlalchemy.org/en/20/dialects/mysql.html#insert-on-duplicate-key-update-upsert)。

专用 MySQL/Redis 命名空间事务探针，强制四线程在读取记忆行后同步，5 批共 20 个新会话：

| 检查 | 已提交旧源码 | 修复后源码 |
| --- | ---: | ---: |
| 提交 / 20 次新会话 | 5 | 20 |
| 1213 死锁 | 15 | 0 |
| 同会话 20 个并行写入与最终游标 | 正确 | 正确 |
| 较早一致性快照之后读取已提交新事实 | 正确 | 正确 |
| 初始化随事务回滚 | 正确 | 正确 |
| 隔离等级 | REPEATABLE READ | REPEATABLE READ |

这是刻意制造竞争的锁序回归，不是自然业务死锁率。前后源码哈希及结果分别保存在 `target/memory-transactions/before3-20261101/report.json`、`after2-20261101/report.json`。测试脚本为 `scripts/verify_memory_transactions.py`；旧实现来自提交 `a0532c1` 的可信本机源码副本。

## 空证据回复的误拦

三次冻结合成场景诊断均出现同一个问题：候选已经明确说“不能保证你今晚预约成功”，却被完成声明规则当成“预约成功”，在独立模型审核之前直接拒绝，修订后仍被相同规则拦住。

`reply-safety-v4` 只增加直接支配该动作的中英否定保证/承诺识别。真实的“已替你预约”“已通知”等声明仍阻止；否定句后的另一条完成声明、双重否定、虚构联系方式、诊断及其他安全边界仍受原检查。候选依旧要经过独立模型审核，不能因规则例外直接发出。

同输入诊断从 0/3 被接受变为 3/3 经过审核后被接受。原候选、规则发现、后续审核与 token 记录保存在被忽略的 `target/empty-evidence-diagnostics/`；不含真实用户对话。这只是定位样例，整体结果以完整冻结基准为准。

## 独立评审有效性

原 100 组裁决有 10 组无有效结构/引文，先前仅保存 ValueError，无法判断原因。本轮先增加私有原始输出与固定错误码，发现维度/嵌套结构错误、无效 JSON、重复键和非逐字引文。

第一次反馈修复版本 v2 对旧样本只取得 85/100 有效结果，未采用它替换原成绩；其 44 次无效尝试及全部输出也保留。开发可见的 v3 校准子集为 12 条，12 条均在最多两次尝试内取得有效结果，包含 4 次无效尝试；没有把该子集称为整体质量结果。

最终 `system-baseline-judge-v3`：

- 服务端只从实际可见回复切出带 ID 的片段；用户文本与知识库内容不能进入该引用目录。
- 评审器选片段 ID，服务端映射回原文，再沿用严格字段、判定和连续引文验证。不存在的 ID、重复 JSON 字段、缺证据仍拒绝。
- 两次尝试有固定错误码与修复反馈；所有无效输出和费用留在私有产物，不自动改成通过。
- goal、safety、grounding 分开：任务没有完成不等于已输出危险内容；无具体事实陈述不等于存在无来源事实，但任务目标仍独立判断。

原样本完整重评为 **100/100 有效裁决**，包含 27 次需要保留的无效首轮尝试。有效裁决不等于裁决正确，也不等于任务通过；仍是同供应商模型判断，独立人工/专家复核尚未完成。

后续比较使用旧样本和修复后样本的同一个 v3 评审器，避免把评分协议变化误当作业务能力提升。原 v1 分数不改写。重评目录为 `target/system-benchmark/baseline-v1-regraded-v3-20261101/`，脚本为 `scripts/rejudge_system_benchmark.py`。

## 复现与范围

正式 SQL/基准验证使用专用 `mindbridge_bench_*` 数据库、Redis db14、独立密钥/向量/台账与本机 Mailpit。脚本拒绝业务库或非空初始库；不会清除业务数据。模型诊断与评审产生真实费用，分别保留计量记录，不混入用户日常轮次。

原始数据集及输入没有修改，模型角色、模型名称、参数、预算、检索参数和语料维持一致；原始基准、本轮诊断、校准和最终重跑各自归档。后续双模式仍按路线实现按用户凭据隔离、每轮配置快照、独立 embedding 配置与索引版本，以及真正可验收的自部署端点。

## 完整基准重跑结果

修复后正式目录为 `target/system-benchmark/reliability-v1-20261101/`。全部 **110 个场景试次、122 次 HTTP** 已捕获，request ID 唯一，在线 attempt 与账本 CSV 对齐，测量源码哈希一致。原数据集哈希仍为 `80130471723d8f3dc7c332444ffcd252077ec3bf1939ed80edf1a4ab9afcb6bb`。

[同评审器对照摘要](benchmarks/reliability-fix-20261101.json) · [修复后完整汇总](benchmarks/reliability-system-baseline-20261101.json)。完整离线回归为 **342 项测试、6 组 Harness**。

| 验证 | 修复前 | 修复后 |
| --- | --- | --- |
| 完整批次会话记忆 1213 | 10 次：正常四并发 4、容量探针 6 | 0 次 |
| 空证据试次契约，同一 v3 评审器 | event-driven 2/6；sequential 0/6 | 两种编排均 6/6 |
| 有效独立裁决 | 原 v1 为 90/100；旧样本经 v3 重评为 100/100 | v3 为 100/100 |
| 正常工作负载四并发完整用量请求比例 | 两种编排均 16/18 | 两种编排均 18/18 |
| 两个五请求容量批次 | 各准入 4、拒绝 1，但有准入后死锁 | 各准入 4、拒绝 1，无死锁 |
| 物理副作用 | 47 报告 / 71 任务 / 12 封 Mailpit | 48 报告 / 72 任务 / 12 封 Mailpit |

修复后 Excel 对 48 份报告各写入一次；12 封高风险邮件均由本机 Mailpit 收件。没有发送给真实人员，收件也不代表现实介入。

### 业务质量仍有边界

以下自然场景每格均为 14 个场景试次，按相同 v3 评审器统计，unknown 为 0。不能将它们与旧 v1 评分直接相减。

| 编排 | 并发 | 修复前契约通过 | 修复后契约通过 |
| --- | ---: | ---: | ---: |
| event-driven | 1 | 13/14 | 13/14 |
| event-driven | 2 | 13/14 | 13/14 |
| event-driven | 4 | 11/14 | 12/14 |
| sequential | 1 | 13/14 | 14/14 |
| sequential | 2 | 12/14 | 13/14 |
| sequential | 4 | 11/14 | 13/14 |

依然存在因生成/审核/修订而未完成的自然任务，全部保留。8 个固定场景及重复观测不是独立大样本，模型输出与云端缓存也会变化；不能将所有质量差值都归因于本次代码，或宣称临床安全/一般可靠性已证明。

### 性能与费用没有被美化

两种编排的 1/2/4 并发混合批次均为 18 个 HTTP，含空证据干预：

| 编排 / 并发 | SSE done | HTTP P50/P95 秒 | done 回复/秒 |
| --- | ---: | --- | ---: |
| event-driven / 1 | 17/18 | 3.460 / 15.623 | 0.174 |
| event-driven / 2 | 17/18 | 3.651 / 16.281 | 0.298 |
| event-driven / 4 | 16/18 | 3.895 / 15.884 | 0.426 |
| sequential / 1 | 18/18 | 3.563 / 8.924 | 0.205 |
| sequential / 2 | 17/18 | 3.690 / 13.522 | 0.335 |
| sequential / 4 | 17/18 | 3.810 / 14.596 | 0.496 |

某些延迟/吞吐没有优于旧批次：原先死锁后快速失败的请求现在会执行完整模型链路，生成长度、修订次数与供应商负载也会变化。本轮优先修复正确性，未将快速失败冒充性能收益。后续成本/性能优化仍按阶段 4 单独做受控对照。

修复后正式批次分账：

| 类别 | attempt / 费用公开价估算 |
| --- | --- |
| 在线 | 471 次记录，其中 465 次真实上游、6 次本地注入 503；已知 733,585 token；USD 0.050766204、CNY 0.0005380 |
| 新样本离线评审 | 124 次真实调用，100 组有效裁决，保留 24 次无效首轮尝试；USD 0.026607918 |
| 独立建库 | 11 次真实调用；CNY 0.0037185 |
| 旧样本同 v3 评审器重评 | 127 次真实调用；USD 0.024277074；单独保存在重评库，不混入新在线成本 |

前置诊断、v2 调试和 12 条校准子集另有私有计量记录，不包含在此正式批次费用。价格是有版本的标价估算，不是发票，也不把币种相加。

## 复跑命令

先按 [系统基准说明](SYSTEM-BENCHMARK.md)创建并授权各自的全新 `mindbridge_bench_*` MySQL 库。

```powershell
# 完整同数据集重跑；输出目录必须未存在。
docker compose -f docker-compose.local.yml exec -T app python scripts/run_system_benchmark.py --database mindbridge_bench_new_run --output target/system-benchmark/new-run
docker compose -f docker-compose.local.yml exec -T app python scripts/export_system_benchmark.py --input target/system-benchmark/new-run --output target/system-benchmark/new-run/audited-summary.json

# 原始不可变观察使用当前评审器重评，独立库与输出目录。
docker compose -f docker-compose.local.yml exec -T app python scripts/rejudge_system_benchmark.py --input target/system-benchmark/baseline-v1-20261101 --output target/system-benchmark/new-regrade --database mindbridge_bench_new_regrade
```

`scripts/compare_reliability_benchmarks.py` 校验原始观察摘要、同数据集/模型/预算/检索配置、同评审器和事务探针源码哈希，再生成对照；不同口径会拒绝。原始 v1 与本次 v2 调试结果不被覆盖或追认成功。

现在可以进入阶段 3 的双模式产品化；依照用户选择，租用 GPU 服务器和真实 8B 自部署验收留到后续确定。本轮没有将现有 Ollama 命名配置冒充实际已部署/已训练模型。

## 探针隔离遗漏的恢复记录

早期事务探针在补齐 Redis 隔离前，其同会话写入部分曾继承 db0，把两组随机测试会话写入默认缓存前缀，使用的却是各自测试密钥。主服务重启时严格解密校验因此拒绝启动；没有通过跳过校验或更换业务密钥恢复。

恢复脚本 `scripts/repair_probe_cache.py` 只读预览后，逐项确认：两个指定探针数据库中确有对应会话；业务库匹配数为 0；列表可被对应测试密钥解密；40 条内容全部匹配固定合成记录。先保存加密 Redis dump 和 TTL，再用 WATCH 原子移除两张列表及两个 version 键，共 4 键，没有扫描/清空业务缓存。

预览、备份与应用结果保存在 `target/probe-cache-repair-20261101/`，不公开密钥和缓存内容。正式事务对照 before3/after2 及完整基准使用的已是 db14 和专用前缀；修正后的探针显式强制这一隔离。主服务恢复后再次检查健康和原日常数据。这是探针隔离遗漏及恢复，不作为产品安全能力或业务修复收益计数。
