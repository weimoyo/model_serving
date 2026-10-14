# Phase 1 verification — 2026-10-14

## Automated gate

- `uv run python -m pytest -q --basetemp <project-state-dir>`: **9 passed**.
- Tests cover the control-plane lifecycle, non-streaming and SSE proxying,
  failure replacement, storage boundaries, managed read-only model mounts,
  per-Replica workspaces, Artifact projection and Runtime Profile validation.
- The Linux Agent test verifies structured Docker argv and confirms that no
  Docker socket or arbitrary shell command is exposed to inference containers.

## Real Spark environment

- Node: `spark-01` / NVIDIA DGX Spark GB10 / `aarch64`.
- Agent: 1.1.0, Docker 29.2.1, node state `ONLINE`.
- Model pool: `/srv/model-serving/models`, quota 200 GiB.
- Workspace pool: `/srv/model-serving/workspaces`, quota 50 GiB.
- Downloaded model: `Qwen/Qwen2.5-0.5B-Instruct` at immutable revision
  `7ae557604adf67be50417f59c2c2f167def9a775`.
- Artifact: `artifact-0da54de0495eae49b2cebb7b`, size 999,606,601 bytes,
  status `READY` on `spark-01`.
- Runtime image digest:
  `sha256:a551215b6e9369738303ccb67e343c7edd5da816c03b1fbe6fab5babf2162898`.
- Runtime Profile: `vllm-spark-gb10`.

## Real inference gate

- Deployment: `qwen2.5-0.5b-spark1-v8`, `desired_replicas=1`.
- Runtime args include `max_model_len=4096`, `max_num_seqs=1`, eager mode and
  `gpu_memory_utilization=0.25`. The lower memory ratio is required because the
  shared GB10 had about 37.3 GiB free during acceptance.
- The first Replica converged to `READY`, with container, SSH tunnel, health and
  model identity checks all passing.
- `GET /v1/models` published only `qwen2.5-0.5b-instruct`.
- A real non-streaming `POST /v1/chat/completions` returned exactly
  `SPARK_MODEL_OK` and reported token usage.
- A real streaming request returned `text/event-stream`, 15 data events and the
  terminal `data: [DONE]` event.

## Failure and replacement gate

- The active first-epoch container was deliberately killed.
- Epoch 1 converged to `FAILED`, `container_state=exited`, `exit_code=137`.
- During replacement, the gateway returned HTTP 503 with
  `no READY replica with capacity`; no request was routed to the failed Replica.
- Epoch 2 was automatically created with the same Artifact and converged to
  `READY`; exactly one READY Replica remained.
- A real non-streaming request succeeded again through the replacement Replica.
- The control plane was then restarted without manually injecting
  `MODEL_SERVING_SSH_KEY`; the persisted key pointer was loaded, epoch 2 remained
  `READY`, and a final real request returned exactly `FINAL_OK`.

## Live fixes proven by this gate

- Managed per-Replica workspace directories are writable across runtime UID/GID
  differences while the storage-pool root remains controlled.
- Read-only `authorized_keys` works with Docker user-namespace ownership mapping;
  password authentication remains disabled.
- The local gateway identity is selected through `.state/gateway_key.path`, so a
  control-plane restart no longer depends on a transient PowerShell environment.
- Reconciliation is serialized, preventing concurrent duplicate Replica creation.
- Terminal container exit code and log tail are persisted into `failure_reason`.
- The Dashboard no longer contains a hard-coded demo key. Loopback access receives
  an HttpOnly, SameSite=Strict session Cookie, while unauthenticated API requests
  without that local session continue to return HTTP 401.
- After rotating the control-plane key and restarting, the Dashboard loaded the
  catalog without manual key entry and a real request returned `UI_SESSION_OK`.
- Terminal records use soft archival instead of physical deletion. The live
  database migration archived 7 deleted Deployments, 48 failed/released Replicas
  and 4 completed jobs; the single Spark READY Replica remained visible and
  schedulable, while all 50 Replica history rows remained queryable with
  `include_archived=true`.

## Acceptance status

**PASSED.** Phase 1 has completed its automated and real-device gates: node
verification, managed download and Artifact registration, real vLLM deployment,
model catalog publication, non-streaming inference, SSE inference, failed-Replica
traffic removal and automatic same-Artifact replacement.
