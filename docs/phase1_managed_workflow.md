# 第一阶段：受管模型全流程

完成一次 Node Agent 安装和节点注册后，下面所有操作都在运行控制面的 Windows
本机执行，不再登录 Spark。控制面只向 Agent 发送存储池 ID 与相对路径，推理容器
无法看到未授权宿主机目录。

## 0. 一次性更新 Spark Agent

把本项目当前的 `node_agent.py` 更新到 Spark。可直接在 Windows 项目根目录运行；SSH
密码和 sudo 密码只进入交互式终端，不写入项目：

```powershell
.\scripts\update_spark_agent.ps1 -Spark 'guolab-dgx-01@172.18.166.16'
```

如果 Spark 访问 Hugging Face 官方站持续出现 connection reset，可由节点管理员切换到
固定白名单内的公开镜像；活动任务的 commit SHA 仍由控制面锁定：

```powershell
.\scripts\update_spark_agent.ps1 -Spark 'guolab-dgx-01@172.18.166.16' `
  -HfEndpoint 'https://hf-mirror.com'
```

等价的 Spark 端安装与检查命令是：

```bash
/opt/model-serving/.venv/bin/pip install 'fastapi>=0.110,<1' 'uvicorn>=0.29,<1' 'huggingface-hub>=0.27,<2'
sudo systemctl restart model-serving-agent
curl http://127.0.0.1:9010/healthz
```

这一步属于节点引导。后续存储授权、扫描、下载、部署和验证全部走本机 API。

## 1. 本机变量与节点验证

```powershell
$Base = 'http://127.0.0.1:8080'
$Headers = @{ Authorization = "Bearer $env:MODEL_SERVING_API_KEY" }
$NodeId = 'spark-1'

Invoke-RestMethod "$Base/api/nodes/$NodeId/probe" -Method Post -Headers $Headers
Invoke-RestMethod "$Base/api/nodes" -Headers $Headers | ConvertTo-Json -Depth 8
```

只有 `state=ONLINE` 且 inventory 中存在预期 GPU UUID，才继续。

## 2. 授权模型池和 workspace

更新脚本会一次性创建 `/srv/model-serving/models` 和 `/srv/model-serving/workspaces`，
并把它们加入 systemd 的 `ReadWritePaths`。模型池允许 Agent 下载，但容器只能只读；
workspace 对 Agent 和容器均可写。其他已有模型目录可以注册为 `agent_access=ro`、
`runtime_access=ro`；若要让 Agent 写入其他宿主机根目录，管理员还必须显式修改 systemd
沙箱，单靠 API 不能越过该边界。

```powershell
$modelPool = @{
  id='spark1-models'; host_path='/srv/model-serving/models'; container_path='/models'
  purpose='model'; agent_access='rw'; runtime_access='ro'
  quota_gib=200; min_free_gib=20
} | ConvertTo-Json

$workspacePool = @{
  id='spark1-workspace'; host_path='/srv/model-serving/workspaces'; container_path='/workspace'
  purpose='workspace'; agent_access='rw'; runtime_access='rw'
  quota_gib=100; min_free_gib=20
} | ConvertTo-Json

Invoke-RestMethod "$Base/api/nodes/$NodeId/storage-pools/spark1-models" `
  -Method Put -Headers $Headers -ContentType application/json -Body $modelPool
Invoke-RestMethod "$Base/api/nodes/$NodeId/storage-pools/spark1-workspace" `
  -Method Put -Headers $Headers -ContentType application/json -Body $workspacePool
Invoke-RestMethod "$Base/api/storage-pools?node_id=$NodeId" -Headers $Headers
```

Agent 会拒绝相对路径中的 `..`、绝对路径、符号链接逃逸、权限不符、剩余空间不足和
已超配额的目录。

## 3A. 扫描节点已有模型

```powershell
$scan = Invoke-RestMethod "$Base/api/nodes/$NodeId/scans" -Method Post `
  -Headers $Headers -ContentType application/json `
  -Body '{"storage_pool_id":"spark1-models","relative_path":"."}'

do {
  Start-Sleep 2
  $scan = Invoke-RestMethod "$Base/api/scans/$($scan.id)" -Headers $Headers
  "$($scan.state) $($scan.progress | ConvertTo-Json -Compress)"
} while ($scan.state -in @('QUEUED','RUNNING','CANCEL_REQUESTED'))

$scan.result | Format-List
```

完成后，每个发现项自动形成 `model_artifact` 和该 Spark 上的 READY location。GGUF、
Ollama、NIM/Triton 目录会被识别为当前 vLLM profile 不兼容，而不会直接部署。

## 3B. 从 Hugging Face 下载小模型

下载必须指定不可变 revision；生产使用完整 commit SHA。`HF_TOKEN` 如有需要只配置在
Spark Agent 服务环境，不进入控制面数据库或请求体。

```powershell
$download = Invoke-RestMethod "$Base/api/nodes/$NodeId/downloads" -Method Post `
  -Headers $Headers -ContentType application/json -Body @'
{
  "storage_pool_id":"spark1-models",
  "repository":"Qwen/Qwen2.5-0.5B-Instruct",
  "revision":"<full-commit-sha>"
}
'@

do {
  Start-Sleep 3
  $download = Invoke-RestMethod "$Base/api/downloads/$($download.id)" -Headers $Headers
  "$($download.state) $($download.progress | ConvertTo-Json -Compress)"
} while ($download.state -in @('QUEUED','RUNNING','VALIDATING'))

$download | ConvertTo-Json -Depth 8
```

Agent 下载到池内 `.staging`，校验模型结构和配额后再原子发布。相同节点、仓库和 revision
的活动任务会去重。

## 4. 创建逻辑 Model、关联 Artifact 和 Runtime Profile

从扫描结果的 `artifact_id` 或下载任务的 `artifact_id` 选择目标：

```powershell
$ArtifactId = $download.artifact_id

Invoke-RestMethod "$Base/api/models" -Method Post -Headers $Headers `
  -ContentType application/json -Body @'
{"id":"qwen-0.5b","display_name":"Qwen 2.5 0.5B Instruct","repository":"Qwen/Qwen2.5-0.5B-Instruct","revision":"<full-commit-sha>","capabilities":["chat","streaming"]}
'@

Invoke-RestMethod "$Base/api/artifacts/$ArtifactId/models/qwen-0.5b" `
  -Method Post -Headers $Headers

Invoke-RestMethod "$Base/api/runtime-profiles/vllm-spark" -Method Put `
  -Headers $Headers -ContentType application/json -Body @'
{
  "id":"vllm-spark",
  "name":"vLLM on DGX Spark",
  "runtime":"vllm",
  "image":"<registry>/llm-vllm-ssh@sha256:<64-hex-digest>",
  "architectures":["aarch64"],
  "accelerator_kinds":["GPU"],
  "allowed_args":["max_model_len","gpu_memory_utilization","dtype"],
  "enabled":true
}
'@
```

Runtime Profile 只允许白名单参数。镜像、挂载、网络、SSH key 和 Docker 权限都不能由
`runtime_args` 改写。

## 5. 部署与状态收敛

```powershell
Invoke-RestMethod "$Base/api/deployments" -Method Post -Headers $Headers `
  -ContentType application/json -Body (@{
    id='qwen-0.5b-spark1'; name='Qwen 0.5B Spark 1'; model_id='qwen-0.5b'
    served_model_id='qwen-0.5b'; desired_replicas=1
    artifact_id=$ArtifactId; runtime_profile_id='vllm-spark'
    workspace_storage_pool_id='spark1-workspace'
    runtime_args=@{ max_model_len=4096; gpu_memory_utilization=0.8 }
  } | ConvertTo-Json -Depth 5)

do {
  Start-Sleep 5
  Invoke-RestMethod "$Base/api/reconcile" -Method Post -Headers $Headers | Out-Null
  $deployment = Invoke-RestMethod "$Base/api/deployments/qwen-0.5b-spark1" -Headers $Headers
  $deployment.replicas | Format-Table id,state,container_state,endpoint_healthy,model_runtime_ready,failure_reason
} while ($deployment.replicas[0].state -notin @('READY','FAILED'))
```

调度器必须同时满足节点在线、架构/runtime 匹配、GPU 可用、Artifact location READY、
模型池可读和 workspace 可写。Artifact 暂未就绪时 Replica 保持
`WAITING_FOR_ARTIFACT`，不会创建一个注定失败的容器。

## 6. OpenAI API 验收

```powershell
Invoke-RestMethod "$Base/v1/models" -Headers $Headers

Invoke-RestMethod "$Base/v1/chat/completions" -Method Post -Headers $Headers `
  -ContentType application/json -Body @'
{"model":"qwen-0.5b","messages":[{"role":"user","content":"只回复 SPARK_MODEL_OK"}],"stream":false}
'@

curl.exe -N "$Base/v1/chat/completions" `
  -H "Authorization: Bearer $env:MODEL_SERVING_API_KEY" `
  -H 'Content-Type: application/json' `
  --data-binary '{"model":"qwen-0.5b","messages":[{"role":"user","content":"从1数到3"}],"stream":true}'
```

最终接受条件是：`/v1/models` 发布模型、非流式请求成功、SSE 以 `[DONE]` 结束；随后
终止容器，旧 Replica 立即摘流，并由新 Replica 复用相同只读 Artifact 恢复 READY。
