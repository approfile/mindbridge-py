# 更新记录

## 2026-10-31：P0/P1 五项业务改进

- 版本化会话事实抽取，排除问句/假设/归属他人信息，修复英文句尾点号并重建旧派生状态。
- 危险否认分支保留个人咨询诉求；增强高风险兜底、具体动作与中英文完成声明筛查。
- 正文生成/审核增加预算内分类重试与部分流丢弃，检测未完成 SSE。
- 移除 60 字改写前缀截断，按预算显式降级；RAG 正文增加逐句来源覆盖与引文核验。
- 新增 26 个回归测试；289 个单测、6 组 Harness 通过。冻结标签和历史成绩保留，专项真实结果见 docs/p0p1-hardening-20261031.md。

## 2026-10-30：仓库展示优化

- 重组 README 的项目定位、界面演示、核心设计、检索消融、分层评测和快速运行入口，配置与详细成绩使用折叠区。
- 新增原创 SVG 封面、系统架构图与混合检索流程图。
- 使用当前代码和隔离 mock 服务重新截取 5 张界面图，替换旧截图；补充默认正文隐藏与按用途受控审阅展示。
- 图片来源、尺寸与哈希记录于 docs/screenshots/manifest.json；保留真实评测结果及其边界。

## 2026-10-30：同步本地当前版本

- 同步 10 月 24–28 日的安全路由、正文审核、工具租约、统一观测、请求预算与敏感数据治理源码。
- 新增冻结 60 场景 Agent 契约、指标计算、实际副作用采集、分层真实评测、RAGAS 与真人抽检支持。
- 首页和十篇主题文档改用当前配置与真实结果，保留失败/未知及复现边界。
- 原 scripts/verify_offline.py 的依赖桩检查改为正式测试/Harness 入口；旧工具审计测试由当前 test_tool_reliability.py 等覆盖，避免绕过 claim token 的过时调用。
- 历史截图保留供参考，不作为当前隐私验收证据；旧 SVG 架构图由当前 Mermaid 与主题文档替代。
- 修复全新 checkout 的 Harness 密钥初始化，并将其角色模型强制隔离为 mock；不使用业务数据密钥。
- 修复慢收尾回归测试的 100 毫秒墙钟依赖，改为事件同步，保留完成后不可追记超时的断言。
- 发布仅包含白名单合成报告，不包含 .env、密钥、运行库、日志或 embedding 缓存。
- 未在本次同步中修复主报告列出的记忆、咨询边界、生成/审核波动等问题；不重写冻结标签或历史成绩。

---

# Changelog

本文件记录 MindBridge 的对外版本变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- `docs/ARCHITECTURE.md`、`docs/SAFETY.md`、`docs/RAG.md`、`docs/MEMORY-SKILLS.md`、`docs/API.md`、
  `docs/QUALITY.md`、`docs/BENCHMARKS.md`、`docs/LIMITATIONS.md`、`docs/DEVELOPMENT.md`：
  把原 README 的深度章节拆分到 `docs/`，README 只保留 30 秒入口与索引。
- `docs/DESIGN-NOTES.md`（由 `docs/INTERVIEW-NOTES.md` 更名并改写为面向代码评审者的设计说明）。
- `docs/screenshots/`：真实运行实例的学生端 / 管理端截图。
- `docs/rag-pipeline.svg`、`docs/tool-queue.svg`：混合检索与异步工具队列架构图。
- `LICENSE`（MIT）、`CHANGELOG.md`。
- README 首屏新增项目定位、三条核心亮点、mock 模式 3 行快速开始、效果演示与文档索引。

### Changed

- 修正 README 规模口径：后端 Python 文件数按实测口径统一（`app/` 44 个模块 / 约 5.9k 行）。
- 重写“工程质量”章节：保留交付前缺陷审计的事实与回归测试，移除易被误读的自嘲式表述。
- 清理 README 中过时的安全说明，补充 `.env` 从不入库的说明。
- 统一 `scripts/demo_turn.py` 的调用方式；补充 `python scripts/demo_turn.py` 与
  `python -m app.harness.runner --suite rag` 的可复现验证。

### Fixed

- 架构图 `docs/agent-runtime.svg` 的文字裁切、标注重叠与箭头歧义；`docs/architecture-overview.svg`
  的字号与层级编号排版。
- README 内部锚点与章节交叉引用。

## [0.1.0] - 2026-09-19

### Added

- 事件驱动多 Agent runtime：共享黑板（frozen dataclass + 不可变重建）、能力认领调度、预算控制
  （8 轮 / 单轮 4 认领 / 单 Agent 3 认领）与五条件安全采纳门闸。
- 三级风险判定（词典硬拦 → 严格 JSON 模型判定 → 启发式兜底）与 SafetyAgent 版本绑定审查。
- 路由式混合检索：Chroma 向量 + 自研 BM25 + 确定性重排 + 三层降级；60 条评测集。
- 分层记忆（MySQL / Redis / Agent 私有记忆）与上下文压缩。
- MCP 工具服务与异步工具队列：重试、限流、死信、作业依赖、风险等级策略与审计。
- 学生端 SSE 流式对话与管理员端（报告 / 个案 / 工具任务 / trace）。
- 6 组零依赖工程 Harness、单元测试与 GitHub Actions CI。
- Ollama 本地微调模型接入与 OpenAI-compatible API 接入。

[Unreleased]: https://github.com/approfile/mindbridge-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/approfile/mindbridge-py/releases/tag/v0.1.0
