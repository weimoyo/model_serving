# DGX Spark 手动接入与大模型部署教程

本教程适用于：控制面/Gateway 运行在 Windows 本机，DGX Spark 作为远端
Linux + NVIDIA + Docker 推理节点。所有命令都在项目当前实现范围内，不依赖
Snowpack。

> 本文第 1–6 节仍可用于一次性节点引导。节点注册完成后，不再按旧的“直接仓库”
> 方式部署；存储授权、已有模型扫描、模型下载、Artifact、Runtime Profile 和只读
> 挂载部署统一按 [第一阶段受管流程](phase1_managed_workflow.md) 操作。

> 当前 MVP 每个 Deployment 固定 `desired_replicas=1`，也不把两台 Spark
> 聚合为一个分布式 vLLM 实例。多节点数据库结构已保留，但本文是一台 Spark、
> 一张 GB10、一个 Replica 的流程。

## 1. 准备参数和网络

先确定：

- `GATEWAY_IP`：运行本项目的 Windows 主机局域网地址；
- `SPARK_IP`：DGX Spark 局域网地址；
- `SPARK_USER`：可以 SSH 登录并使用 sudo 的账户；
- Gateway API Key：客户端访问本机 Gateway 的密钥；
- Node Agent Key：只供 Gateway 调用这一台 Spark Agent，必须与 Gateway Key 不同。

网络方向：

```text
Windows Gateway -> SPARK_IP:9010          Node Agent API
Windows Gateway -> SPARK_IP:<动态高端口>   Replica 容器 SSH
Spark           -> Hugging Face / NGC     拉取模型和镜像
```

Node Agent 使用 `docker run -p 22` 为每个 Replica 分配动态 SSH 端口。请只允许
`GATEWAY_IP` 访问 Spark 的 TCP 9010 和 Docker 动态端口；跨不可信网络时应先建立
VPN，不要把 Agent HTTP 直接暴露到公网。

## 2. Spark 预检

在 Spark 上执行：

```bash
uname -m
nvidia-smi --query-gpu=uuid,name --format=csv,noheader
docker version
nvidia-ctk cdi list
free -h
df -h
```

预期架构为 `aarch64`，能看到 GB10 GPU UUID、Docker Server 和
`nvidia.com/gpu=...` CDI 设备。若 Docker 无 GPU：

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

若当前 SSH 用户不能直接运行 Docker，可执行一次并重新登录：

```bash
sudo usermod -aG docker "$USER"
```

再用一个适配 Spark 的 ARM64 CUDA/vLLM 镜像执行 `nvidia-smi` 验证容器 GPU。
不要使用只有 amd64 架构的镜像。

## 3. 把 Node Agent 和 runtime 构建文件传到 Spark

在 Windows 项目根目录执行，把三个占位符替换为实际值：

```powershell
$Spark = '<SPARK_USER>@<SPARK_IP>'
scp node_agent.py "${Spark}:/tmp/node_agent.py"
scp deploy/model-serving-agent.service "${Spark}:/tmp/model-serving-agent.service"
scp -r runtime/llm-vllm-ssh "${Spark}:/tmp/llm-vllm-ssh"
```

在 Spark 上安装 Agent：

```bash
sudo install -d -m 0755 /opt/model-serving
sudo install -m 0644 /tmp/node_agent.py /opt/model-serving/node_agent.py
sudo python3 -m venv /opt/model-serving/.venv
sudo /opt/model-serving/.venv/bin/pip install --upgrade pip
sudo /opt/model-serving/.venv/bin/pip install 'fastapi>=0.110,<1' 'pydantic>=2,<3' 'uvicorn>=0.29,<1' 'huggingface-hub>=0.27,<2'
sudo install -m 0644 /tmp/model-serving-agent.service /etc/systemd/system/model-serving-agent.service
sudo install -d -m 0750 /var/lib/model-serving-agent
```

生成每节点独立密钥：

```bash
openssl rand -hex 32
sudoedit /etc/model-serving-agent.env
```

环境文件只写一行，并把值替换为刚生成的密钥：

```dotenv
NODE_AGENT_API_KEY=<SPARK_NODE_AGENT_KEY>
```

启动并检查：

```bash
sudo chmod 600 /etc/model-serving-agent.env
sudo systemctl daemon-reload
sudo systemctl enable --now model-serving-agent
sudo systemctl status model-serving-agent --no-pager
curl http://127.0.0.1:9010/healthz
curl -H 'Authorization: Bearer <SPARK_NODE_AGENT_KEY>' \
  http://127.0.0.1:9010/v1/inventory | python3 -m json.tool
```

inventory 中应有 `"runtime":"vllm"`，并且 `accelerators` 至少一项。DGX Spark
统一内存可能使 `memory_gib` 显示 `null`，不影响 UUID 调度。

## 4. 构建本项目的 vLLM + SSH runtime

本项目需要在 NVIDIA vLLM 基础镜像上加入 sshd。Spark 的镜像、模型格式和
启动参数必须来自同一套兼容配方。首次尝试可以使用项目默认的 Spark-capable
NVIDIA 25.09 基础镜像；正式部署建议把下面的基础镜像替换为 NVIDIA 当前
DGX Spark recipe 指定的版本。

```bash
cd /tmp/llm-vllm-ssh
sudo docker build \
  --build-arg VLLM_BASE_IMAGE=nvcr.io/nvidia/vllm:25.09-py3 \
  -t model-serving/vllm-ssh:spark .
sudo docker image inspect model-serving/vllm-ssh:spark --format '{{.Id}}'
```

记下输出的 `sha256:...`。后续把这个 image ID 作为 `runtime_image`，可以避免
本地 tag 被改写。如果拉取 NGC 镜像要求登录，先按 NGC 指引执行
`docker login nvcr.io`。

## 5. 让 Windows Gateway 获得 Spark Agent Key

Gateway 从进程环境变量读取每台节点的 Agent Key，数据库只保存变量名。停止
当前 Gateway 后，在 Windows 项目根目录重新启动：

```powershell
$env:MODEL_SERVING_API_KEY = '<GATEWAY_API_KEY>'
$env:SPARK01_AGENT_KEY = '<SPARK_NODE_AGENT_KEY>'
$env:MODEL_SERVING_STATE_DIR = (Resolve-Path .state).Path
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8080
```

保持该窗口运行。首次启动会自动生成 `.state/gateway_ed25519`；创建 Replica 时，
Gateway 公钥会通过 Agent 写入容器，因此无需手工向 Spark 注入 Gateway 私钥。

另开一个 PowerShell：

```powershell
$GatewayKey = '<GATEWAY_API_KEY>'
$Headers = @{ Authorization = "Bearer $GatewayKey" }
Invoke-RestMethod http://127.0.0.1:8080/readyz
```

## 6. 注册 Spark 算力

```powershell
$Node = @{
  id = 'spark-01'
  name = 'DGX Spark 01'
  host = '<SPARK_IP>'
  agent_url = 'http://<SPARK_IP>:9010'
  agent_key_env = 'SPARK01_AGENT_KEY'
  platform = 'DGX Spark GB10'
  network_zone = 'lan'
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8080/api/nodes `
  -Headers $Headers -ContentType application/json -Body $Node |
  ConvertTo-Json -Depth 8
```

成功标志：节点 `state` 为 `ONLINE`，inventory 中为 `runtime=vllm`，并出现
真实 GPU UUID。也可以访问 `http://127.0.0.1:8080/` 查看卡片。

## 7. 注册一个大模型

下面用公开的 `Qwen/Qwen3-30B-A3B-Instruct-2507` 演示。该仓库约 61 GB，
支持 vLLM；请先用较短上下文完成首次 Gate。不要填 `main`，应实时读取不可变
commit：

```powershell
$ModelRepo = 'Qwen/Qwen3-30B-A3B-Instruct-2507'
$ModelInfo = Invoke-RestMethod "https://huggingface.co/api/models/$ModelRepo"
$ModelSha = $ModelInfo.sha
$ModelSha

$Model = @{
  id = 'qwen3-30b-a3b'
  display_name = 'Qwen3 30B A3B Instruct 2507'
  family = 'Qwen3 MoE'
  task = 'chat'
  capabilities = @('chat', 'streaming')
  repository = $ModelRepo
  revision = $ModelSha
  parameter_count = 30500000000
  precision = 'BF16'
  context_length = 262144
  license = 'Apache-2.0'
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8080/api/models `
  -Headers $Headers -ContentType application/json -Body $Model |
  ConvertTo-Json -Depth 6
```

如需 gated/private 模型，当前基础流程还需要安全地向容器提供 HF Token；不要把
token 放进 `runtime_args` 或数据库。建议首次验收先用公开模型。

## 8. 创建 Deployment

先从 Spark 取得刚构建的不可变 image ID：

```powershell
$RuntimeImage = (ssh <SPARK_USER>@<SPARK_IP> `
  "docker image inspect model-serving/vllm-ssh:spark --format '{{.Id}}'").Trim()
$RuntimeImage
```

创建单副本 Deployment：

```powershell
$Deployment = @{
  id = 'qwen3-30b-spark-prod'
  name = 'Qwen3 30B on DGX Spark'
  model_id = 'qwen3-30b-a3b'
  served_model_id = 'qwen3-30b-a3b'
  desired_replicas = 1
  runtime = 'vllm'
  runtime_image = $RuntimeImage
  gpu_count_per_replica = 1
  max_concurrency = 1
  runtime_args = @{
    max_model_len = 32768
    gpu_memory_utilization = 0.75
    max_num_seqs = 1
    enforce_eager = $true
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8080/api/deployments `
  -Headers $Headers -ContentType application/json -Body $Deployment |
  ConvertTo-Json -Depth 10
```

第一次启动需要下载约 61 GB 权重，Replica 会依次经过 `STARTING`、
`UNHEALTHY`，最终达到 `READY`。Agent 使用共享 Docker volume
`model-serving-hf-cache`，故障 replacement 会复用已下载的模型缓存。

循环查看状态：

```powershell
while ($true) {
  $D = Invoke-RestMethod `
    http://127.0.0.1:8080/api/deployments/qwen3-30b-spark-prod `
    -Headers $Headers
  $R = $D.replicas[-1]
  "$(Get-Date -Format HH:mm:ss) epoch=$($R.instance_epoch) state=$($R.state) runtime=$($R.container_state) reason=$($R.failure_reason)"
  if ($R.state -eq 'READY') { break }
  Start-Sleep 10
}
```

在 Spark 上看真实加载日志：

```bash
sudo docker ps --filter label=model-serving.replica
sudo docker logs -f <上一步显示的-runtime_id>
```

如果长时间不 READY，先不要反复创建 Deployment；检查日志并参照文末排错。

## 9. 测试 OpenAI-compatible API

模型列表：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models -Headers $Headers |
  ConvertTo-Json -Depth 5
```

非流式：

```powershell
$Chat = @{
  model = 'qwen3-30b-a3b'
  messages = @(@{ role = 'user'; content = '请用三句话介绍 DGX Spark。' })
  stream = $false
  max_tokens = 256
  temperature = 0.2
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8080/v1/chat/completions `
  -Headers $Headers -ContentType application/json -Body $Chat |
  ConvertTo-Json -Depth 10
```

流式：

```powershell
curl.exe -N http://127.0.0.1:8080/v1/chat/completions `
  -H "Authorization: Bearer $GatewayKey" `
  -H 'Content-Type: application/json' `
  -d '{"model":"qwen3-30b-a3b","messages":[{"role":"user","content":"写一句流式测试。"}],"stream":true,"max_tokens":64}'
```

成功流式响应应包含多个 `data:` 事件，并以 `data: [DONE]` 结束。

## 10. 故障摘流与恢复 Gate

从 Deployment 响应中找到当前 `runtime_id`，在 Spark 上执行：

```bash
sudo docker kill <runtime_id>
```

然后在 Windows 立即触发一次收敛：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8080/api/reconcile -Headers $Headers
Invoke-RestMethod http://127.0.0.1:8080/v1/models -Headers $Headers |
  ConvertTo-Json -Depth 5
```

预期旧 Replica 为 `FAILED`，在新 Replica READY 前模型不会出现在
`/v1/models`，对话请求返回 503；控制器随后创建更高 `instance_epoch` 的新
Replica，并复用共享模型缓存恢复到 READY。

## 11. 删除 Deployment

```powershell
Invoke-RestMethod -Method Delete `
  http://127.0.0.1:8080/api/deployments/qwen3-30b-spark-prod `
  -Headers $Headers | ConvertTo-Json -Depth 10
```

这会删除运行容器，但保留共享模型缓存。只有明确不再需要模型缓存时，才在
Spark 上手工删除 `model-serving-hf-cache` volume。

## 12. 常见问题

| 现象 | 首要检查 |
|---|---|
| 节点 `OFFLINE` | 9010 网络、Agent Key 环境变量名、`journalctl -u model-serving-agent` |
| accelerators 为空 | 使用最新版 `node_agent.py`；检查 `nvidia-ctk cdi list` 和驱动 |
| `no online node has ...` | 节点是否 ONLINE、GPU UUID 是否已被另一个活动 Deployment 占用 |
| `runtime creation failed` | Spark 上是否存在该 image ID、Docker/CDI 是否可用 |
| `SSH tunnel exited` | Gateway 到容器动态 SSH 端口是否被防火墙拦截 |
| 长时间 `UNHEALTHY` | `docker logs`；通常是首次下载、模型/镜像不兼容或内存不足 |
| OOM | 降低 `max_model_len`、`max_num_seqs`、`gpu_memory_utilization`，或改用 Spark recipe 的 NVFP4 模型 |
| served model absent | `served_model_id` 必须与 vLLM 的 `--served-model-name` 完全一致 |

NVIDIA 当前建议优先使用 DGX Spark recipe 中成套提供的模型、镜像和参数，不能
只按模型大小判断兼容性。若切换到 NVFP4 或其他新模型，请整体替换本教程的
模型仓库、基础镜像和 `runtime_args`，不要混搭不同 recipe。
