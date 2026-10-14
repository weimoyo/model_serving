from fastapi.testclient import TestClient
import json
import os

import node_agent


def test_node_agent_auth_inventory_and_safe_docker_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_AGENT_API_KEY", "node-key")
    monkeypatch.setattr(node_agent, "STATE_DIR", tmp_path)
    monkeypatch.setattr(node_agent, "gpu_inventory", lambda: [{"kind": "GPU", "uuid": "GPU-1", "model": "GB10", "state": "available"}])
    calls = []
    running = {"value": False}

    def fake_command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "version"]:
            return "29.2.1"
        if args[:2] == ["docker", "run"]:
            running["value"] = True
            return "container-id"
        return ""

    def fake_observe(name):
        return {"runtime_id": name, "state": "running" if running["value"] else "missing", "ssh_port": 22022 if running["value"] else None}

    monkeypatch.setattr(node_agent, "command", fake_command)
    monkeypatch.setattr(node_agent, "observe", fake_observe)
    with TestClient(node_agent.app) as client:
        assert client.get("/v1/inventory").status_code == 401
        headers = {"Authorization": "Bearer node-key"}
        assert client.get("/v1/inventory", headers=headers).json()["docker_version"] == "29.2.1"
        response = client.post("/v1/replicas", headers=headers, json={
            "replica_id": "replica-1", "image": "registry/runtime@sha256:abc",
            "model_repository": "org/model", "model_revision": "deadbeef",
            "served_model_id": "served", "runtime_args": {"max_model_len": 2048},
            "gpu_uuids": ["GPU-1"], "gateway_public_key": "ssh-ed25519 " + "A" * 50,
        })
        assert response.status_code == 201, response.text
    docker_run = next(args for args in calls if args[:2] == ["docker", "run"])
    assert ["--device", "nvidia.com/gpu=GPU-1"] == docker_run[docker_run.index("--device"):docker_run.index("--device") + 2]
    assert docker_run[-1] == "registry/runtime@sha256:abc"
    assert "model-serving-hf-cache:/workspace/.cache/huggingface" in docker_run
    assert "MODEL_SERVING_SSH_STRICT_MODES=no" in docker_run
    assert not any("shell" in str(item).lower() for item in docker_run)
    auth_file = tmp_path / "auth" / "replica-1" / "authorized_keys"
    assert auth_file.is_file()
    if os.name != "nt":
        assert (auth_file.stat().st_mode & 0o777) == 0o644


def test_gpu_inventory_keeps_unified_memory_gpu(monkeypatch):
    monkeypatch.setattr(
        node_agent,
        "command",
        lambda *_args, **_kwargs: "GPU-GB10, NVIDIA GB10, [N/A], [N/A], N/A",
    )
    assert node_agent.gpu_inventory() == [{
        "kind": "GPU", "uuid": "GPU-GB10", "model": "NVIDIA GB10", "state": "available",
        "memory_gib": None, "memory_free_gib": None, "utilization_percent": None,
    }]


def test_observe_treats_docker_empty_array_as_missing(monkeypatch):
    monkeypatch.setattr(node_agent, "command", lambda *_args, **_kwargs: "[]")
    assert node_agent.observe("model-serving-not-created") == {
        "runtime_id": "model-serving-not-created", "state": "missing", "ssh_port": None,
    }


def test_storage_pool_boundary_scan_and_managed_read_only_mount(tmp_path, monkeypatch):
    state, models, work = tmp_path / "state", tmp_path / "models", tmp_path / "work"
    model_dir = models / "tiny"
    model_dir.mkdir(parents=True)
    work.mkdir()
    (model_dir / "config.json").write_text(json.dumps({"architectures": ["TinyForCausalLM"], "model_type": "tiny"}))
    (model_dir / "model.safetensors").write_bytes(b"weights")
    monkeypatch.setenv("NODE_AGENT_API_KEY", "node-key")
    monkeypatch.setattr(node_agent, "STATE_DIR", state)
    monkeypatch.setattr(node_agent, "gpu_inventory", lambda: [{"kind": "GPU", "uuid": "GPU-1", "model": "GB10", "state": "available"}])
    calls, running = [], {"value": False}

    def fake_command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            running["value"] = True
        return ""

    monkeypatch.setattr(node_agent, "command", fake_command)
    monkeypatch.setattr(node_agent, "observe", lambda name: {"runtime_id": name,
        "state": "running" if running["value"] else "missing", "ssh_port": 22022 if running["value"] else None})
    headers = {"Authorization": "Bearer node-key"}
    with TestClient(node_agent.app) as client:
        for pool in (
            {"id": "models", "host_path": str(models), "container_path": "/models", "purpose": "model",
             "agent_access": "rw", "runtime_access": "ro", "min_free_gib": 0},
            {"id": "work", "host_path": str(work), "container_path": "/workspace", "purpose": "workspace",
             "agent_access": "rw", "runtime_access": "rw", "min_free_gib": 0},
        ):
            assert client.put(f"/v1/storage-pools/{pool['id']}", headers=headers, json=pool).status_code == 200
        assert client.post("/v1/scans", headers=headers,
                           json={"storage_pool_id": "models", "relative_path": "../"}).status_code == 422
        scan_id = "scan-test"
        node_agent.update_job("scan", scan_id, id=scan_id, state="QUEUED", progress={}, result=[])
        node_agent.scan_worker(scan_id, node_agent.load_pool("models"), ".")
        result = client.get(f"/v1/scans/{scan_id}", headers=headers).json()
        assert result["state"] == "COMPLETED" and result["result"][0]["relative_path"] == "tiny"
        created = client.post("/v1/replicas", headers=headers, json={
            "replica_id": "managed-1", "image": "runtime@sha256:abc", "model_repository": "org/model",
            "model_revision": "deadbeef", "served_model_id": "served", "gpu_uuids": ["GPU-1"],
            "gateway_public_key": "ssh-ed25519 " + "A" * 50, "artifact_storage_pool_id": "models",
            "artifact_relative_path": "tiny", "workspace_storage_pool_id": "work",
        })
        assert created.status_code == 201, created.text
    docker_run = next(args for args in calls if args[:2] == ["docker", "run"])
    assert f"{model_dir.resolve()}:/models/current:ro" in docker_run
    assert f"{(work / 'managed-1').resolve()}:/workspace:rw" in docker_run
    assert ((work / "managed-1").stat().st_mode & 0o777) == 0o777
    assert "/var/run/docker.sock" not in " ".join(docker_run)
