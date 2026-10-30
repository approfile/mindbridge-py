# 运行、测试与复现

## 本机演示

Python 3.12，安装 requirements.txt，按[首页](../README.md)的 SQLite + mock 命令启动。Redis 不可用时近期记忆降级；log 预警只记录模拟投递。不要把这些结果当作真实 embedding、SMTP 或模型验证。

真实 API 的聊天和 embedding 配置分开；.env.example 提供空密钥模板。角色级 AGENT_MODEL_* 可覆盖默认 provider/model。可选 Ollama 只需配置 AI_PROVIDER=ollama、OLLAMA_BASE_URL 和本机实际存在的 OLLAMA_MODEL；仓库没有 GGUF 权重。

## Docker

复制 .env.example 为 .env，填写模型及向量 API 配置后执行 docker compose up -d --build。服务为 MySQL 8.0（13306）、Redis 7.2（16379）、应用（8080）；数据、台账及 privacy.key 位于挂载的 data/，向量另用卷。默认演示数据库凭据需要自行更换。

Compose 不自动配置 SMTP。真实预警需显式提供 SMTP_HOST/PORT/USERNAME/PASSWORD、TLS/SSL、收发人及 ALERT_EMAIL_DELIVERY_MODE=smtp。当前 Compose 只透传其中列出的变量；额外配置请在本机 override 中明确加入。ALERT_EMAIL_DELIVERY_MODE=log 不是邮件投递证据。

镜像不携带 .env、密钥、运行库、缓存或权重；升级现有数据库前备份数据及对应密钥。迁移加密不会清除旧备份。

## 离线检查

```bash
pip install -r requirements.txt -r requirements-evaluation.txt
mkdir -p target data
AI_PROVIDER=mock DATABASE_URL=sqlite:///./target/tests.sqlite3 \
KNOWLEDGE_VECTOR_ENABLED=false python -m unittest discover -s tests
python -m app.harness.runner
python scripts/validate_agent_tasks.py
```

CI 与本机验证只证明所运行的测试。mock RAG Harness 使用 BM25 降级，不能复现 Qwen 向量成绩。完整任务评测脚本不由普通单测自动触发。

## 分层真实执行的前置条件

scripts/run_agent_results.py、run_agent_layered.py 和 judge_agent_layers.py 使用独立 SQLite、Redis db15 的随机命名空间、隔离台账与本机 Mailpit（HTTP 8025、SMTP 1025），不会复用线上业务库。实际副作用验证需要这些服务；没有 Mailpit 时不能把邮件记录 SUCCESS 算已收件。

历史 v1 的 app/evaluation/agent_layered_execution.py、scripts/prepare_rag_quality_samples.py 还读取 .local-runtime/chunk-size-ab-20261025/embedding-cache.json 和 target/chunk-size-ab/report.json；部分父目录也需预先存在。这些历史缓存不公开，所以新 checkout 不能直接一条命令重跑全部真实层。当前发布保留原脚本与源码哈希，不改写历史实验。后续需做版本化的可配置缓存/在线重建适配，再生成新的执行证据。

历史原始入口与参数见[主报告](agent-layered-evaluation-v1.md)，输出目录应新建，不能覆盖旧成绩。参考答案不传给生成端；人工表须真人填写。

## RAGAS 独立环境

```bash
python3.12 -m venv .venv-ragas
.venv-ragas/bin/pip install -r requirements.txt -r requirements-ragas-evaluation.txt
.venv-ragas/bin/pip check
# 先满足上述采样前置条件，生成新的 samples 后：
.venv-ragas/bin/python scripts/score_ragas_layers.py \
  --samples target/agent-layered/ragas-v1 \
  --output target/agent-layered/ragas-v1/new-report.json
```

RAGAS、独立模型评审与真实生成会调用付费 API。该依赖环境与业务环境分开，避免 LangChain 等可选依赖改变线上环境。历史报告公开的是固定合成实验，不含 API 凭据、私有数据库或密钥。
