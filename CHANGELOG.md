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
