from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class NodeRuntimeError(RuntimeError):
    pass


def ensure_gateway_keypair(private_key: Path) -> str:
    public_key = Path(str(private_key) + ".pub")
    if not private_key.exists() or not public_key.exists():
        private_key.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "model-serving-gateway", "-f", str(private_key)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            raise NodeRuntimeError(f"could not generate gateway SSH key: {result.stderr[:300]}")
    return public_key.read_text(encoding="utf-8").strip()


@dataclass
class Tunnel:
    process: asyncio.subprocess.Process
    local_port: int
    target: str


class SshTunnelManager:
    def __init__(self, private_key: Path, known_hosts: Path):
        self.private_key = private_key
        self.known_hosts = known_hosts
        self._tunnels: dict[str, Tunnel] = {}

    async def ensure(self, replica_id: str, host: str, ssh_port: int, *, user: str = "root", remote_port: int = 8000) -> str:
        target = f"{user}@{host}:{ssh_port}:{remote_port}"
        current = self._tunnels.get(replica_id)
        if current and current.target == target and current.process.returncode is None:
            return f"http://127.0.0.1:{current.local_port}"
        await self.close(replica_id)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            local_port = int(sock.getsockname()[1])
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        args = [
            "ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-i", str(self.private_key), "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            "-p", str(ssh_port), f"{user}@{host}",
        ]
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.wait(), timeout=0.4)
        except TimeoutError:
            pass
        if process.returncode is not None:
            error = (await process.stderr.read()).decode(errors="replace")[:500] if process.stderr else ""
            raise NodeRuntimeError(f"SSH tunnel exited: {error}")
        self._tunnels[replica_id] = Tunnel(process, local_port, target)
        return f"http://127.0.0.1:{local_port}"

    async def close(self, replica_id: str) -> None:
        tunnel = self._tunnels.pop(replica_id, None)
        if tunnel and tunnel.process.returncode is None:
            tunnel.process.terminate()
            try:
                await asyncio.wait_for(tunnel.process.wait(), 3)
            except TimeoutError:
                tunnel.process.kill()
                await tunnel.process.wait()

    async def close_all(self) -> None:
        for replica_id in list(self._tunnels):
            await self.close(replica_id)


class NodeRuntimeManager:
    """Calls this project's node agents and owns gateway SSH tunnels."""

    def __init__(self, private_key: Path, known_hosts: Path, *, timeout: float = 5,
                 node_secret_dir: Path | None = None):
        self.public_key = ensure_gateway_keypair(private_key)
        self.tunnels = SshTunnelManager(private_key, known_hosts)
        self.timeout = timeout
        self.node_secret_dir = node_secret_dir

    def _headers(self, node: dict[str, Any]) -> dict[str, str]:
        key = os.getenv(node["agent_key_env"])
        if not key and self.node_secret_dir:
            secret_file = self.node_secret_dir / f"{node['agent_key_env']}.token"
            try:
                key = secret_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
        if not key:
            raise NodeRuntimeError(
                f"node key {node['agent_key_env']} is not configured in the environment or node secret store"
            )
        return {"Authorization": f"Bearer {key}"}

    async def _request(self, node: dict[str, Any], method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, node["agent_url"].rstrip("/") + path, headers=self._headers(node), **kwargs)
        if response.status_code >= 400:
            raise NodeRuntimeError(f"node agent {method} {path} returned {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    async def inventory(self, node: dict[str, Any]) -> dict[str, Any]:
        return await self._request(node, "GET", "/v1/inventory")

    async def upsert_storage_pool(self, node: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        return await self._request(node, "PUT", f"/v1/storage-pools/{value['id']}", json=value)

    async def revoke_storage_pool(self, node: dict[str, Any], pool_id: str) -> None:
        await self._request(node, "DELETE", f"/v1/storage-pools/{pool_id}")

    async def start_scan(self, node: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        return await self._request(node, "POST", "/v1/scans", json=value)

    async def get_scan(self, node: dict[str, Any], job_id: str) -> dict[str, Any]:
        return await self._request(node, "GET", f"/v1/scans/{job_id}")

    async def cancel_scan(self, node: dict[str, Any], job_id: str) -> dict[str, Any]:
        return await self._request(node, "DELETE", f"/v1/scans/{job_id}")

    async def start_download(self, node: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        return await self._request(node, "POST", "/v1/downloads", json=value)

    async def get_download(self, node: dict[str, Any], job_id: str) -> dict[str, Any]:
        return await self._request(node, "GET", f"/v1/downloads/{job_id}")

    async def verify_artifact(self, node: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        return await self._request(node, "POST", "/v1/artifacts/verify", json=value)

    async def create(self, node: dict[str, Any], deployment: dict[str, Any], replica_id: str, gpu_uuids: list[str]) -> dict[str, Any]:
        model = deployment["model"]
        payload = {
            "replica_id": replica_id, "image": deployment["runtime_image"],
            "runtime": deployment["runtime"],
            "model_repository": model["repository"], "model_revision": model["revision"],
            "served_model_id": deployment["served_model_id"], "runtime_args": deployment["runtime_args"],
            "gpu_uuids": gpu_uuids, "cpu_cores": deployment.get("cpu_cores_per_replica"),
            "memory_gib": deployment.get("memory_gib_per_replica"), "gateway_public_key": self.public_key,
        }
        if deployment.get("artifact_location"):
            payload.update({
                "artifact_storage_pool_id": deployment["artifact_location"]["storage_pool_id"],
                "artifact_relative_path": deployment["artifact_location"]["relative_path"],
                "workspace_storage_pool_id": deployment["workspace_storage_pool_id"],
            })
        return await self._request(node, "POST", "/v1/replicas", json=payload)

    async def get(self, node: dict[str, Any], runtime_id: str) -> dict[str, Any]:
        return await self._request(node, "GET", f"/v1/replicas/{runtime_id}")

    async def delete(self, node: dict[str, Any], runtime_id: str) -> None:
        await self._request(node, "DELETE", f"/v1/replicas/{runtime_id}")

    async def endpoint(self, replica_id: str, node: dict[str, Any], runtime: dict[str, Any]) -> str:
        if runtime.get("llm_base_url"):
            return str(runtime["llm_base_url"]).rstrip("/")
        port = runtime.get("ssh_port")
        if not port:
            raise NodeRuntimeError("running runtime has no SSH port")
        return await self.tunnels.ensure(replica_id, node["host"], int(port),
                                         user=str(runtime.get("ssh_user") or "root"),
                                         remote_port=int(runtime.get("runtime_port") or 8000))

    async def close(self, replica_id: str) -> None:
        await self.tunnels.close(replica_id)

    async def close_all(self) -> None:
        await self.tunnels.close_all()
