# 自部署 GPU 验证：2026-11-02

AutoDL RTX 5090 上的 Qwen3-8B 与 Qwen3-Embedding-0.6B 已启动，通过本机 Docker SSH 隧道完成协议探测、账号配置保存、34 片段独立向量索引，以及普通对话与咨询两轮合成 HTTP 冒烟。阶段 3 **尚未整体验收**。

可核对的公开元数据见 [selfhost-gpu-smoke-20261102.json](benchmarks/selfhost-gpu-smoke-20261102.json)。日期使用新加坡时间；原始报告目录使用 UTC，所以为 `20261101T18…Z`。历史 `dual-mode-code-20261101.json` 保留当时状态，不回填为 GPU 已验收。

## 运行条件与结果

- SSH 实查：RTX 5090，32607 MiB 显存，驱动 595.71.05；双服务运行时观察到显存占用 29307 MiB。这不是峰值容量或性能承诺。
- 聊天 BF16、8192 token 服务窗口；embedding BF16、1024 维。窗口长度仅验证服务声明，未做满窗口压力测试。
- 启动命令设置 `VLLM_USE_FLASHINFER_SAMPLER=0`，绕过此次 FlashInfer 采样 JIT 初始化失败，仍使用 GPU 推理。
- 协议探测 5 次模型请求全部完成，普通/流式/JSON/审核/embedding 均通过，总计 204 token。
- 演示学生账号通过产品 API 保存完整自部署配置 revision 1，索引建成 34 片段。
- 修复后完整离线回归 **370 项通过**，6 组工程 Harness 通过；它们不是额外的真实 GPU 质量样本。

| 合成场景 | 应用结果 | 端到端耗时 | 模型请求数 | 输入 / 输出 token | 前缀命中 token / 有缓存统计的输入 token |
|---|---|---:|---:|---:|---:|
| 普通对话 | done | 4.56 秒 | 4 | 2034 / 83 | 2000 / 2034（98.3%） |
| 考试紧张与短期睡眠困扰咨询 | done | 15.00 秒 | 6 | 12717 / 870 | 11808 / 12684（93.1%） |

两轮账本均已 finalized，写入错误为 0；记录到的全部模型请求均经 `model-tunnel`，mode 为 `selfhost`。咨询的 embedding 输入 33 token 不含前缀缓存拆分，不能放进该命中率分母。缓存已被重复探测/重放预热，以上命中率不能代表真实用户流量，也不能当作冷启动对照。GPU 租金、空闲时长和成功请求摊销成本尚未配置，费用为未知，不是零。

## 首次失败与修复

首次普通对话正常，咨询则在两次正文审核后固定兜底：`FACT_MISCLASSIFIED_AS_SUPPORT`，最终 `REVISION_BUDGET_EXHAUSTED`。模型把含因果/效果断言的句子归入共情，原有笼统反馈不足以让它纠正。该失败和首次报告哈希保留在公开元数据中。

现在将具体失败句子和原因传入同一候选的有限分类澄清，以及后续正文修订；没有改变引用必须来自原始来源、事实不能标为共情、审核拒绝不能被覆盖等校验。未增加澄清或正文修订次数。重新执行相同两个合成场景后均得到正常完成。

同时修复冒烟脚本的假通过：原来一轮成功即可设置 `passed=true`；现在要求两轮都正常完成，任何兜底都会返回失败并保留报告。新增回归覆盖这次假通过和具体证据反馈。

## 复现与剩余验收

部署和产品操作见 [AutoDL 手册](AUTODL.md)。保存完整自部署配置后，在 Windows 项目目录运行：

```powershell
docker compose -f docker-compose.local.yml -f docker-compose.selfhost.yml exec -T app python scripts/probe_selfhost.py
docker compose -f docker-compose.local.yml -f docker-compose.selfhost.yml exec -T app python scripts/smoke_model_profile.py --expect-mode selfhost
```

协议原始报告：`target/selfhost-probe/20261101T184253Z/report.json`；首次业务报告：`target/model-profile-smoke/20261101T184357Z/report.json`；修复后业务报告：`target/model-profile-smoke/20261101T184652Z/report.json`。本地回归和 Harness 日志分别为 `target/selfhost-regression-20261102.log`、`target/selfhost-harness-20261102.log`，不提交运行目录。

以上样本参与了问题调试，不属于未见测试集。仍需验证风险与空证据场景、双账号真实并发隔离、主动禁用外部模型 API 的运行，以及同预算 1/2/4 并发、独立质量、吞吐与租卡成本对照。两轮 SSE 完成不能推出业务质量或生产容量合格。
