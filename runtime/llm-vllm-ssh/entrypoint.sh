#!/usr/bin/env bash
set -euo pipefail

: "${MODEL_REPOSITORY:?MODEL_REPOSITORY is required}"
: "${MODEL_REVISION:?MODEL_REVISION is required}"
: "${SERVED_MODEL_NAME:?SERVED_MODEL_NAME is required}"

install -d -m 0700 /run/model-serving /run/sshd /workspace/.model-serving/ssh-host-keys

ensure_host_key() {
  local kind="$1" path="$2"
  shift 2
  if [ ! -s "$path" ] || ! ssh-keygen -y -f "$path" >/dev/null 2>&1; then
    rm -f "$path" "$path.pub"
    ssh-keygen -q -t "$kind" "$@" -N '' -f "$path"
  fi
  chmod 0600 "$path"
}

ED25519=/workspace/.model-serving/ssh-host-keys/ssh_host_ed25519_key
RSA=/workspace/.model-serving/ssh-host-keys/ssh_host_rsa_key
ensure_host_key ed25519 "$ED25519"
ensure_host_key rsa "$RSA" -b 3072

cat > /run/model-serving/sshd_config <<EOF
Port 22
ListenAddress 0.0.0.0
PidFile /run/model-serving/sshd.pid
HostKey $ED25519
HostKey $RSA
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
AuthorizedKeysFile /model-serving-auth/authorized_keys
StrictModes ${MODEL_SERVING_SSH_STRICT_MODES:-yes}
AllowAgentForwarding no
AllowTcpForwarding yes
PermitOpen 127.0.0.1:${VLLM_PORT:-8000} localhost:${VLLM_PORT:-8000}
X11Forwarding no
ClientAliveInterval 60
ClientAliveCountMax 5
Subsystem sftp internal-sftp
EOF

EXTRA_ARGS=()
if [ -n "${VLLM_ARGS_JSON:-}" ]; then
  mapfile -d '' -t EXTRA_ARGS < <(python3 - <<'PY'
import json, os, sys
value = json.loads(os.environ.get("VLLM_ARGS_JSON", "{}"))
if not isinstance(value, dict):
    raise SystemExit("VLLM_ARGS_JSON must be an object")
for key, item in value.items():
    flag = "--" + key.replace("_", "-")
    if item is True:
        sys.stdout.write(flag + "\0")
    elif item is False or item is None:
        continue
    elif isinstance(item, list):
        for element in item:
            sys.stdout.write(flag + "\0" + str(element) + "\0")
    else:
        sys.stdout.write(flag + "\0" + str(item) + "\0")
PY
  )
fi

/usr/sbin/sshd -D -e -f /run/model-serving/sshd_config &
SSHD_PID=$!
python3 -m vllm.entrypoints.openai.api_server \
  --host "${VLLM_HOST:-127.0.0.1}" \
  --port "${VLLM_PORT:-8000}" \
  --model "$MODEL_REPOSITORY" \
  --revision "$MODEL_REVISION" \
  --served-model-name "$SERVED_MODEL_NAME" \
  "${EXTRA_ARGS[@]}" &
VLLM_PID=$!

shutdown() {
  kill -TERM "$VLLM_PID" "$SSHD_PID" 2>/dev/null || true
  wait "$VLLM_PID" "$SSHD_PID" 2>/dev/null || true
}
trap shutdown TERM INT EXIT

wait -n "$SSHD_PID" "$VLLM_PID"
STATUS=$?
shutdown
exit "$STATUS"
