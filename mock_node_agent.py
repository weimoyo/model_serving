"""Development-only node-agent double; it never invokes Docker."""
from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Mock Model Serving Node Agent")
runtimes: dict[str, dict] = {}


@app.get("/v1/inventory")
def inventory():
    return {"hostname": "mock-node", "architecture": "aarch64", "os": "Linux", "memory_gib": 128,
            "docker_version": "mock", "accelerators": [{"kind": "GPU", "uuid": "GPU-MOCK-0001", "model": "GB10-simulated", "state": "available"}]}


@app.post("/v1/replicas", status_code=201)
def create(body: dict):
    runtime_id = f"mock-{uuid.uuid4().hex[:8]}"
    value = {"runtime_id": runtime_id, "state": "running", "ssh_port": 22022,
             "llm_base_url": os.getenv("MOCK_LLM_URL", "http://127.0.0.1:8000")}
    runtimes[runtime_id] = value
    return value


@app.get("/v1/replicas/{runtime_id}")
def get(runtime_id: str):
    if runtime_id not in runtimes:
        raise HTTPException(404, "runtime not found")
    return runtimes[runtime_id]


@app.delete("/v1/replicas/{runtime_id}")
def delete(runtime_id: str):
    value = get(runtime_id)
    value["state"] = "removed"
    return value
