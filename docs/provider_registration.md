# 算力提供者一键注册指南

本流程面向 Linux 和 NVIDIA GPU 算力提供者。对方只需拉取本项目、取得一个短期
Enrollment Token，然后运行 `scripts/register_provider.sh`。对方不需要控制面管理员
API Key，也不需要手工编辑 systemd、Agent 密钥或 JSON。

## 1. 平台管理员准备

节点必须能访问控制面，控制面也必须能反向访问节点的 Agent 端口和随后由 Docker
发布的 Replica SSH 端口。正式环境应在控制面前配置 HTTPS 反向代理。可信实验室局域网
可以临时让控制面监听局域网地址：

```powershell
.\scripts\start_control.ps1 -BindHost 0.0.0.0 -Port 8080
```

不要把未加 TLS 的管理接口直接暴露到公网。

为一个提供者创建一次性、60 分钟有效的 Enrollment Token：

```powershell
.\scripts\create_enrollment_token.ps1 `
  -TtlMinutes 60 `
  -Uses 1 `
  -OutFile .state\provider-enrollment.token
```

通过安全渠道把 Token 文件和 Git 仓库地址交给提供者。Token 的哈希进入数据库，明文只在
创建时返回；使用一次、过期或撤销后都不能再次注册。

## 2. 提供者机器要求

- Linux；
- Python 3.11 或更高版本，并安装 `venv`；
- Docker daemon；
- NVIDIA 驱动和可工作的 `nvidia-smi`；
- NVIDIA Container Toolkit，包含 `nvidia-ctk`；
- 能访问 Git、Python 包源和所选 vLLM 基础镜像；
- 能访问平台提供的 HTTPS 控制面地址。

脚本不会自动升级显卡驱动、Docker 或内核。缺少基础组件时会停止并给出缺失项，避免破坏
提供者已有 CUDA 环境。

## 3. 提供者交互式注册

```bash
git clone <MODEL_SERVING_GIT_URL>
cd model_serving
sudo -E bash scripts/register_provider.sh
```

脚本依次询问：

- 控制面 URL；
- Node ID、显示名称和控制面可达地址；
- 网络区域；
- 模型、workspace、cache 和 output 目录；
- 短期 Enrollment Token。

正式环境必须使用 HTTPS。实验室可信局域网临时使用 HTTP 时，必须显式添加：

```bash
sudo -E bash scripts/register_provider.sh \
  --control-url http://192.168.1.10:8080 \
  --allow-insecure-http
```

## 4. 参数化注册

Token 不应直接出现在命令行。先把 Token 保存到仅 root 可读文件：

```bash
sudo install -m 0600 provider-enrollment.token /root/provider-enrollment.token
```

然后执行：

```bash
sudo -E bash scripts/register_provider.sh \
  --control-url https://models.example.edu \
  --token-file /root/provider-enrollment.token \
  --node-id provider-dgx-01 \
  --name 'Provider DGX 01' \
  --host 10.20.30.41 \
  --network-zone provider-a \
  --model-dir /srv/model-serving/models \
  --workspace-dir /srv/model-serving/workspaces \
  --cache-dir /srv/model-serving/cache \
  --output-dir /srv/model-serving/output \
  --yes
```

使用已有、已满足 `vllm-ssh-v1` 契约的镜像时：

```bash
sudo -E bash scripts/register_provider.sh \
  --control-url https://models.example.edu \
  --token-file /root/provider-enrollment.token \
  --node-id provider-dgx-01 \
  --runtime-image registry.example.edu/model-serving/vllm-ssh@sha256:<digest> \
  --skip-build \
  --yes
```

运行 `bash scripts/register_provider.sh --help` 可以查看全部参数。

## 5. 脚本自动完成的工作

1. 检查 Linux、Python、Docker、NVIDIA 驱动和 Container Toolkit。
2. 重新生成并验证 NVIDIA CDI 设备清单。
3. 创建模型、workspace、cache、output 授权目录。
4. 安装独立 Node Agent venv 和 Python 依赖。
5. 生成每节点独立随机 Agent Key，并以 `0600` 保存。
6. 安装、启用并启动 `model-serving-agent.service`。
7. 校验 Agent health 和鉴权 inventory。
8. 构建或校验 vLLM + SSH runtime 镜像。
9. 使用真实 GPU 启动一次临时容器探针。
10. 使用一次性 Token 向控制面登记 Node、四类 Storage Pool 和 Runtime Profile。
11. 默认发起模型目录异步扫描。

节点 Agent Key 会通过注册时的 HTTPS 请求交给控制面，并保存在控制面本地 ACL 受限的
`.state/node-secrets/` 中；业务数据库只保存密钥引用名。API 响应不会回显该密钥。

## 6. 注册成功标志

终端最后应显示：

```text
Node: provider-dgx-01 (ONLINE)
Storage pools:
  - provider-dgx-01-models: model READY -> /srv/model-serving/models
  - provider-dgx-01-workspace: workspace READY -> /srv/model-serving/workspaces
Runtime profiles:
  - vllm-provider-dgx-01: vllm sha256:...
Initial scan: scan-... (QUEUED)
```

平台管理员可以检查：

```powershell
$Token = [IO.File]::ReadAllText((Resolve-Path .state\control.token)).Trim()
$Headers = @{ Authorization = "Bearer $Token" }
Invoke-RestMethod http://127.0.0.1:8080/api/nodes -Headers $Headers
Invoke-RestMethod http://127.0.0.1:8080/api/storage-pools -Headers $Headers
Invoke-RestMethod http://127.0.0.1:8080/api/runtime-profiles -Headers $Headers
Invoke-RestMethod http://127.0.0.1:8080/api/scans -Headers $Headers
```

## 7. 网络和安全边界

- Agent TCP 9010 只允许控制面地址访问。
- Docker 为 Replica 动态发布的 SSH 端口也只允许控制面访问。
- 正式注册必须走 HTTPS，不使用 `--allow-insecure-http`。
- 不要把 Enrollment Token、Agent Key、Hugging Face Token 提交到 Git。
- 提供者不应获得控制面管理员 API Key。
- 推理容器不会获得 Docker socket、Agent Key 或未授权宿主机目录。

注册失败后，Token 可能已被消费。修复节点端错误后，由平台管理员创建一个新的 Token 再次
执行脚本。脚本可以重复执行，会更新 Agent 安装、资源授权和节点信息。
