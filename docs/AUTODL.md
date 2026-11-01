# AutoDL：租机、部署和接入

## 选什么机器

这套脚本首版面向以下**推理验收**配置。RTX 5090 已完成真实协议与两轮业务冒烟，完整质量、容量和成本验收仍待完成，见 [GPU 验证记录](SELFHOST-VERIFICATION-20261102.md)：

| 项目 | 要求 |
|---|---|
| GPU | **单卡 RTX 5090，32GB 显存**；确认不是其他 24GB 型号 |
| CPU | 8 核起，优先 16 核 |
| 系统内存 | **建议 64GB，至少 48GB**；不要把显存当作系统内存 |
| 磁盘 | 可用数据盘至少 100GB，建议 **150GB** |
| 系统 / Python | Linux x86_64，Ubuntu 22.04/24.04，Python 3.12（或 3.11） |
| 驱动 | 首版固定 vLLM 0.29.0；选支持其 CUDA 13.0 wheel 的宿主 NVIDIA 驱动，通常 580 系列或更新；租前核对实例驱动信息 |
| 计费 | 先按量，不必包月 |

vLLM 0.29.0 默认 CUDA 13.0 构建，版本依据 [官方发布页](https://github.com/vllm-project/vllm/releases/tag/v0.29.0)。镜像自带的 PyTorch 不替代宿主驱动；脚本使用独立 venv，安装后会检查 `torch.cuda.is_available()`。驱动不匹配时先停止，选择兼容实例/官方构建，不盲目修改宿主驱动。

生成：[Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B)，BF16，服务窗口 8192 token、初始最大序列数 4；embedding：[Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)，BF16、1024 维。两个服务的显存预算分别为 80% / 12%，其余留作余量。此配置是起始预算，**不承诺 4 个用户各跑满 8K 的实际容量**；需租后试跑、调参和基准测试。

4090 24GB 不适用这份初始双服务 BF16 预设，安装脚本会拒绝不足 30GiB 的卡。后续可单独设计量化/CPU embedding 配置；SFT、GRPO 另定训练资源。

官网目前展示参考价 5090 32GB 约 2.78 元/时，实际依实例与库存而变：[AutoDL 官网](https://autodl.com/)。实例开机即计时，空闲也收费；付费磁盘关机后可能继续收费，见 [计费说明](https://www.autodl.com/docs/price/)。完成本机代码准备后再开 GPU。

## 第一步：在 AutoDL 上部署模型

创建实例后，打开 AutoDL 的 JupyterLab 终端或通过它提供的 SSH 登录命令连接。SSH 密码只在你本机/终端输入，不发到聊天。

在 **AutoDL 的 Linux 终端**执行：

```bash
cd /root/autodl-tmp
git clone --branch codex/project-improvements-20261101 --single-branch https://github.com/approfile/mindbridge-py.git
cd mindbridge-py
bash deploy/autodl/install.sh
```

脚本安装推理依赖、下载模型、记录首次下载的精确 revision，生成私有服务 Key。只拉取仓库，无需上传本机 `.env`、MySQL 数据或原有云端 Key。首次下载较大，等待终端结束。

安装脚本默认使用官方 HTTPS PyPI，缓存放在数据盘；可用 `MINDBRIDGE_PIP_INDEX_URL` 指定其他经过确认的 HTTPS 镜像。不会修改系统 pip 配置。如果旧版源码包打印 `Looking in indexes: http://mirrors.aliyun.com/...`，随后显示 `from versions: none`，只能说明该源未提供可用候选，不能据此断定版本不存在或显卡不兼容。在当前终端执行 `export PIP_INDEX_URL=https://pypi.org/simple` 后重新运行安装脚本即可覆盖旧源；若仍失败，保留 pip 最后的网络/依赖错误再排查，不盲目更换版本或关闭 TLS 验证。

如果访问 GitHub/Hugging Face 失败，先按 [AutoDL 网络加速文档](https://www.autodl.com/docs/network_turbo/)启用平台提供的加速，再重试。不要在终端输出或截图里展示代理凭据。若此前下载中断，原目录内重新执行安装即可；已成功记录的模型 revision 会保持锁定。

推荐在 tmux 中运行服务：

```bash
tmux new -s mindbridge-models
bash deploy/autodl/start.sh
```

如果 `tmux` 未安装，可在 AutoDL 容器中执行 `apt-get update && apt-get install -y tmux`。等待出现 `Both services ready`。按 **Ctrl+B，然后 D** 将服务留在后台；下次 `tmux attach -t mindbridge-models` 返回。**Ctrl+C 会停止两个模型服务**。

只监听服务器回环地址 8000 / 8001，不开公网端口。Key 在 `/root/autodl-tmp/mindbridge-models/service.key`，日志和 manifest 也在该目录。不要把 Key 贴到聊天或 GitHub。启动失败查看 `chat.log` / `embedding.log`，分享前确认没有私有内容。

### FlashInfer 采样器启动失败

如果聊天服务在采样器预热时出现 `FlashInfer requires GPUs with sm75 or higher`，不能仅凭这条错误认定 5090 不受支持。需结合堆栈检查 FlashInfer JIT 与镜像内 CUDA toolkit 的兼容性；目前实例尚未完成真实双服务验收。

本预设默认设置 `VLLM_USE_FLASHINFER_SAMPLER=0`，使用 vLLM 的 PyTorch 原生采样路径，仍然在 GPU 上推理，模型大小与显存预算不变。开关依据 [vLLM 0.29.0 官方说明](https://docs.vllm.ai/en/v0.29.0/configuration/env_vars/)。这属于启动兼容设置，吞吐和延迟仍需后续实测；确认 CUDA toolkit 兼容后可显式设置为 `1` 再对比。

旧版源码包无需重新安装依赖或下载模型，在没有现存 tmux 会话时直接执行：

```bash
tmux new-session -s mindbridge-models -c /root/autodl-tmp/mindbridge-py \
  'env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY VLLM_USE_FLASHINFER_SAMPLER=0 bash deploy/autodl/start.sh; exec bash'
```

上述命令会创建 tmux 会话并启动服务。等待 `Both services ready` 后，再按一次 Ctrl+B、松开后按 D；若仍失败，在另一个终端查看 `tail -n 100 /root/autodl-tmp/mindbridge-models/chat.log`。新版 readiness 检查始终直连回环地址，避免下载代理接收服务鉴权信息；旧版通过上面命令清除代理变量达到相同目的。

## 第二步：在你本机建立 SSH 隧道

返回 **Windows 本机 PowerShell**，进入本项目：

```powershell
Set-Location 'D:\Program Files\Github\mindbridge-py'
.\scripts\connect-autodl.ps1 -HostName '替换为AutoDL的SSH主机名' -Port 12345
```

把主机名与端口替换为实例提供的 SSH 信息；例如平台命令 `ssh -p 12345 root@某主机`，这里分别填写该主机和 `12345`。若登录用户不是 root，追加 `-UserName 用户名`。

该脚本会：生成专用于模型隧道的 SSH Key，要求你核对服务器指纹并输入 SSH 密码，将公钥安装到服务器，再通过 SCP 私下取回服务 Key 和模型 manifest。随后只更新本机 `.env` 的 `SELFHOST_*` 项，保留原有云端配置。`.local-runtime/autodl/` 与 `.env` 均已忽略，不提交 Git。

确认实例保持开机、模型仍运行，然后执行：

```powershell
.\scripts\start-local.ps1 -RealModels -WithSelfhost
docker compose -f docker-compose.local.yml -f docker-compose.selfhost.yml exec -T app python scripts/probe_selfhost.py
```

隧道在本机 Docker 私有网络中运行，不在 Windows 发布推理端口；SSH 严格检查已确认的 host key。最后的 probe 直接调用真实 GPU 服务，检查普通/流式/JSON/审核/上下文声明/向量维度，结果写入 `target/selfhost-probe/`。这仍不是完整业务基准。

如果只想以自部署为用户选项、部署默认模式暂时保留 mock，可用 `-WithSelfhost` 而不加 `-RealModels`；账号保存完整自部署配置后仍会使用真实模型。已有云端 `.env` 配置的当前开发环境建议使用上面带 `-RealModels` 的命令。

## 第三步：在产品里选择模式

打开 <http://127.0.0.1:8080>，用学生账号登录，点击“模型设置”：

1. 选择“完整自部署”。
2. 对话选 `Qwen/Qwen3-8B`，检索选 `Qwen/Qwen3-Embedding-0.6B`，维度 1024；无需个人 Key。
3. 点击“测试并保存”，等独立索引建立并切换成功。
4. 新建会话，测试普通对话、咨询、空证据与风险场景；在管理端查看每轮 profile revision 和用量。
5. 切换回个人 API 模式时，分别填写 DeepSeek 和 DashScope Key，重新测试保存。

保存自部署配置后，可在本机执行两轮合成 HTTP 冒烟（消耗少量 GPU 推理时间，不修改模型配置）：

```powershell
docker compose -f docker-compose.local.yml -f docker-compose.selfhost.yml exec -T app python scripts/smoke_model_profile.py --expect-mode selfhost
```

脚本默认使用现有学生演示账号，报告写入 `target/model-profile-smoke/`；分别记录正常完成与兜底，不把 SSE 完成当作质量评审。

后续正式验收还需：真实 GPU 完整链路、禁用外部模型 API 后的运行、双账号并发隔离、同口径 1/2/4 并发及质量评测、GPU 时长/吞吐/成功请求成本。全部完成前不标记阶段 3 已验收。

## 关机与恢复

不测时在 AutoDL 控制台关机以停止 GPU 开机计费。关机后自部署模式会不可用，不会自动切到云端；可在产品中明确切换 API 模式。重新开机后回服务器执行 `bash deploy/autodl/start.sh`；本机隧道失败会按重启策略重连。不要反复执行连接脚本来替代启动模型。

更换实例/SSH 地址后，重新执行本机连接脚本并重建本机栈。新模型 revision 需要重新测试并保存个人配置。释放实例前备份模型锁文件与必要产物；本地数据盘的可靠性边界见 [AutoDL 文档](https://www.autodl.com/docs/local_disk/)。
