"""Standalone GPU node agent for this project (no Snowpack dependency)."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


STATE_DIR = Path(os.getenv("MODEL_SERVING_AGENT_STATE", "/var/lib/model-serving-agent"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
app = FastAPI(title="Model Serving Node Agent", version="1.1.0")
JOB_LOCK = threading.Lock()


def auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("NODE_AGENT_API_KEY")
    supplied = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    if not expected:
        raise HTTPException(503, "NODE_AGENT_API_KEY is not configured")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid node agent key")


def command(args: list[str], *, timeout: int = 30, allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"command unavailable: {args[0]}: {exc}") from exc
    if result.returncode and not allow_failure:
        raise HTTPException(503, f"{args[0]} failed: {result.stderr[:500]}")
    return result.stdout.strip()


def container_logs(name: str) -> str:
    try:
        result = subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout + result.stderr).strip()[-8000:]


def memory_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024**2, 2)
    except (OSError, ValueError):
        return None
    return None


def gpu_inventory() -> list[dict[str, Any]]:
    output = command(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.free,utilization.gpu", "--format=csv,noheader,nounits"], allow_failure=True)
    result = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        gpu: dict[str, Any] = {"kind": "GPU", "uuid": parts[0], "model": parts[1], "state": "available"}
        # GB10 uses unified memory and may report N/A for nvidia-smi memory
        # fields. UUID and model are still sufficient for CDI placement.
        try:
            gpu["memory_gib"] = round(float(parts[2]) / 1024, 2)
        except ValueError:
            gpu["memory_gib"] = None
        try:
            gpu["memory_free_gib"] = round(float(parts[3]) / 1024, 2)
        except ValueError:
            gpu["memory_free_gib"] = None
        try:
            gpu["utilization_percent"] = float(parts[4])
        except ValueError:
            gpu["utilization_percent"] = None
        result.append(gpu)
    return result


def runtime_image_inventory() -> list[dict[str, Any]]:
    ids = command(["docker", "image", "ls", "--no-trunc", "--quiet", "--filter",
                   "label=io.model-serving.runtime-contract=vllm-ssh-v1"], allow_failure=True)
    result = []
    for image_id in dict.fromkeys(ids.splitlines()):
        raw = command(["docker", "image", "inspect", image_id], allow_failure=True)
        try:
            info = json.loads(raw)[0]
        except (ValueError, IndexError, TypeError):
            continue
        result.append({"id": info.get("Id"), "repo_tags": info.get("RepoTags") or [],
                       "repo_digests": info.get("RepoDigests") or [], "architecture": info.get("Architecture"),
                       "created": info.get("Created"), "contract": "vllm-ssh-v1"})
    return result


class ReplicaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replica_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    runtime: Literal["vllm", "mlx"] = "vllm"
    image: str = Field(min_length=1, max_length=500)
    model_repository: str = Field(min_length=1, max_length=500)
    model_revision: str = Field(min_length=7, max_length=200)
    served_model_id: str = Field(min_length=1, max_length=256)
    runtime_args: dict[str, Any] = Field(default_factory=dict)
    gpu_uuids: list[str] = Field(min_length=1)
    cpu_cores: int | None = Field(default=None, ge=1)
    memory_gib: int | None = Field(default=None, ge=1)
    gateway_public_key: str = Field(min_length=40, max_length=2000)
    artifact_storage_pool_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    artifact_relative_path: str | None = Field(default=None, max_length=1000)
    workspace_storage_pool_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class StoragePoolGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    host_path: str = Field(min_length=1, max_length=1000)
    container_path: str = Field(min_length=1, max_length=500)
    purpose: Literal["model", "workspace", "cache", "output"]
    agent_access: Literal["ro", "rw"]
    runtime_access: Literal["none", "ro", "rw"]
    quota_gib: float | None = Field(default=None, gt=0)
    min_free_gib: float = Field(default=1, ge=0)


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_pool_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    relative_path: str = Field(default=".", min_length=1, max_length=1000)


class DownloadRequest(ScanRequest):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    relative_path: str | None = Field(default=None, max_length=1000)


class VerifyArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_pool_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    relative_path: str = Field(min_length=1, max_length=1000)
    checksum: str = Field(min_length=20, max_length=200)


def json_state(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError) as exc:
        raise HTTPException(503, f"invalid agent state {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def pool_file() -> Path:
    return STATE_DIR / "storage_pools.json"


def load_pool(pool_id: str) -> dict[str, Any]:
    value = json_state(pool_file(), {}).get(pool_id)
    if not value:
        raise HTTPException(404, "storage pool is not registered")
    return value


def resolve_in_pool(pool: dict[str, Any], relative_path: str | None, *, must_exist: bool = True) -> Path:
    if not relative_path:
        relative_path = "."
    supplied = Path(relative_path)
    if supplied.is_absolute() or ".." in supplied.parts:
        raise HTTPException(422, "path must be relative and cannot contain '..'")
    root = Path(pool["host_path"]).resolve(strict=True)
    candidate = (root / supplied).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(403, "path escapes the authorized storage pool") from exc
    return candidate


def pool_observation(value: dict[str, Any]) -> dict[str, Any]:
    root = Path(value["host_path"]).expanduser()
    if not root.is_absolute():
        raise HTTPException(422, "host_path must be absolute")
    if not root.exists() or not root.is_dir():
        raise HTTPException(422, "host_path must be an existing directory")
    root = root.resolve(strict=True)
    if not os.access(root, os.R_OK):
        raise HTTPException(403, "storage pool is not readable")
    if value["agent_access"] == "rw":
        probe = root / (".model-serving-write-test-" + uuid.uuid4().hex)
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(403, f"storage pool is not writable in the Agent sandbox: {exc}") from exc
    usage = shutil.disk_usage(root)
    min_free = int(float(value.get("min_free_gib", 0)) * 1024**3)
    if usage.free < min_free:
        raise HTTPException(409, "storage pool does not satisfy min_free_gib")
    used_bytes = sum(item.stat().st_size for item in root.rglob("*") if item.is_file() and not item.is_symlink())
    quota_bytes = int(value["quota_gib"] * 1024**3) if value.get("quota_gib") else None
    if quota_bytes is not None and used_bytes > quota_bytes:
        raise HTTPException(409, "storage pool usage exceeds quota_gib")
    return {**value, "host_path": str(root), "state": "READY", "capacity_bytes": usage.total,
            "free_bytes": usage.free, "used_bytes": used_bytes}


def jobs_file(kind: str) -> Path:
    return STATE_DIR / f"{kind}_jobs.json"


def load_jobs(kind: str) -> dict[str, Any]:
    with JOB_LOCK:
        return json_state(jobs_file(kind), {})


def update_job(kind: str, job_id: str, **fields: Any) -> None:
    with JOB_LOCK:
        jobs = json_state(jobs_file(kind), {})
        jobs[job_id] = {**jobs.get(job_id, {}), **fields}
        write_json(jobs_file(kind), jobs)


def model_manifest(path: Path, authorized_root: Path | None = None) -> dict[str, Any] | None:
    config_path = path / "config.json"
    gguf = sorted(path.glob("*.gguf"))
    safetensors = sorted(path.glob("*.safetensors"))
    bins = sorted(path.glob("pytorch_model*.bin"))
    ollama = (path / "Modelfile").exists() or (path / "manifest.json").exists() and (path / "blobs").exists()
    nim = (path / "config.pbtxt").exists() or (path / "model-store").exists()
    if not config_path.exists() and not gguf and not ollama and not nim:
        return None
    files = []
    root = (authorized_root or path).resolve(strict=True)
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            item.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            continue
        files.append(item)
    files.sort()
    total = sum(item.stat().st_size for item in files)
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(path).as_posix()
        stat = item.stat()
        digest.update(f"{relative}\0{stat.st_size}\n".encode())
        if relative in {"config.json", "tokenizer_config.json", "generation_config.json"}:
            digest.update(item.read_bytes())
    metadata: dict[str, Any] = {"files": len(files), "weight_files": len(safetensors or bins or gguf),
                                "tokenizer_present": any((path / name).exists() for name in
                                ("tokenizer.json", "tokenizer.model", "tokenizer_config.json"))}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            metadata.update({"architectures": config.get("architectures") or [], "model_type": config.get("model_type"),
                             "revision": config.get("_commit_hash")})
        except (OSError, ValueError):
            metadata["config_invalid"] = True
    if ollama:
        fmt, compatible = "ollama", False
    elif nim:
        fmt, compatible = "nim-or-triton", False
    elif gguf:
        fmt, compatible = "gguf", False
    elif safetensors:
        fmt, compatible = "safetensors", True
    elif bins:
        fmt, compatible = "pytorch", True
    else:
        fmt, compatible = "transformers", True
    return {"format": fmt, "runtime_compatible": compatible, "size_bytes": total,
            "checksum": "manifest-sha256:" + digest.hexdigest(), "metadata": metadata}


def scan_worker(job_id: str, pool: dict[str, Any], relative_path: str) -> None:
    try:
        root = resolve_in_pool(pool, relative_path)
        update_job("scan", job_id, state="RUNNING", progress={"directories_checked": 0})
        result, checked = [], 0
        candidates = [root]
        for current, directories, _files in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if not (Path(current) / name).is_symlink() and name != ".staging"]
            candidates.extend(Path(current) / name for name in directories)
        for candidate in candidates:
            current = load_jobs("scan").get(job_id, {})
            if current.get("state") == "CANCEL_REQUESTED":
                update_job("scan", job_id, state="CANCELLED", failure_reason="scan cancelled by control plane")
                return
            checked += 1
            manifest = model_manifest(candidate, Path(pool["host_path"]))
            if manifest:
                relative = candidate.relative_to(Path(pool["host_path"])).as_posix() or "."
                result.append({"relative_path": relative, **manifest})
            if checked % 25 == 0:
                update_job("scan", job_id, progress={"directories_checked": checked, "models_found": len(result)})
        update_job("scan", job_id, state="COMPLETED", progress={"directories_checked": checked, "models_found": len(result)}, result=result)
    except Exception as exc:
        update_job("scan", job_id, state="FAILED", failure_reason=f"{type(exc).__name__}: {str(exc)[:500]}")


def download_worker(job_id: str, body: dict[str, Any], pool: dict[str, Any]) -> None:
    staging: Path | None = None
    try:
        if pool["purpose"] != "model" or pool["agent_access"] != "rw":
            raise RuntimeError("download target must be a writable model storage pool")
        root = resolve_in_pool(pool, ".")
        relative = body.get("relative_path") or f"{body['repository'].replace('/', '--')}--{body['revision']}"
        target = resolve_in_pool(pool, relative, must_exist=False)
        staging = root / ".staging" / job_id
        staging.mkdir(parents=True, exist_ok=False)
        update_job("download", job_id, state="RUNNING", progress={"phase": "downloading"}, relative_path=relative)
        try:
            # Public mirrors may advertise Xet metadata whose CAS endpoint still
            # requires separate official-site authentication. Plain HTTP/LFS is
            # deterministic and works with the configured HF endpoint.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            from huggingface_hub import HfApi, snapshot_download
        except ImportError as exc:
            raise RuntimeError("huggingface_hub is not installed in the Node Agent environment") from exc
        info = HfApi().model_info(repo_id=body["repository"], revision=body["revision"], files_metadata=True)
        expected_bytes = sum(int(item.size or 0) for item in (info.siblings or []))
        observation = pool_observation(pool)
        quota_bytes = int(pool["quota_gib"] * 1024**3) if pool.get("quota_gib") else None
        if quota_bytes is not None and observation["used_bytes"] + expected_bytes > quota_bytes:
            raise RuntimeError("download would exceed storage pool quota")
        if observation["free_bytes"] - expected_bytes < int(pool.get("min_free_gib", 0) * 1024**3):
            raise RuntimeError("download would violate storage pool minimum free space")
        update_job("download", job_id, progress={"phase": "downloading", "total_bytes": expected_bytes})
        hf_cache = STATE_DIR / "hf-cache"
        hf_cache.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=body["repository"], revision=body["revision"],
                          local_dir=str(staging), cache_dir=str(hf_cache))
        update_job("download", job_id, state="VALIDATING", progress={"phase": "validating"})
        manifest = model_manifest(staging, root)
        if not manifest or not manifest["runtime_compatible"]:
            raise RuntimeError("downloaded repository is not a supported Transformers model")
        # Staging lives inside the authorized pool, so this measurement already
        # includes the new model exactly once.
        pool_observation(pool)
        if target.exists():
            existing = model_manifest(target, root)
            if existing and existing["checksum"] == manifest["checksum"]:
                shutil.rmtree(staging)
            else:
                raise RuntimeError("download target already exists with different content")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(target)
        update_job("download", job_id, state="COMPLETED", progress={"phase": "published", "bytes": manifest["size_bytes"]},
                   result={"relative_path": relative, "repository": body["repository"], "revision": body["revision"], **manifest})
    except Exception as exc:
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        update_job("download", job_id, state="FAILED", failure_reason=f"{type(exc).__name__}: {str(exc)[:500]}")


def container_name(replica_id: str) -> str:
    if not NAME_RE.fullmatch(replica_id):
        raise HTTPException(422, "invalid replica id")
    return "model-serving-" + replica_id.lower()


def observe(name: str) -> dict[str, Any]:
    raw = command(["docker", "inspect", name], allow_failure=True)
    if not raw:
        return {"runtime_id": name, "state": "missing", "ssh_port": None}
    try:
        values = json.loads(raw)
        if not values:
            return {"runtime_id": name, "state": "missing", "ssh_port": None}
        info = values[0]
        ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
        binding = (ports.get("22/tcp") or [{}])[0]
        state = str((info.get("State") or {}).get("Status") or "unknown").lower()
        result = {"runtime_id": name, "container_id": info.get("Id"), "state": state,
                  "ssh_port": int(binding["HostPort"]) if binding.get("HostPort") else None}
        if state in {"exited", "dead", "error", "failed"}:
            result["recent_logs"] = container_logs(name)
            result["exit_code"] = (info.get("State") or {}).get("ExitCode")
        return result
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(503, f"invalid docker inspect output: {exc}") from exc


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": "1.1.0"}


@app.get("/v1/inventory", dependencies=[Depends(auth)])
def inventory():
    docker_version = command(["docker", "version", "--format", "{{.Server.Version}}"], allow_failure=True)
    return {"hostname": platform.node(), "architecture": platform.machine(), "os": platform.platform(),
            "cpu_count": os.cpu_count(), "memory_gib": memory_gib(), "docker_version": docker_version or None,
            "runtime": "vllm", "agent_version": "1.1.0",
            "features": ["storage-pools", "model-scan", "artifact-verify", "hf-download", "managed-mounts",
                         "runtime-image-inventory"],
            "accelerators": gpu_inventory(), "runtime_images": runtime_image_inventory()}


@app.put("/v1/storage-pools/{pool_id}", dependencies=[Depends(auth)])
def put_storage_pool(pool_id: str, body: StoragePoolGrant):
    if pool_id != body.id:
        raise HTTPException(422, "path and body storage pool IDs differ")
    observed = pool_observation(body.model_dump())
    pools = json_state(pool_file(), {})
    pools[pool_id] = observed
    write_json(pool_file(), pools)
    return observed


@app.get("/v1/storage-pools", dependencies=[Depends(auth)])
def list_storage_pools():
    values = []
    for pool in json_state(pool_file(), {}).values():
        try:
            values.append(pool_observation(pool))
        except HTTPException as exc:
            values.append({**pool, "state": "ERROR", "failure_reason": str(exc.detail)})
    return {"data": values}


@app.delete("/v1/storage-pools/{pool_id}", dependencies=[Depends(auth)])
def delete_storage_pool(pool_id: str):
    pools = json_state(pool_file(), {})
    if pool_id not in pools:
        raise HTTPException(404, "storage pool is not registered")
    pools.pop(pool_id)
    write_json(pool_file(), pools)
    return {"id": pool_id, "state": "REVOKED"}


@app.post("/v1/scans", dependencies=[Depends(auth)], status_code=202)
def start_scan(body: ScanRequest):
    pool = load_pool(body.storage_pool_id)
    resolve_in_pool(pool, body.relative_path)
    for existing in load_jobs("scan").values():
        if (existing.get("storage_pool_id") == body.storage_pool_id and existing.get("relative_path") == body.relative_path
                and existing.get("state") in {"QUEUED", "RUNNING"}):
            return existing
    job_id = "scan-" + uuid.uuid4().hex
    update_job("scan", job_id, id=job_id, storage_pool_id=body.storage_pool_id,
               relative_path=body.relative_path, state="QUEUED", progress={}, result=[])
    threading.Thread(target=scan_worker, args=(job_id, pool, body.relative_path), daemon=True).start()
    return load_jobs("scan")[job_id]


@app.get("/v1/scans/{job_id}", dependencies=[Depends(auth)])
def get_scan(job_id: str):
    job = load_jobs("scan").get(job_id)
    if not job:
        raise HTTPException(404, "scan job not found")
    return job


@app.post("/v1/artifacts/verify", dependencies=[Depends(auth)])
def verify_artifact(body: VerifyArtifactRequest):
    pool = load_pool(body.storage_pool_id)
    path = resolve_in_pool(pool, body.relative_path)
    manifest = model_manifest(path, Path(pool["host_path"]))
    if not manifest:
        return {"state": "MISSING", "failure_reason": "model structure is missing"}
    if not secrets.compare_digest(manifest["checksum"], body.checksum):
        return {"state": "CORRUPT", "failure_reason": "artifact checksum does not match", "observed": manifest}
    return {"state": "READY", "observed": manifest}


@app.delete("/v1/scans/{job_id}", dependencies=[Depends(auth)])
def cancel_scan(job_id: str):
    job = load_jobs("scan").get(job_id)
    if not job:
        raise HTTPException(404, "scan job not found")
    if job["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
        return job
    update_job("scan", job_id, state="CANCEL_REQUESTED")
    return load_jobs("scan")[job_id]


@app.post("/v1/downloads", dependencies=[Depends(auth)], status_code=202)
def start_download(body: DownloadRequest):
    if body.revision.lower() in {"main", "master", "latest", "head"}:
        raise HTTPException(422, "revision must be immutable")
    pool = load_pool(body.storage_pool_id)
    for existing in load_jobs("download").values():
        if (existing.get("storage_pool_id") == body.storage_pool_id and existing.get("repository") == body.repository
                and existing.get("revision") == body.revision and existing.get("state") in {"QUEUED", "RUNNING", "VALIDATING"}):
            return existing
    job_id = "download-" + uuid.uuid4().hex
    value = body.model_dump()
    update_job("download", job_id, id=job_id, storage_pool_id=body.storage_pool_id, state="QUEUED",
               repository=body.repository, revision=body.revision, relative_path=body.relative_path, progress={})
    threading.Thread(target=download_worker, args=(job_id, value, pool), daemon=True).start()
    return load_jobs("download")[job_id]


@app.get("/v1/downloads/{job_id}", dependencies=[Depends(auth)])
def get_download(job_id: str):
    job = load_jobs("download").get(job_id)
    if not job:
        raise HTTPException(404, "download job not found")
    return job


@app.post("/v1/replicas", dependencies=[Depends(auth)], status_code=201)
def create_replica(body: ReplicaCreate):
    if body.runtime != "vllm":
        raise HTTPException(422, "this Docker agent supports only the vllm runtime")
    if not body.gateway_public_key.startswith(("ssh-ed25519 ", "ssh-rsa ")):
        raise HTTPException(422, "unsupported gateway SSH public key")
    available = {gpu["uuid"] for gpu in gpu_inventory()}
    missing = set(body.gpu_uuids) - available
    if missing:
        raise HTTPException(409, f"GPU UUIDs are unavailable: {sorted(missing)}")
    artifact_path = None
    workspace_path = None
    if any((body.artifact_storage_pool_id, body.artifact_relative_path, body.workspace_storage_pool_id)):
        if not all((body.artifact_storage_pool_id, body.artifact_relative_path, body.workspace_storage_pool_id)):
            raise HTTPException(422, "managed replica requires artifact and workspace storage references")
        artifact_pool = load_pool(body.artifact_storage_pool_id)
        workspace_pool = load_pool(body.workspace_storage_pool_id)
        if artifact_pool["purpose"] != "model" or artifact_pool["runtime_access"] not in {"ro", "rw"}:
            raise HTTPException(403, "artifact storage pool is not runtime-readable")
        if workspace_pool["purpose"] != "workspace" or workspace_pool["runtime_access"] != "rw":
            raise HTTPException(403, "workspace storage pool is not runtime-writable")
        artifact_path = resolve_in_pool(artifact_pool, body.artifact_relative_path)
        workspace_root = resolve_in_pool(workspace_pool, ".")
        workspace_path = resolve_in_pool(workspace_pool, body.replica_id, must_exist=False)
        usage = shutil.disk_usage(workspace_root)
        if usage.free < int(workspace_pool.get("min_free_gib", 0) * 1024**3):
            raise HTTPException(409, "workspace storage pool has insufficient free space")
        workspace_observation = pool_observation(workspace_pool)
        quota_bytes = int(workspace_pool["quota_gib"] * 1024**3) if workspace_pool.get("quota_gib") else None
        if quota_bytes is not None and workspace_observation["used_bytes"] >= quota_bytes:
            raise HTTPException(409, "workspace storage pool quota is exhausted")
        workspace_path.mkdir(parents=False, exist_ok=True)
        # The runtime image may use a non-root service account and its uid/gid is
        # intentionally not coupled to the host.  This directory is dedicated to
        # one replica, so make the bind-mounted workspace writable before Docker
        # starts while keeping the storage-pool root itself protected.
        os.chmod(workspace_path, 0o777)
    name = container_name(body.replica_id)
    existing = observe(name)
    if existing["state"] != "missing":
        return existing
    auth_dir = STATE_DIR / "auth" / body.replica_id
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file = auth_dir / "authorized_keys"
    auth_file.write_text(body.gateway_public_key.strip() + "\n", encoding="utf-8")
    try:
        # This is a public key, not a secret.  Keep it non-writable by group or
        # others, but world-readable so Docker user-namespace remapping can
        # expose the bind-mounted file to root inside the runtime container.
        os.chmod(auth_file, 0o644)
    except OSError:
        pass
    args = ["docker", "run", "-d", "--name", name, "--restart", "no",
            "--label", f"model-serving.replica={body.replica_id}", "-p", "22",
            "-v", f"{auth_file.resolve()}:/model-serving-auth/authorized_keys:ro"]
    if artifact_path and workspace_path:
        args.extend(["-v", f"{artifact_path}:/models/current:ro", "-v", f"{workspace_path}:/workspace:rw"])
    else:
        args.extend(["-v", f"model-serving-{body.replica_id}:/workspace",
                     "-v", "model-serving-hf-cache:/workspace/.cache/huggingface"])
    for gpu_uuid in body.gpu_uuids:
        args.extend(["--device", f"nvidia.com/gpu={gpu_uuid}"])
    if body.cpu_cores:
        args.extend(["--cpus", str(body.cpu_cores)])
    if body.memory_gib:
        args.extend(["--memory", f"{body.memory_gib}g"])
    for key, value in {
        "MODEL_REPOSITORY": "/models/current" if artifact_path else body.model_repository,
        "MODEL_REVISION": body.model_revision,
        "SERVED_MODEL_NAME": body.served_model_id,
        "VLLM_ARGS_JSON": json.dumps(body.runtime_args, separators=(",", ":")),
        # Docker user namespaces can present the read-only authorized_keys bind
        # mount as nobody:nogroup inside the container.  The file is generated
        # by this root Agent and cannot be modified by the runtime, so ownership
        # validation adds no protection here and would reject a valid key.
        "MODEL_SERVING_SSH_STRICT_MODES": "no",
    }.items():
        args.extend(["-e", f"{key}={value}"])
    args.append(body.image)
    command(args, timeout=600)
    return observe(name)


@app.get("/v1/replicas/{runtime_id}", dependencies=[Depends(auth)])
def get_replica(runtime_id: str):
    if not runtime_id.startswith("model-serving-"):
        raise HTTPException(422, "invalid runtime id")
    return observe(runtime_id)


@app.delete("/v1/replicas/{runtime_id}", dependencies=[Depends(auth)])
def delete_replica(runtime_id: str):
    if not runtime_id.startswith("model-serving-"):
        raise HTTPException(422, "invalid runtime id")
    command(["docker", "rm", "-f", runtime_id], timeout=60, allow_failure=True)
    replica_id = runtime_id.removeprefix("model-serving-")
    auth_dir = (STATE_DIR / "auth" / replica_id).resolve()
    auth_root = (STATE_DIR / "auth").resolve()
    if auth_dir.parent == auth_root and auth_dir.exists():
        shutil.rmtree(auth_dir)
    return {"runtime_id": runtime_id, "state": "removed"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("NODE_AGENT_PORT", "9010")))
