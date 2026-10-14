# Laboratory feasibility inventory

Status: login and basic capability check
Checked: 2026-10-13 (Asia/Shanghai)

This file intentionally records addresses, account names, and validation
results, but not password values. Password authentication was used during the
check and succeeded for all four explicitly addressed hosts. Secrets must not
be committed to the repository or copied into logs.

| Role | Address | Account | Reported hostname | Architecture | Accelerator | GPU UUID | Docker daemon | Snowpack Agent | SSH/password | Initial use |
|---|---|---|---|---|---|---|---|---|---|---|
| DGX Spark 01 | `172.18.166.16` | `guolab-dgx-01` | `spark-e9b5` | `aarch64` | NVIDIA GB10 | `GPU-1f67d5f3-4186-48e1-26eb-030ca9b2b4b1` | `29.2.1`, accessible | restart loop | verified | blocked until control-plane TLS is repaired |
| DGX Spark 02 | `172.18.166.17` | `guolab-dgx-02` | `spark-5a86` | `aarch64` | NVIDIA GB10 | `GPU-5fe4c060-eacd-2d21-a6e8-a3f23a12249f` | `29.2.1`, accessible | active | verified | primary CUDA/vLLM candidate |
| DGX Spark 03 | `172.18.166.25` | `guolab-dgx-03` | `spark-3cb6` | `aarch64` | NVIDIA GB10 | `GPU-4e04e4f8-936b-ad63-2d38-7d4412d2a4c9` | `29.2.1`, accessible | active | verified | primary CUDA/vLLM candidate |
| Mac Studio | `192.168.3.241` | `lix` | `Studio506` | `arm64` | Apple/Metal, not enumerated by this check | n/a | not found | not checked | verified | later MLX/Metal backend track |
| 4 x RTX PRO 6000 container host | unresolved `remoteidiot` SSH alias | unresolved | not checked | expected `amd64` | expected 4 x RTX PRO 6000 | not checked | not checked | not checked | name not resolved | optional after Spark path works |

## Evidence boundary

- `SSH TCP reachable` means a TCP connection to port 22 succeeded.
- `Password login verified` means the supplied account/password completed an
  SSH login and ran read-only identification commands.
- Docker daemon access was verified on all three Spark nodes. All reported
  Docker `29.2.1` on `aarch64`.
- NVIDIA Container Toolkit CLI `1.19.1` is installed on all three Spark nodes.
  This does not yet prove a real GPU-bound container launch or image
  compatibility.
- GPU identity was read from `nvidia-smi` on each Spark. The three UUIDs are
  distinct.
- Existing Snowpack Agent state was inspected but not changed. Spark 02 and 03
  reported `active`. Spark 01 is in an auto-restart loop because its connection
  to `https://computility.net.cn:24443` fails certificate verification with
  `certificate has expired`; its restart counter was already above 14,000.
- No model was downloaded and no container or model server was started.

## Remaining live checks

Before admitting a Spark into the model-serving pool, still verify:

1. A real GPU-bound container using an ARM64-compatible CUDA probe image.
2. Available memory and disk capacity.
3. Snowpack control-plane reachability and a current accepted heartbeat.
4. The GPU UUID reported through Snowpack matches the host observation.
5. A real GPU-bound Snowpack Instance and its lease evidence.

For `remoteidiot`, obtain either the concrete SSH hostname/IP and account, or
make the intended SSH config available to this execution environment. Here,
`ssh -G remoteidiot` did not resolve it to a usable host and direct connection
failed at name resolution.
