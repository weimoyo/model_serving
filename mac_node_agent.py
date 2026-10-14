"""macOS/Apple-Silicon Node Agent using MLX-LM process runtimes."""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


STATE_DIR = Path(os.getenv("MODEL_SERVING_AGENT_STATE", str(Path.home() / ".model-serving-agent")))
PYTHON = os.getenv("MLX_LM_PYTHON", str(STATE_DIR / "venv" / "bin" / "python"))
TOKEN_FILE = Path(os.getenv("NODE_AGENT_API_KEY_FILE", str(STATE_DIR / "agent.token")))
app = FastAPI(title="Model Serving Mac Node Agent", version="1.0.0")


def configured_key() -> Optional[str]:
    value = os.getenv("NODE_AGENT_API_KEY")
    if value:
        return value
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
    return value


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    expected = configured_key()
    supplied = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    if not expected:
        raise HTTPException(503, "NODE_AGENT_API_KEY is not configured")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid node agent key")


def run(args: list[str], *, timeout: int = 20) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"command unavailable: {args[0]}: {exc}") from exc
    if result.returncode:
        raise HTTPException(503, f"{args[0]} failed: {result.stderr[:500]}")
    return result.stdout.strip()


def machine_uuid() -> str:
    try:
        output = run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
        if match:
            return match.group(1)
    except HTTPException:
        pass
    return platform.node()


def memory_gib() -> float:
    return round(int(run(["sysctl", "-n", "hw.memsize"])) / 1024**3, 2)


def chip_name() -> str:
    try:
        return run(["sysctl", "-n", "machdep.cpu.brand_string"])
    except HTTPException:
        return "Apple Silicon"


def state_file(runtime_id: str) -> Path:
    if not re.fullmatch(r"model-serving-[A-Za-z0-9_.-]{1,160}", runtime_id):
        raise HTTPException(422, "invalid runtime id")
    return STATE_DIR / "runtimes" / f"{runtime_id}.json"


def load_state(runtime_id: str) -> Optional[dict[str, Any]]:
    path = state_file(runtime_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(503, f"invalid runtime state: {exc}") from exc


def save_state(value: dict[str, Any]) -> None:
    path = state_file(value["runtime_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def observe(runtime_id: str) -> dict[str, Any]:
    value = load_state(runtime_id)
    if not value:
        return {"runtime_id": runtime_id, "state": "missing"}
    value["state"] = "running" if process_alive(int(value["pid"])) else "exited"
    return value


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ReplicaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replica_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    runtime: Literal["vllm", "mlx"]
    image: str
    model_repository: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(min_length=7, max_length=200)
    served_model_id: str = Field(min_length=1, max_length=256)
    runtime_args: dict[str, Any] = Field(default_factory=dict)
    gpu_uuids: list[str] = Field(min_length=1)
    cpu_cores: Optional[int] = None
    memory_gib: Optional[int] = None
    gateway_public_key: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/v1/inventory", dependencies=[Depends(auth)])
def inventory():
    chip = chip_name()
    return {"hostname": platform.node(), "architecture": platform.machine(), "os": platform.platform(),
            "cpu_count": os.cpu_count(), "memory_gib": memory_gib(), "runtime": "mlx",
            "accelerators": [{"kind": "Apple Metal", "uuid": f"APPLE-{machine_uuid()}", "model": chip,
                              "memory_gib": memory_gib(), "state": "available"}]}


@app.post("/v1/replicas", dependencies=[Depends(auth)], status_code=201)
def create_replica(body: ReplicaCreate):
    if body.runtime != "mlx":
        raise HTTPException(422, "this Mac agent supports only the mlx runtime")
    expected_uuid = inventory()["accelerators"][0]["uuid"]
    if body.gpu_uuids != [expected_uuid]:
        raise HTTPException(409, "requested Apple accelerator is unavailable")
    runtime_id = "model-serving-" + body.replica_id
    current = observe(runtime_id)
    if current["state"] != "missing":
        return current
    if not Path(PYTHON).exists():
        raise HTTPException(503, f"MLX Python environment not found: {PYTHON}")
    port = free_port()
    command = [PYTHON, "-m", "mlx_lm.server", "--model", body.model_repository,
               "--host", "127.0.0.1", "--port", str(port)]
    for key, value in body.runtime_args.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            command.append(flag)
        elif value is not False and value is not None:
            command.extend([flag, str(value)])
    log_dir = STATE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{runtime_id}.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    value = {"runtime_id": runtime_id, "pid": process.pid, "state": "running", "ssh_port": 22,
             "ssh_user": os.getenv("USER", "lix"), "runtime_port": port,
             "model_repository": body.model_repository, "model_revision": body.model_revision,
             "served_model_id": body.served_model_id, "log_path": str(log_path)}
    save_state(value)
    return value


@app.get("/v1/replicas/{runtime_id}", dependencies=[Depends(auth)])
def get_replica(runtime_id: str):
    return observe(runtime_id)


@app.delete("/v1/replicas/{runtime_id}", dependencies=[Depends(auth)])
def delete_replica(runtime_id: str):
    value = load_state(runtime_id)
    if value and process_alive(int(value["pid"])):
        try:
            os.killpg(int(value["pid"]), signal.SIGTERM)
        except OSError:
            pass
    path = state_file(runtime_id)
    if path.exists():
        path.unlink()
    return {"runtime_id": runtime_id, "state": "removed"}


@app.get("/v1/replicas/{runtime_id}/logs", dependencies=[Depends(auth)])
def replica_logs(runtime_id: str):
    value = load_state(runtime_id)
    if not value:
        raise HTTPException(404, "runtime not found")
    path = Path(value["log_path"])
    return {"runtime_id": runtime_id, "log": path.read_text(encoding="utf-8", errors="replace")[-20000:] if path.exists() else ""}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("NODE_AGENT_PORT", "9010")))
