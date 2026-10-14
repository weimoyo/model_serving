# Model Serving Roadmap / TODO

状态说明：`[ ]` 未开始，`[-]` 进行中，`[x]` 已完成。

## 已确认的架构原则

- [x] 控制面、数据库、用户 API 和管理 UI 运行在用户本机。
- [x] 算力节点只保留一次性安装的 Node Agent、容器运行时、驱动和受控数据目录。
- [x] 节点注册、资源授权、模型发现、下载、部署、删除和故障恢复均通过本项目 API 发起。
- [x] 推理框架优先封装在不可变容器镜像中，不直接依赖宿主机 Python 环境。
- [x] 模型由 Agent 管理，推理容器以只读方式挂载；workspace 和 cache 使用独立、受限的读写目录。
- [x] 不提供任意宿主机文件浏览或远程 shell API；Agent 只能操作显式授权的资源。

## 当前基线

- [x] 本机 Gateway、SQLite、Model/Deployment/Replica 数据结构和状态收敛。
- [x] Mac Studio MLX 节点真实部署与故障 replacement Gate。
- [x] DGX Spark 节点注册、vLLM runtime 能力和 GB10 UUID 盘点。
- [x] 调度器按 `mlx` / `vllm` runtime 匹配节点。
- [x] Spark 统一内存的 `nvidia-smi N/A` 兼容。
- [x] vLLM Replica 使用共享 `model-serving-hf-cache` Docker volume。
- [x] Linux/NVIDIA 提供者可用一次性 Enrollment Token 执行一键注册脚本，自动完成 Agent、CDI、资源池、Runtime Profile 和初始模型扫描。

## 第一阶段：授权存储、已有模型发现与只读部署闭环

目标：不登录算力节点手工操作，仅通过本机 API 发现节点已有模型并部署一个小模型。

### 1. 存储池与权限模型

- [x] 新增 `storage_pools` 数据表：节点、宿主机路径、容器路径、用途、配额和状态。
- [x] 区分 `agent_access`（`ro` / `rw`）与 `runtime_access`（`none` / `ro` / `rw`）。
- [x] 支持用途：`model`、`workspace`、`cache`、`output`。
- [x] 节点注册或独立 API 可创建、查看、更新和撤销存储池授权。
- [x] Agent 对授权根路径执行 `realpath` 校验，禁止 `..`、符号链接逃逸和跨根目录访问。
- [x] 对不存在、不可读、不可写、空间不足和超配额的存储池返回明确错误。
- [x] 管理 UI 展示存储池、权限、容量和剩余空间。

验收条件：控制端可以注册一个 Spark 模型目录和一个 workspace；Agent 无法访问授权根之外的路径。

### 2. 已有模型扫描

- [x] 新增 Agent 受限扫描接口，只扫描指定 `storage_pool_id`。
- [x] 识别 Hugging Face Transformers/Safetensors 模型结构。
- [x] 记录 `config.json`、tokenizer、权重分片、格式、架构、大小和可能的 revision。
- [x] 识别但明确标注 GGUF、Ollama、NIM/Triton 等当前 runtime 不可直接使用的格式。
- [x] 扫描任务异步执行，支持进度、取消、失败原因和重复扫描去重。
- [x] 控制端新增模型发现 API 和 UI，管理视图只使用 Artifact 相对路径。

验收条件：从本机发起扫描后可以看到 Spark 已有模型及兼容性，整个过程无需 SSH 登录 Spark。

### 3. Artifact 数据模型

- [x] 新增 `model_artifacts`：仓库、不可变 revision、格式、大小、checksum 和兼容元数据。
- [x] 新增 `artifact_locations`：Artifact 所在节点、存储池、相对路径、状态和最后校验时间。
- [x] 保持 `Model` 为用户可见逻辑模型，Artifact 为具体不可变文件版本。
- [x] 支持将扫描结果登记为 Artifact，并防止同 ID 指向不同内容。
- [x] 支持重新校验文件完整性并将丢失/损坏位置标记为不可调度。

验收条件：同一逻辑 Model 可以关联精确 Artifact，Artifact 可以记录一个或多个节点位置。

### 4. Runtime Profile

- [x] 新增 `runtime_profiles`：runtime、镜像 digest、CPU 架构、加速器、启动契约和版本。
- [x] 维护每个 profile 允许使用的 runtime 参数白名单。
- [x] 禁止通过 `runtime_args` 传入任意危险参数或改变挂载边界。
- [x] vLLM profile 固定 OpenAI API、health、served-model identity 和 SSH tunnel 契约。
- [x] 宿主机 MLX profile 与 vLLM 容器 profile 分离，并支持默认禁用。

验收条件：Deployment 只能引用节点兼容且已启用的 Runtime Profile。

### 5. 只读模型挂载与调度

- [x] Deployment 改为引用 `artifact_id`、`runtime_profile_id` 和 workspace storage pool。
- [x] 调度器同时检查节点在线、runtime、GPU、Artifact location、权限和容量。
- [x] Agent 仅把 Artifact 子目录只读挂载到容器固定路径（如 `/models/current`）。
- [x] 为 Replica 创建独立、按存储池配额准入的读写 workspace。
- [x] 不向推理容器暴露 Docker socket、Agent 密钥或未授权宿主机目录。
- [x] Artifact 未就绪时进入 `WAITING_FOR_ARTIFACT`，不得创建不可用 Replica。
- [x] 使用 Spark 上的小模型完成非流式、SSE 和模型 identity Gate。
- [x] 主动终止容器，验证旧 Replica 摘流、新 Replica 复用同一 Artifact 快速恢复。

验收条件：从本机 UI/API 选择 Spark 已有小模型并完成真实推理；replacement 不复制或重新下载模型。

### 6. 第一阶段下载入口（由原第二阶段前移）

- [x] 本机 API 可在指定可写模型池发起 Hugging Face 下载任务。
- [x] 强制不可变 revision，凭据仅由节点环境注入。
- [x] 使用 `.staging`、结构/配额校验和原子发布。
- [x] 下载任务具有状态、进度、错误和活动任务去重，并在完成后自动登记 Artifact。
- [x] 在 Spark 1 下载小模型并通过真实部署 Gate。

## 第二阶段：下载、上传、配额与生命周期

目标：所有模型进入节点的过程均由控制端管理、可追踪、可恢复。

### 1. Artifact Job

- [ ] 新增 `transfer_jobs`：类型、来源、目标、进度、状态、错误、创建者和时间戳。
- [ ] API 发起 Hugging Face 模型下载，必须使用不可变 revision。
- [ ] 下载到 staging 目录，完成 checksum/结构校验后原子发布到模型仓库。
- [ ] 支持暂停、取消、失败重试、幂等请求和断点续传。
- [ ] 同一 Artifact/节点只允许一个有效下载任务，避免重复占用空间。
- [ ] 下载凭据使用节点秘密存储，不进入数据库、日志或 runtime 参数。

### 2. 本机到节点的上传

- [ ] 支持从控制端分块上传模型或其他 Artifact。
- [ ] 支持分块 checksum、总 checksum、续传和超时清理。
- [ ] 服务端限制单文件、总任务大小和允许的文件类型。
- [ ] 上传完成后走与远程下载相同的校验和原子发布流程。

### 3. 配额和清理

- [ ] 存储池预留空间检查和 soft/hard quota。
- [ ] workspace、cache、model、output 分别统计占用。
- [ ] 实现 LRU/保留策略，但禁止自动删除正在使用或被 Deployment 引用的 Artifact。
- [ ] 提供 dry-run 清理预览和显式确认接口。
- [ ] 所有删除进入审计记录；重要 Artifact 支持保护标记。

验收条件：从本机发起小模型下载或上传，可观察完整进度；失败后可恢复，且不会产生可见的半成品 Artifact。

## 第三阶段：多用户、权限和多节点数据面

目标：从单管理员实验平台演进为可审计的共享算力服务。

### 1. 身份与 RBAC

- [ ] 用多 API Key/身份替代单全局 Key。
- [ ] 角色至少包含：管理员、节点管理员、模型管理员、部署用户、只读用户。
- [ ] 为 Node、Storage Pool、Artifact、Runtime Profile 和 Deployment 建立资源级授权。
- [ ] 每用户限制最大 Deployment、并发请求、GPU 配额、存储配额和任务数。
- [ ] API Key 支持创建、轮换、吊销、过期和最后使用时间。

### 2. 审计和用量

- [ ] 新增不可变审计事件：注册、授权、扫描、下载、上传、部署、删除和密钥操作。
- [ ] 统计请求数、token、时延、GPU 使用时间、存储和网络传输量。
- [ ] 提供按用户、模型、Deployment、节点聚合的查询接口。
- [ ] 为未来计费保留 usage ledger，但不在此阶段实现真实扣费。

### 3. 多节点 Artifact 复制

- [ ] Artifact location 感知的调度和复制计划。
- [ ] 节点间或控制端中转的分块复制、校验和断点续传。
- [ ] 副本数量、目标节点和网络带宽限制策略。
- [ ] 节点离线、文件损坏后重新复制并恢复位置状态。
- [ ] 区分“一个 Deployment 多 Replica”和“一个分布式 runtime 跨多节点”，分别建模。

验收条件：不同用户只能看到和使用获授权资源；Artifact 可在节点间受控复制；所有操作和用量均可追溯。

## 横向安全与质量要求

- [ ] 所有路径、挂载、镜像和 runtime 参数使用结构化字段，不拼接 shell 命令。
- [ ] Agent API 保持每节点独立密钥，并规划 mTLS/节点证书。
- [ ] 敏感信息不进入数据库明文、URL、错误响应或日志。
- [ ] 对新数据库表和状态机提供迁移、单元测试和故障恢复测试。
- [ ] 对 Agent 接口提供鉴权、路径逃逸、symlink、配额绕过和重复请求测试。
- [ ] 每阶段都在真实 Spark 上执行成功、失败、重启和 replacement Gate。
- [x] 文档和 Dashboard 与实际 API 同步更新。
- [x] 终态 Deployment、Replica 和任务自动软归档；Dashboard 默认隐藏并可查看历史。
- [x] Node/Model 安全归档接口拒绝在线或仍被活跃部署引用的资源。

## 下一项执行任务

- [x] 将当前 Agent 更新到 Spark 1，注册模型池/workspace，从本机发起小模型下载并完成真实推理、SSE 和 replacement Gate。
