# Standalone Model Serving 完整使用手册

本文对应当前项目 1.1.x 实现。控制面运行在本机，算力节点运行 Node Agent 和推理
容器；系统不依赖 Snowpack。实时交互文档位于：

- Dashboard：`http://127.0.0.1:8080/`
- Swagger UI：`http://127.0.0.1:8080/docs`
- OpenAPI JSON：`http://127.0.0.1:8080/openapi.json`

## 1. 系统边界

控制面负责 Node、Storage Pool、Model、Artifact、Runtime Profile、Deployment、
Replica、调度、状态收敛、OpenAI 兼容网关和历史归档。Linux Node Agent 负责受限目录
操作、模型扫描/下载、Docker 容器生命周期和节点 inventory。推理容器只获得已授权的
模型只读挂载、独立 workspace、指定 GPU 和只读 SSH 公钥；不会获得 Docker socket、
控制面 API Key、Node Agent Key 或任意宿主机目录。

当前版本的重要限制：

- 每个 Deployment 固定 `desired_replicas=1`，数据库结构保留多副本扩展空间。
- Linux/NVIDIA 使用 vLLM + SSH 容器；Apple Silicon 使用 MLX Agent。
- 当前提供 chat completions，不提供 embeddings、rerank、audio、images 等 OpenAI API。
- 一个 Replica 使用一台节点上的一个或多个完整 GPU UUID；暂不做跨节点分布式 vLLM。

## 2. 启动、停止和鉴权

在 Windows 项目根目录启动：

```powershell
Set-Location D:\Workspace\MyCodes\model_serving
.\scripts\start_control.ps1
```

服务默认只监听 `127.0.0.1:8080`。`GET /healthz` 表示 HTTP 进程存活，
`GET /readyz` 还会检查数据库；二者不需要鉴权。

### 2.1 Dashboard 鉴权

本机打开 `/` 时，服务签发 `HttpOnly`、`SameSite=Strict` 的 session Cookie，页面中
无需填写 API Key。该自动会话仅对 loopback 客户端签发。

### 2.2 API/SDK 鉴权

除 `/`、`/healthz`、`/readyz`、`/docs`、`/openapi.json` 和一次性 Token 保护的
`POST /api/enroll` 外，接口需要以下任一种：

```http
Authorization: Bearer <CONTROL_API_KEY>
```

或：

```http
X-API-Key: <CONTROL_API_KEY>
```

本机 PowerShell 推荐从 ACL 受限文件读取，不把 Key 写入脚本：

```powershell
$Base = 'http://127.0.0.1:8080'
$Token = [IO.File]::ReadAllText((Resolve-Path .state\control.token)).Trim()
$Headers = @{ Authorization = "Bearer $Token" }
```

Node Agent Key 与控制面 Key 必须不同。节点数据库只保存密钥引用名，例如
`NODE_SPARK_01_AGENT_KEY`；手工注册可以通过进程环境或旧式 `.state/*_AGENT_KEY.token`
注入，一键注册的密钥保存在 ACL 受限的 `.state/node-secrets/` 中。

## 3. 公开接口总览

### 3.1 健康和页面

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/` | Dashboard，同时为本机浏览器建立会话 | 否 |
| GET | `/healthz` | 进程存活检查 | 否 |
| GET | `/readyz` | 数据库 readiness | 否 |
| GET | `/docs` | Swagger UI | 否 |
| GET | `/openapi.json` | OpenAPI 3 定义 | 否 |

### 3.2 节点和存储

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/nodes` | 注册或更新节点并立即 probe |
| GET | `/api/nodes?include_archived=false` | 列出节点 |
| POST | `/api/nodes/{node_id}/probe` | 立即重新采集 inventory |
| POST | `/api/nodes/{node_id}/archive` | 安全归档离线且无活跃 Replica 的节点 |
| PUT | `/api/nodes/{node_id}/storage-pools/{pool_id}` | 创建或更新授权存储池 |
| GET | `/api/storage-pools?node_id=...` | 列出全部或指定节点存储池 |
| DELETE | `/api/storage-pools/{pool_id}` | 撤销未被活跃 Deployment 使用的存储池 |

### 3.3 算力提供者自注册

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| POST | `/api/enrollment-tokens` | 创建短期、有限次数 Enrollment Token | 管理员 API Key |
| GET | `/api/enrollment-tokens` | 查看 Token 元数据，不返回明文 | 管理员 API Key |
| DELETE | `/api/enrollment-tokens/{token_id}` | 撤销未撤销的 Token | 管理员 API Key |
| POST | `/api/enroll` | 一次登记节点、密钥、资源池、Runtime Profile 和初始扫描 | Enrollment Token |

完整提供者操作见 `docs/provider_registration.md`。正式环境必须经 HTTPS 提交
Enrollment Token 和 Node Agent Key。

### 3.4 模型发现、下载和 Artifact

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/nodes/{node_id}/scans` | 在授权池内发起异步扫描 |
| GET | `/api/scans` | 列出扫描任务 |
| GET | `/api/scans/{job_id}` | 刷新任务状态；完成时自动登记 Artifact |
| DELETE | `/api/scans/{job_id}` | 请求取消扫描 |
| POST | `/api/nodes/{node_id}/downloads` | 从 Hugging Face 下载不可变 revision |
| GET | `/api/downloads` | 列出下载任务 |
| GET | `/api/downloads/{job_id}` | 刷新下载；完成时自动登记 Artifact |
| GET | `/api/artifacts` | 列出 Artifact 及其节点位置 |
| POST | `/api/artifacts/{artifact_id}/models/{model_id}` | 关联物理 Artifact 与逻辑 Model |
| POST | `/api/artifacts/{artifact_id}/locations/{location_id}/verify` | 重新校验节点文件 |

### 3.5 Model、Runtime Profile 和 Deployment

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/models` | 创建/幂等更新逻辑模型 |
| GET | `/api/models?include_archived=false` | 列出逻辑模型 |
| POST | `/api/models/{model_id}/archive` | 归档没有活跃 Deployment 的模型 |
| PUT | `/api/runtime-profiles/{profile_id}` | 创建或更新运行时白名单 |
| GET | `/api/runtime-profiles` | 列出 Runtime Profile |
| POST | `/api/deployments` | 创建单副本 Deployment 并触发调度 |
| GET | `/api/deployments?include_archived=false` | 列出 Deployment 和 Replica |
| GET | `/api/deployments/{deployment_id}` | 查询一个 Deployment |
| DELETE | `/api/deployments/{deployment_id}` | 排空、删除 runtime 并保留历史 |
| POST | `/api/reconcile` | 立即执行一次节点和 Replica 状态收敛 |
| GET | `/api/catalog?include_archived=false` | 一次获取 Dashboard/调度目录 |

### 3.6 推理和维护

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/models` | 仅发布当前真正可路由的逻辑模型 |
| POST | `/v1/chat/completions` | OpenAI 兼容非流式/SSE chat |
| POST | `/api/maintenance/archive-stale` | 软归档终态历史，不删除审计数据 |

归档接口查询参数：

- `terminal_replica_hours`：FAILED/RELEASED Replica 保留小时数，默认 24。
- `deleted_deployment_hours`：DELETED Deployment 保留小时数，默认 24。
- `completed_job_hours`：完成/失败/取消任务保留小时数，默认 168。

后台会持续使用默认保留期自动归档。传入 `0` 可立即归档；归档记录可通过
`include_archived=true` 查询。

## 4. 主要请求结构

### 4.1 NodeCreate

```json
{
  "id": "spark-01",
  "name": "DGX Spark 01",
  "host": "172.18.166.16",
  "agent_url": "http://172.18.166.16:9010",
  "agent_key_env": "SPARK01_AGENT_KEY",
  "platform": "DGX Spark GB10",
  "network_zone": "lan"
}
```

`id` 只能包含字母、数字、点、下划线和横线；`agent_key_env` 必须是合法环境变量名。
成功响应包含 inventory，例如架构、CPU、内存、Docker、GPU UUID、runtime image。

### 4.2 EnrollmentTokenCreate 和 NodeEnrollmentCreate

管理员先创建短期 Token：

```json
{"ttl_minutes": 60, "uses": 1}
```

`POST /api/enroll` 接收一次性完整注册包：

```json
{
  "enrollment_token": "mse_<one-time-token>",
  "agent_key": "<per-node-random-secret>",
  "node": {"id":"provider-01","name":"Provider 01","host":"10.20.30.41","agent_url":"http://10.20.30.41:9010","agent_key_env":"IGNORED_BY_SERVER"},
  "storage_pools": [],
  "runtime_profiles": [],
  "scan_existing_models": true
}
```

控制面根据 Node ID 生成密钥引用名，安全保存 `agent_key`，不会在响应中回显 Token 或
Agent Key。推荐直接使用 `scripts/register_provider.sh` 生成并提交该结构。

### 4.3 StoragePoolCreate

```json
{
  "id": "spark1-models",
  "host_path": "/srv/model-serving/models",
  "container_path": "/models",
  "purpose": "model",
  "agent_access": "rw",
  "runtime_access": "ro",
  "quota_gib": 200,
  "min_free_gib": 20
}
```

- `purpose`：`model`、`workspace`、`cache`、`output`。
- `agent_access`：`ro` 或 `rw`。
- `runtime_access`：`none`、`ro` 或 `rw`。
- 推荐模型池 `agent=rw,runtime=ro`；workspace 为 `rw,rw`。
- API 授权不能突破 Agent systemd `ReadWritePaths` 等操作系统沙箱。

### 4.4 ScanCreate

```json
{"storage_pool_id":"spark1-models","relative_path":"."}
```

只接受池内相对路径。Agent 拒绝绝对路径、`..` 和符号链接逃逸。任务完成后，扫描结果
中的每个条目获得 `artifact_id`。

### 4.5 DownloadCreate

```json
{
  "storage_pool_id": "spark1-models",
  "repository": "Qwen/Qwen2.5-0.5B-Instruct",
  "revision": "7ae557604adf67be50417f59c2c2f167def9a775"
}
```

`revision` 必须为 7–64 位十六进制 commit，不接受 `main`、`latest` 或 tag。可选
`relative_path` 必须仍在授权池内。下载先进入 `.staging`，结构和配额校验通过后原子发布。

### 4.6 ModelCreate

```json
{
  "id": "qwen2.5-0.5b-instruct",
  "display_name": "Qwen2.5 0.5B Instruct",
  "family": "Qwen2.5",
  "task": "chat",
  "capabilities": ["chat", "streaming"],
  "repository": "Qwen/Qwen2.5-0.5B-Instruct",
  "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
  "parameter_count": 500000000,
  "precision": "BF16",
  "context_length": 32768,
  "license": "Apache-2.0"
}
```

`task` 可取 `chat`、`completion`、`embedding`、`rerank`、`other`，但当前网关只实现
chat。相同 Model ID 的 repository/revision 不允许改变。

### 4.7 RuntimeProfileCreate

```json
{
  "id": "vllm-spark-gb10",
  "name": "vLLM SSH on DGX Spark GB10",
  "runtime": "vllm",
  "image": "sha256:<image-id>",
  "architectures": ["aarch64"],
  "accelerator_kinds": ["GPU"],
  "allowed_args": [
    "max_model_len", "gpu_memory_utilization", "max_num_seqs",
    "enforce_eager", "dtype"
  ],
  "enabled": true
}
```

Deployment 的 `runtime_args` 只能使用 `allowed_args`。镜像、挂载、Docker 参数、SSH
配置和环境边界不能从 Deployment 任意注入。

### 4.8 DeploymentCreate

```json
{
  "id": "qwen2.5-0.5b-spark1-v8",
  "name": "Qwen2.5 0.5B on Spark 1 v8",
  "model_id": "qwen2.5-0.5b-instruct",
  "served_model_id": "qwen2.5-0.5b-instruct",
  "desired_replicas": 1,
  "artifact_id": "artifact-0da54de0495eae49b2cebb7b",
  "runtime_profile_id": "vllm-spark-gb10",
  "workspace_storage_pool_id": "spark1-workspace",
  "runtime": "vllm",
  "runtime_args": {
    "max_model_len": 4096,
    "gpu_memory_utilization": 0.25,
    "max_num_seqs": 1,
    "enforce_eager": true,
    "dtype": "auto"
  },
  "gpu_count_per_replica": 1,
  "max_concurrency": 1
}
```

使用 Runtime Profile 时 `runtime_image` 由 profile 决定。调度要求节点 ONLINE、runtime
和架构匹配、GPU 空闲、Artifact location READY、模型池可读、workspace 可写。

### 4.9 ChatRequest

```json
{
  "model": "qwen2.5-0.5b-instruct",
  "messages": [
    {"role": "system", "content": "回答要简洁。"},
    {"role": "user", "content": "你好"}
  ],
  "stream": false,
  "temperature": 0.2,
  "max_tokens": 256
}
```

`model` 使用逻辑 Model ID，而不是 Artifact ID、Deployment ID 或宿主机路径。除
`model/messages/stream` 外的兼容字段会透传给 vLLM。

## 5. 从零到真实推理的完整流程

以下命令全部在控制面 Windows 项目根目录执行；节点引导完成后无需登录 Spark。

### 5.1 初始化变量并检查服务

```powershell
$Base = 'http://127.0.0.1:8080'
$Token = [IO.File]::ReadAllText((Resolve-Path .state\control.token)).Trim()
$Headers = @{ Authorization = "Bearer $Token" }
$NodeId = 'spark-01'

Invoke-RestMethod "$Base/healthz"
Invoke-RestMethod "$Base/readyz"
Invoke-RestMethod "$Base/api/nodes/$NodeId/probe" -Method Post -Headers $Headers |
  ConvertTo-Json -Depth 8
```

节点必须为 `ONLINE`，inventory 中应存在目标 runtime、架构和 GPU UUID。

### 5.2 注册模型池和 workspace

```powershell
$ModelPool = @{
  id='spark1-models'; host_path='/srv/model-serving/models'; container_path='/models'
  purpose='model'; agent_access='rw'; runtime_access='ro'
  quota_gib=200; min_free_gib=20
} | ConvertTo-Json

$WorkspacePool = @{
  id='spark1-workspace'; host_path='/srv/model-serving/workspaces'; container_path='/workspace'
  purpose='workspace'; agent_access='rw'; runtime_access='rw'
  quota_gib=50; min_free_gib=10
} | ConvertTo-Json

Invoke-RestMethod "$Base/api/nodes/$NodeId/storage-pools/spark1-models" `
  -Method Put -Headers $Headers -ContentType application/json -Body $ModelPool
Invoke-RestMethod "$Base/api/nodes/$NodeId/storage-pools/spark1-workspace" `
  -Method Put -Headers $Headers -ContentType application/json -Body $WorkspacePool
```

### 5.3 选择扫描已有模型或发起下载

扫描：

```powershell
$Job = Invoke-RestMethod "$Base/api/nodes/$NodeId/scans" -Method Post `
  -Headers $Headers -ContentType application/json `
  -Body '{"storage_pool_id":"spark1-models","relative_path":"."}'

do {
  Start-Sleep 2
  $Job = Invoke-RestMethod "$Base/api/scans/$($Job.id)" -Headers $Headers
} while ($Job.state -in @('QUEUED','RUNNING','CANCEL_REQUESTED'))
$Job | ConvertTo-Json -Depth 10
```

下载：

```powershell
$Job = Invoke-RestMethod "$Base/api/nodes/$NodeId/downloads" -Method Post `
  -Headers $Headers -ContentType application/json -Body @'
{
  "storage_pool_id":"spark1-models",
  "repository":"Qwen/Qwen2.5-0.5B-Instruct",
  "revision":"7ae557604adf67be50417f59c2c2f167def9a775"
}
'@

do {
  Start-Sleep 3
  $Job = Invoke-RestMethod "$Base/api/downloads/$($Job.id)" -Headers $Headers
} while ($Job.state -in @('QUEUED','RUNNING','VALIDATING'))
$ArtifactId = $Job.artifact_id
```

### 5.4 创建 Model、绑定 Artifact

```powershell
$ModelBody = @{
  id='qwen2.5-0.5b-instruct'; display_name='Qwen2.5 0.5B Instruct'
  family='Qwen2.5'; task='chat'; capabilities=@('chat','streaming')
  repository='Qwen/Qwen2.5-0.5B-Instruct'
  revision='7ae557604adf67be50417f59c2c2f167def9a775'
  parameter_count=500000000; precision='BF16'; context_length=32768
  license='Apache-2.0'
} | ConvertTo-Json

Invoke-RestMethod "$Base/api/models" -Method Post -Headers $Headers `
  -ContentType application/json -Body $ModelBody
Invoke-RestMethod "$Base/api/artifacts/$ArtifactId/models/qwen2.5-0.5b-instruct" `
  -Method Post -Headers $Headers
```

### 5.5 创建 Runtime Profile 和 Deployment

先用 `GET /api/runtime-profiles` 检查是否已有合适 profile。然后创建 Deployment：

```powershell
$DeploymentBody = @{
  id='qwen-small-spark1'; name='Qwen Small Spark 1'
  model_id='qwen2.5-0.5b-instruct'; served_model_id='qwen2.5-0.5b-instruct'
  desired_replicas=1; artifact_id=$ArtifactId
  runtime_profile_id='vllm-spark-gb10'
  workspace_storage_pool_id='spark1-workspace'
  runtime='vllm'; gpu_count_per_replica=1; max_concurrency=1
  runtime_args=@{
    max_model_len=4096; gpu_memory_utilization=0.25
    max_num_seqs=1; enforce_eager=$true; dtype='auto'
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "$Base/api/deployments" -Method Post -Headers $Headers `
  -ContentType application/json -Body $DeploymentBody
```

### 5.6 等待 READY

```powershell
do {
  Start-Sleep 5
  $Deployment = Invoke-RestMethod "$Base/api/deployments/qwen-small-spark1" `
    -Headers $Headers
  $Deployment.replicas | Format-Table `
    id,state,container_state,endpoint_healthy,model_runtime_ready,failure_reason
} while (@($Deployment.replicas | Where-Object state -eq 'READY').Count -eq 0)
```

只有 Replica 同时满足 `READY`、container running、endpoint health、模型 identity、
节点 ONLINE，才会进入 `/v1/models` 和请求路由。

### 5.7 非流式请求

```powershell
$Body = @{
  model='qwen2.5-0.5b-instruct'
  messages=@(@{role='user';content='只回复 MODEL_OK'})
  stream=$false; max_tokens=32; temperature=0
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "$Base/v1/chat/completions" -Method Post -Headers $Headers `
  -ContentType application/json -Body $Body | ConvertTo-Json -Depth 10
```

### 5.8 SSE 流式请求

```powershell
curl.exe -N "$Base/v1/chat/completions" `
  -H "Authorization: Bearer $Token" `
  -H 'Content-Type: application/json' `
  --data-binary '{"model":"qwen2.5-0.5b-instruct","messages":[{"role":"user","content":"从1数到3"}],"stream":true}'
```

成功流以多条 `data: {...}` 事件返回，并以 `data: [DONE]` 结束。

### 5.9 删除 Deployment

```powershell
Invoke-RestMethod "$Base/api/deployments/qwen-small-spark1" `
  -Method Delete -Headers $Headers
```

系统先将 Replica 摘流，再关闭 SSH tunnel、删除 runtime，最后把 Deployment 标记为
`DELETED`。模型 Artifact 不会随 Deployment 删除。

## 6. 状态和可调度含义

### Node

- `ONLINE`：Agent inventory 可达。
- `OFFLINE`：Agent Key 缺失、网络失败或 Agent 异常；其 Replica 不接流量。
- `UNKNOWN`：刚登记、尚未 probe。

### Replica

- `WAITING_FOR_ARTIFACT`：没有兼容的 READY Artifact、workspace 或空闲 GPU。
- `PROVISIONING`：正在调用 Agent 创建 runtime。
- `STARTING`：容器存在但模型尚未通过 readiness。
- `UNHEALTHY`：容器可能运行，但 tunnel、health 或模型 identity 未通过。
- `READY`：完整 readiness 通过，可进入路由。
- `DRAINING`：Deployment 正在删除，已停止接收新请求。
- `FAILED`：容器退出、丢失或创建失败；Reconciler 会为 ACTIVE Deployment replacement。
- `RELEASED`：Deployment 删除后 runtime 已释放。

### Job/Artifact location

- Job：`QUEUED`、`RUNNING`、`VALIDATING`、`COMPLETED`、`FAILED`、
  `CANCEL_REQUESTED`、`CANCELLED`。
- Artifact location：`READY` 才能调度；也可能为 `MISSING`、`CORRUPT`、
  `UNAVAILABLE`、`REVOKED`。

## 7. 故障恢复和归档

容器进入 exited/dead/missing 等终态后，旧 Replica 立即变为 FAILED 并退出路由；
ACTIVE Deployment 会创建更高 `instance_epoch` 的 replacement，复用同一只读 Artifact。
故障窗口没有 READY Replica 时 `/v1/chat/completions` 返回 HTTP 503，而不会向失效容器
继续发流量。

立即整理终态历史：

```powershell
Invoke-RestMethod "$Base/api/maintenance/archive-stale?terminal_replica_hours=0&deleted_deployment_hours=0&completed_job_hours=0" `
  -Method Post -Headers $Headers
```

查看历史：

```powershell
Invoke-RestMethod "$Base/api/catalog?include_archived=true" -Headers $Headers |
  ConvertTo-Json -Depth 12
```

归档是软删除。重新注册相同 Node 或以相同 immutable repository/revision upsert Model 会
恢复它；在线节点或仍有活跃 Deployment 的模型不能归档。

## 8. 常见错误

| HTTP/现象 | 含义和处理 |
|---|---|
| 401 `invalid API key` | Bearer/X-API-Key 错误；本机 Dashboard 刷新 `/` 重新取得 Cookie |
| 404 | ID 不存在，或资源已归档并退出普通列表/选择 |
| 409 | ID/name 冲突、资源正在使用、在线节点不能归档，或 immutable Model 冲突 |
| 422 | 字段格式错误、非 commit revision、存储池权限/状态不符合要求、runtime 参数不在白名单 |
| 502 | 控制面访问 Agent 或 runtime 失败；查看 `failure_reason` 和节点服务日志 |
| 503 no READY replica | 模型未部署、正在 replacement、节点离线或所有 Replica 满并发 |
| `WAITING_FOR_ARTIFACT` | 检查 location READY、节点一致性、模型池 runtime_access 和 workspace rw |
| vLLM free memory 不足 | 调低 `gpu_memory_utilization` 或释放节点上的其他进程 |

不要把“端口打开”“容器 running”当作服务 READY。完整接受条件是节点 ONLINE、容器
running、endpoint health、`/v1/models` identity、Replica READY 和一次真实推理全部通过。

## 9. Node Agent 内部接口

以下接口位于节点 `:9010`，使用每节点独立 Bearer Key，仅供控制面调用，不应直接开放
给普通用户：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/healthz` | Agent 存活 |
| GET | `/v1/inventory` | 主机、Docker、GPU、镜像 inventory |
| PUT/GET/DELETE | `/v1/storage-pools...` | 节点侧授权池 |
| POST/GET/DELETE | `/v1/scans...` | 受限模型扫描 |
| POST | `/v1/artifacts/verify` | 文件结构/checksum 校验 |
| POST/GET | `/v1/downloads...` | Hugging Face 下载 |
| POST/GET/DELETE | `/v1/replicas...` | runtime 生命周期 |

Linux Agent 只支持 vLLM Docker runtime。Mac Agent 提供 inventory、Replica 生命周期和
Replica logs，使用 MLX 进程模型。生产网络应只允许控制面 IP 访问 Agent 9010 和
Replica 的动态 SSH 端口；跨不可信网络应使用 VPN 或额外的 TLS/mTLS 边界。

## 10. 当前真实环境示例

当前已验证环境包含：

- Node：`spark-01`，DGX Spark GB10，Agent 1.1.0。
- Model：`qwen2.5-0.5b-instruct`。
- Artifact：`artifact-0da54de0495eae49b2cebb7b`。
- Runtime Profile：`vllm-spark-gb10`。
- Deployment：`qwen2.5-0.5b-spark1-v8`。
- 一个 READY replacement Replica；旧 FAILED/RELEASED 记录已软归档。

这些 ID 可作为只读观察示例；新部署应使用新的唯一 Deployment `id` 和 `name`。
