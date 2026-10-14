#!/usr/bin/env bash
set -Eeuo pipefail

# Interactive bootstrap for a Linux/NVIDIA compute provider.  Run from a clone
# of this repository.  The provider only receives a short-lived enrollment
# token, never the platform administrator API key.

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONTROL_URL="${MODEL_SERVING_CONTROL_URL:-}"
TOKEN_FILE=""
NODE_ID=""
NODE_NAME=""
ADVERTISE_HOST=""
NETWORK_ZONE="provider"
AGENT_PORT="9010"
MODEL_DIR="/srv/model-serving/models"
WORKSPACE_DIR="/srv/model-serving/workspaces"
CACHE_DIR="/srv/model-serving/cache"
OUTPUT_DIR="/srv/model-serving/output"
MODEL_QUOTA_GIB="200"
WORKSPACE_QUOTA_GIB="100"
CACHE_QUOTA_GIB="100"
OUTPUT_QUOTA_GIB="100"
RUNTIME_IMAGE="model-serving/vllm-ssh:provider"
VLLM_BASE_IMAGE="nvcr.io/nvidia/vllm:25.09-py3"
BUILD_RUNTIME=1
SCAN_EXISTING=1
ALLOW_INSECURE_HTTP=0
ASSUME_YES=0
PAYLOAD_FILE=""

usage() {
  cat <<'EOF'
Usage: sudo -E ./scripts/register_provider.sh [options]

Required interactively or by option:
  --control-url URL        Platform URL reachable from this node
  --token-file PATH        File containing a short-lived enrollment token
  --node-id ID             Stable node identifier

Common options:
  --name NAME              Display name
  --host HOST              Address the control plane uses to reach this node
  --network-zone NAME      Network zone label (default: provider)
  --agent-port PORT        Node Agent port (default: 9010)
  --model-dir PATH         Authorized model directory
  --workspace-dir PATH     Authorized per-Replica workspace directory
  --cache-dir PATH         Authorized cache directory
  --output-dir PATH        Authorized output directory
  --runtime-image REF      Runtime tag to build or an existing image reference
  --base-image REF         Pinned NVIDIA/vLLM base image used for the build
  --skip-build             Use --runtime-image without building it
  --skip-scan              Do not start the initial model-directory scan
  --allow-insecure-http    Permit enrollment over plain HTTP (trusted LAN only)
  --yes                    Accept defaults without confirmation
  -h, --help               Show this help

The enrollment token may alternatively be supplied in
MODEL_SERVING_ENROLLMENT_TOKEN. Avoid passing a token directly on the command
line because command histories and process listings may expose it.
EOF
}

while (($#)); do
  case "$1" in
    --control-url) CONTROL_URL="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --name) NODE_NAME="$2"; shift 2 ;;
    --host) ADVERTISE_HOST="$2"; shift 2 ;;
    --network-zone) NETWORK_ZONE="$2"; shift 2 ;;
    --agent-port) AGENT_PORT="$2"; shift 2 ;;
    --model-dir) MODEL_DIR="$2"; shift 2 ;;
    --workspace-dir) WORKSPACE_DIR="$2"; shift 2 ;;
    --cache-dir) CACHE_DIR="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --runtime-image) RUNTIME_IMAGE="$2"; shift 2 ;;
    --base-image) VLLM_BASE_IMAGE="$2"; shift 2 ;;
    --skip-build) BUILD_RUNTIME=0; shift ;;
    --skip-scan) SCAN_EXISTING=0; shift ;;
    --allow-insecure-http) ALLOW_INSECURE_HTTP=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cleanup() {
  if [[ -n "$PAYLOAD_FILE" && -f "$PAYLOAD_FILE" ]]; then
    rm -f -- "$PAYLOAD_FILE"
  fi
}
trap cleanup EXIT

prompt_default() {
  local variable_name="$1" label="$2" current_value="${!1}"
  if [[ "$ASSUME_YES" == 1 && -n "$current_value" ]]; then return; fi
  local entered
  read -r -p "$label [$current_value]: " entered
  printf -v "$variable_name" '%s' "${entered:-$current_value}"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

validate_id() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || {
    echo "Invalid identifier: $1" >&2
    exit 1
  }
}

validate_path() {
  [[ "$1" == /* && "$1" != *$'\n'* && "$1" != *$'\r'* && "$1" != *" "* ]] || {
    echo "Path must be absolute and contain no whitespace: $1" >&2
    exit 1
  }
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This bootstrap currently supports Linux/NVIDIA providers only." >&2
  exit 1
fi

for command_name in python3 curl openssl systemctl docker nvidia-smi nvidia-ctk; do
  require_command "$command_name"
done

if [[ ! -f "$PROJECT_ROOT/node_agent.py" || ! -f "$PROJECT_ROOT/requirements.txt" ]]; then
  echo "Run this script from a complete clone of the model_serving repository." >&2
  exit 1
fi

if [[ -z "$CONTROL_URL" ]]; then read -r -p "Control-plane URL: " CONTROL_URL; fi
CONTROL_URL="${CONTROL_URL%/}"
if [[ "$CONTROL_URL" != https://* ]]; then
  if [[ "$CONTROL_URL" != http://* || "$ALLOW_INSECURE_HTTP" != 1 ]]; then
    echo "Enrollment requires HTTPS. Use --allow-insecure-http only on a trusted private network." >&2
    exit 1
  fi
fi

if [[ -z "$NODE_ID" ]]; then
  default_id="$(hostname -s | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9.-')"
  NODE_ID="$default_id"
  prompt_default NODE_ID "Node ID"
fi
validate_id "$NODE_ID"

NODE_NAME="${NODE_NAME:-$(hostname -s)}"
ADVERTISE_HOST="${ADVERTISE_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
prompt_default NODE_NAME "Node display name"
prompt_default ADVERTISE_HOST "Address reachable from the control plane"
prompt_default NETWORK_ZONE "Network zone"
prompt_default AGENT_PORT "Node Agent port"
prompt_default MODEL_DIR "Model directory"
prompt_default WORKSPACE_DIR "Workspace directory"
prompt_default CACHE_DIR "Cache directory"
prompt_default OUTPUT_DIR "Output directory"

[[ "$AGENT_PORT" =~ ^[0-9]+$ ]] && ((AGENT_PORT >= 1 && AGENT_PORT <= 65535)) || {
  echo "Invalid Agent port: $AGENT_PORT" >&2; exit 1;
}
for directory in "$MODEL_DIR" "$WORKSPACE_DIR" "$CACHE_DIR" "$OUTPUT_DIR"; do
  validate_path "$directory"
done

if [[ -n "$TOKEN_FILE" ]]; then
  [[ -r "$TOKEN_FILE" ]] || { echo "Cannot read token file: $TOKEN_FILE" >&2; exit 1; }
  ENROLLMENT_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
elif [[ -n "${MODEL_SERVING_ENROLLMENT_TOKEN:-}" ]]; then
  ENROLLMENT_TOKEN="$MODEL_SERVING_ENROLLMENT_TOKEN"
else
  read -r -s -p "Enrollment token: " ENROLLMENT_TOKEN
  echo
fi
[[ "$ENROLLMENT_TOKEN" == mse_* ]] || { echo "Invalid enrollment token format." >&2; exit 1; }

echo "[1/8] Checking Docker and NVIDIA runtime"
sudo docker info >/dev/null
nvidia-smi >/dev/null
sudo install -d -m 0755 /etc/cdi
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null
if ! sudo nvidia-ctk cdi list | grep -q 'nvidia.com/gpu'; then
  echo "NVIDIA CDI devices were not generated. Check the driver and Container Toolkit installation." >&2
  exit 1
fi

echo "[2/8] Creating authorized storage directories"
sudo install -d -m 0755 "$MODEL_DIR"
sudo install -d -m 0770 "$WORKSPACE_DIR" "$CACHE_DIR" "$OUTPUT_DIR"
sudo install -d -m 0750 /opt/model-serving /var/lib/model-serving-agent

echo "[3/8] Installing the Node Agent"
sudo install -m 0644 "$PROJECT_ROOT/node_agent.py" /opt/model-serving/node_agent.py
if [[ ! -x /opt/model-serving/.venv/bin/python ]]; then
  sudo python3 -m venv /opt/model-serving/.venv
fi
sudo /opt/model-serving/.venv/bin/python -m pip install --upgrade pip >/dev/null
sudo /opt/model-serving/.venv/bin/python -m pip install -r "$PROJECT_ROOT/requirements.txt" >/dev/null

AGENT_KEY="$(openssl rand -hex 32)"
ENV_TMP="$(mktemp)"
chmod 600 "$ENV_TMP"
printf 'NODE_AGENT_API_KEY=%s\nMODEL_SERVING_AGENT_STATE=/var/lib/model-serving-agent\nNODE_AGENT_PORT=%s\n' \
  "$AGENT_KEY" "$AGENT_PORT" > "$ENV_TMP"
sudo install -m 0600 "$ENV_TMP" /etc/model-serving-agent.env
rm -f -- "$ENV_TMP"

SERVICE_TMP="$(mktemp)"
cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=Standalone Model Serving Node Agent
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/model-serving
EnvironmentFile=/etc/model-serving-agent.env
ExecStart=/opt/model-serving/.venv/bin/python -m uvicorn node_agent:app --host 0.0.0.0 --port ${AGENT_PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/model-serving-agent /var/run/docker.sock ${MODEL_DIR} ${WORKSPACE_DIR} ${CACHE_DIR} ${OUTPUT_DIR}

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$SERVICE_TMP" /etc/systemd/system/model-serving-agent.service
rm -f -- "$SERVICE_TMP"
sudo systemctl daemon-reload
sudo systemctl enable --now model-serving-agent

echo "[4/8] Verifying the Node Agent"
for _ in {1..30}; do
  if curl --silent --fail "http://127.0.0.1:${AGENT_PORT}/healthz" >/dev/null; then break; fi
  sleep 1
done
curl --silent --fail "http://127.0.0.1:${AGENT_PORT}/healthz" >/dev/null || {
  sudo journalctl -u model-serving-agent --no-pager -n 80 >&2
  exit 1
}
curl --silent --fail -H "Authorization: Bearer ${AGENT_KEY}" \
  "http://127.0.0.1:${AGENT_PORT}/v1/inventory" >/dev/null

echo "[5/8] Preparing the vLLM + SSH runtime image"
if [[ "$BUILD_RUNTIME" == 1 ]]; then
  sudo docker build --build-arg "VLLM_BASE_IMAGE=${VLLM_BASE_IMAGE}" \
    -t "$RUNTIME_IMAGE" "$PROJECT_ROOT/runtime/llm-vllm-ssh"
else
  sudo docker image inspect "$RUNTIME_IMAGE" >/dev/null
fi
IMAGE_ID="$(sudo docker image inspect "$RUNTIME_IMAGE" --format '{{.Id}}')"
CONTRACT="$(sudo docker image inspect "$RUNTIME_IMAGE" --format '{{index .Config.Labels "io.model-serving.runtime-contract"}}')"
[[ "$CONTRACT" == "vllm-ssh-v1" ]] || { echo "Runtime image does not implement vllm-ssh-v1." >&2; exit 1; }

echo "[6/8] Running a GPU container probe"
sudo docker run --rm --device nvidia.com/gpu=all --entrypoint nvidia-smi "$RUNTIME_IMAGE" >/dev/null

echo "[7/8] Enrolling node, storage pools and runtime profile"
ARCHITECTURE="$(uname -m)"
PLATFORM="NVIDIA $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1 | xargs)"
AGENT_KEY_ENV="NODE_$(printf '%s' "$NODE_ID" | tr '[:lower:]' '[:upper:]' | tr '.-' '__')_AGENT_KEY"
PROFILE_ID="vllm-${NODE_ID}"
MODEL_POOL_ID="${NODE_ID}-models"
WORKSPACE_POOL_ID="${NODE_ID}-workspace"
CACHE_POOL_ID="${NODE_ID}-cache"
OUTPUT_POOL_ID="${NODE_ID}-output"
PAYLOAD_FILE="$(mktemp)"
chmod 600 "$PAYLOAD_FILE"

export ENROLLMENT_TOKEN AGENT_KEY NODE_ID NODE_NAME ADVERTISE_HOST NETWORK_ZONE AGENT_PORT
export ARCHITECTURE PLATFORM AGENT_KEY_ENV PROFILE_ID IMAGE_ID SCAN_EXISTING
export MODEL_DIR WORKSPACE_DIR CACHE_DIR OUTPUT_DIR
export MODEL_QUOTA_GIB WORKSPACE_QUOTA_GIB CACHE_QUOTA_GIB OUTPUT_QUOTA_GIB
export MODEL_POOL_ID WORKSPACE_POOL_ID CACHE_POOL_ID OUTPUT_POOL_ID
/opt/model-serving/.venv/bin/python - "$PAYLOAD_FILE" <<'PY'
import json, os, sys

def pool(identifier, host_path, container_path, purpose, agent_access, runtime_access, quota):
    return {
        "id": identifier, "host_path": host_path, "container_path": container_path,
        "purpose": purpose, "agent_access": agent_access, "runtime_access": runtime_access,
        "quota_gib": float(quota), "min_free_gib": 5.0,
    }

payload = {
    "enrollment_token": os.environ["ENROLLMENT_TOKEN"],
    "agent_key": os.environ["AGENT_KEY"],
    "node": {
        "id": os.environ["NODE_ID"], "name": os.environ["NODE_NAME"],
        "host": os.environ["ADVERTISE_HOST"],
        "agent_url": f"http://{os.environ['ADVERTISE_HOST']}:{os.environ['AGENT_PORT']}",
        "agent_key_env": os.environ["AGENT_KEY_ENV"],
        "platform": os.environ["PLATFORM"], "network_zone": os.environ["NETWORK_ZONE"],
    },
    "storage_pools": [
        pool(os.environ["MODEL_POOL_ID"], os.environ["MODEL_DIR"], "/models", "model", "rw", "ro", os.environ["MODEL_QUOTA_GIB"]),
        pool(os.environ["WORKSPACE_POOL_ID"], os.environ["WORKSPACE_DIR"], "/workspace", "workspace", "rw", "rw", os.environ["WORKSPACE_QUOTA_GIB"]),
        pool(os.environ["CACHE_POOL_ID"], os.environ["CACHE_DIR"], "/cache", "cache", "rw", "rw", os.environ["CACHE_QUOTA_GIB"]),
        pool(os.environ["OUTPUT_POOL_ID"], os.environ["OUTPUT_DIR"], "/output", "output", "rw", "rw", os.environ["OUTPUT_QUOTA_GIB"]),
    ],
    "runtime_profiles": [{
        "id": os.environ["PROFILE_ID"], "name": f"vLLM on {os.environ['NODE_NAME']}",
        "runtime": "vllm", "image": os.environ["IMAGE_ID"],
        "architectures": [os.environ["ARCHITECTURE"]], "accelerator_kinds": ["GPU"],
        "allowed_args": ["max_model_len", "gpu_memory_utilization", "max_num_seqs", "enforce_eager", "dtype"],
        "enabled": True,
    }],
    "scan_existing_models": os.environ["SCAN_EXISTING"] == "1",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
PY

RESPONSE_FILE="$(mktemp)"
chmod 600 "$RESPONSE_FILE"
HTTP_STATUS="$(curl --silent --show-error --output "$RESPONSE_FILE" --write-out '%{http_code}' \
  -H 'Content-Type: application/json' --data-binary "@${PAYLOAD_FILE}" "${CONTROL_URL}/api/enroll")"
if [[ "$HTTP_STATUS" != "201" ]]; then
  echo "Enrollment failed with HTTP ${HTTP_STATUS}:" >&2
  /opt/model-serving/.venv/bin/python -m json.tool "$RESPONSE_FILE" >&2 2>/dev/null || cat "$RESPONSE_FILE" >&2
  rm -f -- "$RESPONSE_FILE"
  exit 1
fi

echo "[8/8] Enrollment completed"
/opt/model-serving/.venv/bin/python - "$RESPONSE_FILE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
node = data["node"]
print(f"Node: {node['id']} ({node['state']})")
print("Storage pools:")
for pool in data.get("storage_pools", []):
    print(f"  - {pool['id']}: {pool['purpose']} {pool['state']} -> {pool['host_path']}")
print("Runtime profiles:")
for profile in data.get("runtime_profiles", []):
    print(f"  - {profile['id']}: {profile['runtime']} {profile['image']}")
scan = data.get("initial_scan")
print(f"Initial scan: {scan['id']} ({scan['state']})" if scan else "Initial scan: skipped")
PY
rm -f -- "$RESPONSE_FILE"

unset ENROLLMENT_TOKEN AGENT_KEY MODEL_SERVING_ENROLLMENT_TOKEN || true
echo "Provider-side setup is complete. Keep TCP ${AGENT_PORT} and Docker-published Replica SSH ports reachable only from the control plane."
