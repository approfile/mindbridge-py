# Windows 本机运行与验证

本项目的 Excel 进程锁依赖 `fcntl`，密钥文件要求 POSIX `0600` 权限。
Windows 本机完整演示使用 Docker Desktop 的 Linux 容器；Windows `.venv` 用于编辑器和不依赖上述功能的脚本。

## 本机配置

在项目根目录 `.env` 填写以下配置，不要提交该文件：

```dotenv
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=填写你的生成模型密钥
OPENAI_MODEL=填写你账号可用的生成模型
OPENAI_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_EMBEDDING_API_KEY=填写你的向量模型密钥
OPENAI_EMBEDDING_MODEL=填写你账号可用的向量模型
```

生成与向量接口可以使用不同供应商，接口须兼容当前代码的 OpenAI chat/completions 和 embeddings 请求。
模板里的历史模型名不保证在你的账号下可用。

本机 `.env` 的数据库地址为 `127.0.0.1:23306`，Redis 为 `127.0.0.1:16379`。
23306 避开本机 Windows 已保留的 13278–13377 端口段。
容器内部连接地址由 `docker-compose.local.yml` 指向服务名。

## 启动

在项目目录的 PowerShell 中执行：

```powershell
# 无需 API 的演示：真实 MySQL、Redis、SMTP，生成使用 mock，检索使用 BM25。
.\scripts\start-local.ps1

# 填好两套 API 后：真实生成、真实 embedding、Chroma + BM25，禁止静默向量降级。
.\scripts\start-local.ps1 -RealModels

# 仅生成密钥已准备好：真实生成 + BM25，向量显式关闭。
.\scripts\start-local.ps1 -RealGenerationOnly
```

启动脚本显式选择模型模式，避免仅填入密钥就意外触发真实调用。
真实模式会产生供应商 API 费用。

| 入口 | 地址 / 账号 |
| --- | --- |
| 应用 | http://127.0.0.1:8080 |
| API 文档 | http://127.0.0.1:8080/docs |
| 学生 | `student` / `student123` |
| 管理员 | `admin` / `admin123` |
| 本地收件箱 | http://127.0.0.1:8025 |
| MySQL | `127.0.0.1:23306`，本地演示库 `mindbridge` |
| Redis | `127.0.0.1:16379` |

本机 Mailpit 只接收测试邮件，不向真实邮箱转发。SMTP 的实际收件证据可在其界面检查。
[Mailpit 官方 Docker 说明](https://mailpit.axllent.org/docs/install/docker/)。
所有宿主端口只绑定 `127.0.0.1`。

## 验证

```powershell
# 当前运行服务的 HTTP/SSE、SQL、Redis 加密、报告、outbox、Excel、邮件和权限证据。
docker compose -f docker-compose.local.yml exec -T app python scripts/verify_local.py

# 对真实模型模式增加 provider 检查，向量是否必须可用由该模式配置约束。
docker compose -f docker-compose.local.yml exec -T app python scripts/verify_local.py --expect-provider openai

# 单测与 6 组 Harness：独立 SQLite、mock，不改动本机演示业务库。
docker compose -f docker-compose.local.yml exec -T app python scripts/verify_offline.py
```

业务冒烟会新增合成学生会话、报告、台账与本地测试邮件，不删除旧数据。
新证据保存在 `target/local-verification/<UTC时间>/report.json`，不能替代历史真实评测成绩。
离线 Harness 输出位于容器 `target/harness/` 的独立 Linux 卷；可用 `docker compose cp` 导出报告。

## 数据与日常维护

本机栈名称 `mindbridge-local`，与旧 `mindbridge-py` 容器和卷分开。
数据库、Redis、邮件及应用 `data/` 各使用独立 Docker 卷。密钥与应用数据在 Linux 卷中，满足文件权限检查；项目目录中的 Windows `data/` 与容器 `/app/data` 不是同一目录。

```powershell
docker compose -f docker-compose.local.yml ps
docker compose -f docker-compose.local.yml logs --tail 100 app
docker compose -f docker-compose.local.yml stop
docker compose -f docker-compose.local.yml cp app:/app/data/mindbridge-risk-ledger.xlsx .local-runtime/mindbridge-risk-ledger.xlsx
```

不要删除数据卷后继续使用旧密钥关联的数据，也不要将密钥发到聊天、提交 Git 或放入镜像。
需要迁移时须一起备份数据库、应用 data 卷及对应密钥。

## 验证范围

普通工程检查不会调用 RAGAS 或模型 judge。它们需要独立评测环境和额外 API 调用。
本机 `.venv-ragas` 单独安装 `requirements-ragas-evaluation.txt`，用于已有 samples 的评分；不要与业务 requirements 合装。
历史分层 v1 还依赖未公开 embedding 缓存，详见 [运行与复现说明](DEVELOPMENT.md)；它不是安装 Redis 就能复现的链路。
Ollama 为可选替代模型，不是 API 模式的前置服务；仓库不含 GGUF 权重。
