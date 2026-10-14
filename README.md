# Standalone Model Serving

这是一个可独立运行的多节点模型服务平台。控制面、OpenAI Gateway、SQLite
数据库都运行在本机；远端设备只运行本项目自己的轻量 Node Agent。项目不需要
Snowpack、Kubernetes 或其他外部调度控制面。

## 架构

```text
                         本机
  API Client -> OpenAI Gateway -> Replica Registry / SQLite
                            |                |
                            +-> Reconciler --+
                                      |
                              Node Agent HTTP API
                                      |
                 Accelerator allocation + runtime lifecycle
                                      |
                    vLLM container or MLX process
                                      ^
                    local authenticated SSH tunnel
```

平台实现了：

- Node、Model、Deployment、Replica 数据库；
- Node Agent 硬件/GPU UUID 盘点和 Docker 生命周期；
- 单 Deployment 创建、删除和故障 replacement；
- MVP 强制 `desired_replicas=1`，表结构和收敛循环保留多副本能力；
- vLLM + SSH runtime，vLLM 只监听容器 `127.0.0.1:8000`；
- Apple Silicon MLX-LM runtime，支持不可变本地模型快照；
- 本机自动生成专用 SSH key，并为每个 Replica 建立 tunnel；
- 节点在线、容器运行、HTTP health、served-model identity 四层检查；
- 标准 `/v1/models` 和 `/v1/chat/completions`，支持 JSON 与 SSE；
- API Key、并发占用计数和路由记录；
- 容器退出、连接失败、5xx 或 stream 中断后立即停止接收新请求。

DGX Spark 从环境检查、Agent 安装、runtime 构建、算力注册到 30B 模型部署的
完整操作见 [docs/dgx_spark_manual.md](docs/dgx_spark_manual.md)。
节点完成引导后，完全通过本机 API 授权存储、扫描/下载模型、登记 Artifact 和部署的
第一阶段流程见 [docs/phase1_managed_workflow.md](docs/phase1_managed_workflow.md)。
当前全部公开接口、请求字段、状态机和端到端操作见
[完整使用手册](docs/complete_user_manual.md)。
第三方 Linux/NVIDIA 算力提供者的一键接入流程见
[算力提供者一键注册指南](docs/provider_registration.md)。

## 本机控制面

```powershell
.\scripts\start_control.ps1
```

启动脚本首次运行会生成 ACL 受限的 `.state/control.token`，并在 `.state/` 生成
Gateway 专用 Ed25519 key。API Key、Node Agent Key 和私钥都不写入业务数据库。

控制面只监听 `127.0.0.1`。从本机打开 `http://127.0.0.1:8080/` 时，Dashboard
会建立 `HttpOnly`、`SameSite=Strict` 的会话 Cookie，因此不需要在页面中复制 API
Key；命令行、SDK 和非本机客户端仍须使用 Bearer API Key。未建立本机会话且未提供
API Key 的请求仍返回 HTTP 401。

Dashboard 默认只展示操作中的资源，并隐藏 `DELETED` Deployment、终态 Replica
和已完成任务。后台按保留期自动把终态记录标记为归档，不物理删除审计数据；页面的
“归档失效记录”可立即整理历史，“显示归档历史”可随时查看。对应管理接口为
`POST /api/maintenance/archive-stale`。离线节点和逻辑模型必须使用各自的归档接口，
且仍在线、仍有活跃 Deployment/Replica 的资源会被拒绝归档。

## 设备 Node Agent

设备要求 Linux、Docker、NVIDIA Container Toolkit 和 Python 3.11+。在设备上：

```bash
export NODE_AGENT_API_KEY='<per-node-random-key>'
python -m uvicorn node_agent:app --host 0.0.0.0 --port 9010
```

长期运行可使用 [deploy/model-serving-agent.service](deploy/model-serving-agent.service)，
并在 `/etc/model-serving-agent.env` 中设置 `NODE_AGENT_API_KEY`。只应允许本机
Gateway 所在地址访问 TCP 9010 和 Agent 创建的 Replica SSH 端口。

Apple Silicon Mac 使用 `mac_node_agent.py` 和 MLX-LM。Agent 首次收到受保护
请求时会生成权限为 `0600` 的 `~/.model-serving-agent/agent.token`；可使用
[deploy/com.model-serving.mac-agent.plist](deploy/com.model-serving.mac-agent.plist)
作为用户级 LaunchAgent。模型应先按不可变 commit 下载到本地目录，Deployment
指定 `"runtime":"mlx"`，并让 `served_model_id` 等于该目录的绝对路径。

本机只保存 Agent Key 对应的环境变量名：

```powershell
$env:DGX01_AGENT_KEY = '<same-per-node-key>'
$headers = @{ Authorization = 'Bearer <model-serving-key>' }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/nodes `
  -Headers $headers -ContentType application/json -Body @'
{"id":"dgx-01","name":"DGX Spark 01","host":"172.18.166.16","agent_url":"http://172.18.166.16:9010","agent_key_env":"DGX01_AGENT_KEY","platform":"DGX Spark","network_zone":"lab"}
'@
```

## 创建模型和 Deployment

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/models `
  -Headers $headers -ContentType application/json -Body @'
{"id":"qwen-small","display_name":"Qwen Small","repository":"Qwen/example","revision":"<immutable-commit>","task":"chat","capabilities":["chat","streaming"]}
'@

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/deployments `
  -Headers $headers -ContentType application/json -Body @'
{"id":"qwen-small-prod","name":"Qwen Small","model_id":"qwen-small","served_model_id":"qwen-small","desired_replicas":1,"runtime_image":"registry/llm-vllm-ssh@sha256:<digest>"}
'@
```

## OpenAI-compatible API

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/chat/completions `
  -Headers $headers -ContentType application/json -Body @'
{"model":"qwen-small","messages":[{"role":"user","content":"hello"}],"stream":false}
'@
```

`GET /api/catalog` 返回完整节点、GPU、Deployment 和 Replica 状态；
`GET /v1/models` 只返回当前可以路由的模型。

## 本地无 GPU 模拟

依次启动三个进程：

```powershell
.\.venv\Scripts\python.exe -m uvicorn mock_backend:app --host 127.0.0.1 --port 8000
$env:MOCK_LLM_URL='http://127.0.0.1:8000'; .\.venv\Scripts\python.exe -m uvicorn mock_node_agent:app --host 127.0.0.1 --port 9010
$env:MODEL_SERVING_API_KEY='local-key'; .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8080
```

然后在新终端执行：

```powershell
$env:MODEL_SERVING_API_KEY='local-key'
$env:DEMO_NODE_AGENT_KEY='mock-key'
.\.venv\Scripts\python.exe seed_demo.py
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

自动测试覆盖节点盘点、GPU UUID 分配、Deployment 创建、READY、非流式、SSE、
容器失败摘流、replacement 和删除。
