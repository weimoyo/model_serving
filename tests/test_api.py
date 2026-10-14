from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from model_serving.api import create_app
from model_serving.config import Settings


class FakeRuntime:
    def __init__(self, backend_url: str):
        self.backend_url, self.created, self.deleted = backend_url, 0, []
        self.runtimes: dict[str, dict] = {}
        self.pools: dict[str, dict] = {}
        self.last_deployment = None

    async def inventory(self, node):
        return {"hostname": "node-1", "architecture": "aarch64", "os": "Linux", "memory_gib": 128, "runtime": "vllm",
                "accelerators": [{"kind": "GPU", "uuid": "GPU-real-uuid", "model": "GB10", "state": "available"}]}

    async def upsert_storage_pool(self, node, value):
        observed = {**value, "host_path": "/srv/" + value["id"], "state": "READY",
                    "capacity_bytes": 100_000_000, "free_bytes": 90_000_000}
        self.pools[value["id"]] = observed
        return observed

    async def revoke_storage_pool(self, node, pool_id):
        self.pools.pop(pool_id, None)

    async def start_scan(self, node, value):
        return {"id": "agent-scan-1", "state": "COMPLETED", "progress": {"models_found": 1}, "result": [{
            "relative_path": "tiny-model", "format": "safetensors", "runtime_compatible": True,
            "size_bytes": 1234, "checksum": "manifest-sha256:abc", "metadata": {"architectures": ["Tiny"]},
        }]}

    async def get_scan(self, node, job_id):
        return await self.start_scan(node, {})

    async def start_download(self, node, value):
        return {"id": "agent-download-1", "state": "QUEUED", "progress": {}}

    async def get_download(self, node, job_id):
        return {"id": job_id, "state": "COMPLETED", "progress": {"phase": "published"}, "result": {
            "relative_path": "downloaded", "repository": "org/tiny", "revision": "deadbeef01",
            "format": "safetensors", "runtime_compatible": True, "size_bytes": 1234,
            "checksum": "manifest-sha256:def", "metadata": {},
        }}

    async def create(self, node, deployment, replica_id, gpu_uuids):
        self.last_deployment = deployment
        self.created += 1
        runtime_id = f"runtime-{self.created}"
        value = {"runtime_id": runtime_id, "state": "running", "ssh_port": 22000 + self.created, "llm_base_url": self.backend_url}
        self.runtimes[runtime_id] = value
        return value

    async def get(self, node, runtime_id):
        return dict(self.runtimes[runtime_id])

    async def delete(self, node, runtime_id):
        self.deleted.append(runtime_id)
        self.runtimes[runtime_id]["state"] = "removed"

    async def endpoint(self, replica_id, node, runtime):
        return runtime["llm_base_url"]

    async def close(self, replica_id):
        return None

    async def close_all(self):
        return None


def start_backend():
    backend = FastAPI()

    @backend.get("/health")
    def health(): return {"status": "ok"}

    @backend.get("/v1/models")
    def models(): return {"object": "list", "data": [{"id": "backend-model"}]}

    @backend.post("/v1/chat/completions")
    async def chat(body: dict):
        assert body["model"] == "backend-model"
        if body.get("stream"):
            async def chunks():
                yield 'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hello"}}]}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")
        return {"id": "answer", "object": "chat.completion", "model": body["model"], "choices": [{"message": {"role": "assistant", "content": "hello"}}]}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(backend, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline: time.sleep(0.01)
    return server, thread, f"http://127.0.0.1:{port}"


def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "db.sqlite3", "key", tmp_path / "gateway", tmp_path / "known_hosts", run_reconciler=False)


def test_loopback_dashboard_establishes_http_only_session(tmp_path: Path):
    runtime = FakeRuntime("http://127.0.0.1:1")
    with TestClient(create_app(settings(tmp_path), runtime=runtime)) as client:
        dashboard = client.get("/")
        cookie = dashboard.headers.get("set-cookie", "")
        assert dashboard.status_code == 200
        assert "model_serving_local_session=" in cookie
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert client.get("/api/nodes").status_code == 200


def test_one_time_provider_enrollment_registers_resources_without_leaking_secret(tmp_path: Path):
    runtime = FakeRuntime("http://127.0.0.1:1")
    headers = {"Authorization": "Bearer key"}
    app = create_app(settings(tmp_path), runtime=runtime)
    with TestClient(app) as client:
        created = client.post(
            "/api/enrollment-tokens", headers=headers, json={"ttl_minutes": 10, "uses": 1}
        )
        assert created.status_code == 201
        token = created.json()["token"]
        payload = {
            "enrollment_token": token,
            "agent_key": "agent-secret-" + "x" * 32,
            "node": {
                "id": "provider-1", "name": "Provider One", "host": "10.0.0.10",
                "agent_url": "http://10.0.0.10:9010", "agent_key_env": "IGNORED_KEY",
                "platform": "DGX Spark", "network_zone": "lab",
            },
            "storage_pools": [
                {"id": "provider-1-models", "host_path": "/models", "container_path": "/models",
                 "purpose": "model", "agent_access": "rw", "runtime_access": "ro", "quota_gib": 20},
                {"id": "provider-1-workspace", "host_path": "/workspace", "container_path": "/workspace",
                 "purpose": "workspace", "agent_access": "rw", "runtime_access": "rw", "quota_gib": 10},
            ],
            "runtime_profiles": [{
                "id": "vllm-provider-1", "name": "vLLM Provider One", "runtime": "vllm",
                "image": "sha256:" + "a" * 64, "architectures": ["aarch64"],
                "accelerator_kinds": ["GPU"], "allowed_args": ["max_model_len"],
            }],
            "scan_existing_models": True,
        }
        enrolled = client.post("/api/enroll", json=payload)
        assert enrolled.status_code == 201, enrolled.text
        body = enrolled.json()
        assert body["status"] == "enrolled" and body["node"]["state"] == "ONLINE"
        assert body["node"]["agent_key_env"].startswith("NODE_PROVIDER_1_")
        assert body["node"]["agent_key_env"].endswith("_AGENT_KEY")
        assert len(body["storage_pools"]) == 2
        assert body["runtime_profiles"][0]["id"] == "vllm-provider-1"
        assert body["initial_scan"]["state"] == "COMPLETED"
        assert "agent-secret" not in enrolled.text and token not in enrolled.text
        secret_file = tmp_path / "node-secrets" / f"{body['node']['agent_key_env']}.token"
        assert secret_file.read_text(encoding="utf-8") == payload["agent_key"]
        assert client.post("/api/enroll", json=payload).status_code == 401
        listed = client.get("/api/enrollment-tokens", headers=headers).json()
        assert listed[0]["uses_remaining"] == 0 and "token" not in listed[0]


def test_standalone_lifecycle_stream_failure_and_replacement(tmp_path: Path, monkeypatch):
    server, thread, backend_url = start_backend()
    runtime = FakeRuntime(backend_url)
    monkeypatch.setenv("NODE_1_KEY", "node-secret")
    headers = {"Authorization": "Bearer key"}
    try:
        with TestClient(create_app(settings(tmp_path), runtime=runtime)) as client:
            assert client.get("/v1/models").status_code == 401
            node = client.post("/api/nodes", headers=headers, json={"id": "node-1", "name": "DGX 1", "host": "10.0.0.1", "agent_url": "http://10.0.0.1:9010", "agent_key_env": "NODE_1_KEY", "platform": "DGX Spark"})
            assert node.status_code == 201 and node.json()["state"] == "ONLINE"
            assert client.post("/api/models", headers=headers, json={"id": "tiny", "display_name": "Tiny", "repository": "org/tiny", "revision": "deadbeef01", "capabilities": ["chat", "streaming"]}).status_code == 201
            deployment = client.post("/api/deployments", headers=headers, json={"id": "tiny-prod", "name": "Tiny", "model_id": "tiny", "served_model_id": "backend-model", "runtime_image": "local/vllm@sha256:abc", "desired_replicas": 1})
            assert deployment.status_code == 201 and len(deployment.json()["replicas"]) == 1
            client.post("/api/reconcile", headers=headers)
            catalog = client.get("/api/catalog", headers=headers).json()
            replica = catalog["models"][0]["deployments"][0]["replicas"][0]
            assert replica["schedulable"] is True
            assert replica["assigned_gpu_uuids"] == ["GPU-real-uuid"]
            assert replica["device"]["inventory"]["architecture"] == "aarch64"
            assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "tiny"
            response = client.post("/v1/chat/completions", headers=headers, json={"model": "tiny", "messages": []})
            assert response.status_code == 200 and response.json()["model"] == "tiny"
            with client.stream("POST", "/v1/chat/completions", headers=headers, json={"model": "tiny", "messages": [], "stream": True}) as response:
                content = "".join(response.iter_text())
                assert response.status_code == 200 and "data: [DONE]" in content

            runtime.runtimes[replica["runtime_id"]]["state"] = "exited"
            client.post("/api/reconcile", headers=headers)
            assert client.get("/v1/models", headers=headers).json()["data"] == []
            assert client.post("/v1/chat/completions", headers=headers, json={"model": "tiny", "messages": []}).status_code == 503
            assert runtime.created == 2 and runtime.deleted == ["runtime-1"]
            client.post("/api/reconcile", headers=headers)
            assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "tiny"
            deleted = client.delete("/api/deployments/tiny-prod", headers=headers)
            assert deleted.status_code == 200 and deleted.json()["state"] == "DELETED"
            assert runtime.deleted == ["runtime-1", "runtime-2"]
            archived = client.post(
                "/api/maintenance/archive-stale?terminal_replica_hours=0&deleted_deployment_hours=0&completed_job_hours=0",
                headers=headers,
            )
            assert archived.status_code == 200
            assert archived.json()["archived"]["deployments"] == 1
            assert archived.json()["archived"]["replicas"] == 2
            assert client.get("/api/deployments", headers=headers).json() == []
            history = client.get("/api/deployments?include_archived=true", headers=headers).json()
            assert len(history) == 1 and len(history[0]["replicas"]) == 2
            assert client.post("/api/nodes/node-1/archive", headers=headers).status_code == 409
    finally:
        server.should_exit = True; thread.join(timeout=5)


def test_validation(tmp_path: Path):
    runtime = FakeRuntime("http://127.0.0.1:1")
    with TestClient(create_app(settings(tmp_path), runtime=runtime)) as client:
        headers = {"X-API-Key": "key"}
        bad = client.post("/api/models", headers=headers, json={"id": "m", "display_name": "M", "repository": "r", "revision": "latest"})
        assert bad.status_code == 422
        invalid_scale = client.post("/api/deployments", headers=headers, json={"id": "d", "name": "D", "model_id": "m", "served_model_id": "m", "runtime_image": "x", "desired_replicas": 2})
        assert invalid_scale.status_code == 422


def test_managed_storage_scan_download_artifact_profile_and_deployment(tmp_path: Path, monkeypatch):
    server, thread, backend_url = start_backend()
    runtime = FakeRuntime(backend_url)
    monkeypatch.setenv("NODE_1_KEY", "node-secret")
    headers = {"Authorization": "Bearer key"}
    try:
        with TestClient(create_app(settings(tmp_path), runtime=runtime)) as client:
            client.post("/api/nodes", headers=headers, json={"id": "node-1", "name": "DGX 1", "host": "10.0.0.1",
                "agent_url": "http://10.0.0.1:9010", "agent_key_env": "NODE_1_KEY"})
            for pool in (
                {"id": "models", "host_path": "/models", "container_path": "/models", "purpose": "model",
                 "agent_access": "rw", "runtime_access": "ro", "quota_gib": 10},
                {"id": "workspace", "host_path": "/workspace", "container_path": "/workspace", "purpose": "workspace",
                 "agent_access": "rw", "runtime_access": "rw", "quota_gib": 10},
            ):
                response = client.put(f"/api/nodes/node-1/storage-pools/{pool['id']}", headers=headers, json=pool)
                assert response.status_code == 200, response.text

            scan = client.post("/api/nodes/node-1/scans", headers=headers,
                               json={"storage_pool_id": "models", "relative_path": "."})
            assert scan.status_code == 202
            observed = client.get(f"/api/scans/{scan.json()['id']}", headers=headers)
            assert observed.status_code == 200 and observed.json()["state"] == "COMPLETED"
            artifact_id = observed.json()["result"][0]["artifact_id"]

            download = client.post("/api/nodes/node-1/downloads", headers=headers, json={
                "storage_pool_id": "models", "repository": "org/tiny", "revision": "deadbeef01"})
            assert download.status_code == 202
            downloaded = client.get(f"/api/downloads/{download.json()['id']}", headers=headers)
            assert downloaded.status_code == 200 and downloaded.json()["artifact_id"]

            assert client.post("/api/models", headers=headers, json={"id": "tiny", "display_name": "Tiny",
                "repository": "org/tiny", "revision": "deadbeef01"}).status_code == 201
            assert client.post(f"/api/artifacts/{artifact_id}/models/tiny", headers=headers).status_code == 200
            profile = {"id": "vllm-spark", "name": "vLLM Spark", "runtime": "vllm",
                "image": "local/vllm@sha256:" + "a" * 64, "architectures": ["aarch64"],
                "accelerator_kinds": ["GPU"], "allowed_args": ["max_model_len"]}
            assert client.put("/api/runtime-profiles/vllm-spark", headers=headers, json=profile).status_code == 200
            forbidden = client.post("/api/deployments", headers=headers, json={"id": "bad", "name": "Bad",
                "model_id": "tiny", "served_model_id": "backend-model", "artifact_id": artifact_id,
                "runtime_profile_id": "vllm-spark", "workspace_storage_pool_id": "workspace",
                "runtime_args": {"privileged": True}})
            assert forbidden.status_code == 422
            deployed = client.post("/api/deployments", headers=headers, json={"id": "managed", "name": "Managed",
                "model_id": "tiny", "served_model_id": "backend-model", "artifact_id": artifact_id,
                "runtime_profile_id": "vllm-spark", "workspace_storage_pool_id": "workspace",
                "runtime_args": {"max_model_len": 2048}})
            assert deployed.status_code == 201 and deployed.json()["replicas"][0]["state"] == "STARTING"
            assert runtime.last_deployment["artifact_location"]["relative_path"] == "tiny-model"
            client.post("/api/reconcile", headers=headers)
            assert client.get("/v1/models", headers=headers).json()["data"][0]["id"] == "tiny"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
