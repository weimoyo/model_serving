"""Create one local mock-backed Model and Deployment."""
import os

import httpx


base_url = os.getenv("MODEL_SERVING_URL", "http://127.0.0.1:8080").rstrip("/")
key = os.environ["MODEL_SERVING_API_KEY"]
headers = {"Authorization": f"Bearer {key}"}

with httpx.Client(timeout=15, headers=headers) as client:
    node = client.post(base_url + "/api/nodes", json={
        "id": "demo-node", "name": "Demo Node", "host": "127.0.0.1",
        "agent_url": os.getenv("DEMO_NODE_AGENT_URL", "http://127.0.0.1:9010"),
        "agent_key_env": "DEMO_NODE_AGENT_KEY", "platform": "simulation",
    })
    node.raise_for_status()
    model = client.post(base_url + "/api/models", json={
        "id": "demo-model", "display_name": "Demo Chat Model", "repository": "local/mock",
        "revision": "0000000000000000000000000000000000000000", "task": "chat",
        "capabilities": ["chat", "streaming"],
    })
    model.raise_for_status()
    deployment = client.post(base_url + "/api/deployments", json={
        "id": "demo-deployment", "name": "Demo Deployment", "model_id": "demo-model",
        "served_model_id": "demo-backend-id", "desired_replicas": 1,
        "runtime_image": "local/llm-vllm-ssh@sha256:simulation",
    })
    deployment.raise_for_status()
    client.post(base_url + "/api/reconcile").raise_for_status()
    print(client.get(base_url + "/api/catalog").json())
    print(client.post(base_url + "/v1/chat/completions", json={
        "model": "demo-model", "messages": [{"role": "user", "content": "hello"}], "stream": False,
    }).json())
