from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from .database import utcnow
from .node_runtime import NodeRuntimeError, NodeRuntimeManager
from .repository import Repository


TERMINAL = {"FAILED", "RELEASED"}


class Reconciler:
    def __init__(self, repo: Repository, runtime: NodeRuntimeManager, *, probe_timeout: float = 5):
        self.repo, self.runtime, self.probe_timeout = repo, runtime, probe_timeout
        self._lock = asyncio.Lock()

    async def probe_node(self, node_id: str) -> dict[str, Any]:
        node = self.repo.get_node(node_id)
        if not node:
            raise KeyError(node_id)
        try:
            inventory = await self.runtime.inventory(node)
        except Exception as exc:
            self.repo.update_node_observation(node_id, state="OFFLINE", reason=f"{type(exc).__name__}: {str(exc)[:300]}")
            return self.repo.get_node(node_id)  # type: ignore[return-value]
        self.repo.update_node_observation(node_id, state="ONLINE", inventory=inventory)
        return self.repo.get_node(node_id)  # type: ignore[return-value]

    async def reconcile_all(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            for node in self.repo.list_nodes():
                await self.probe_node(node["id"])
            for deployment in self.repo.list_deployments(active_only=True):
                try:
                    await self._reconcile(deployment["id"], probe_nodes=False)
                except Exception:
                    continue

    async def reconcile(self, deployment_id: str, *, probe_nodes: bool = True) -> dict[str, Any] | None:
        # API-triggered and background reconciliation share one critical
        # section. Without this, both can observe zero live replicas and each
        # create one even when desired_replicas=1.
        async with self._lock:
            return await self._reconcile(deployment_id, probe_nodes=probe_nodes)

    async def _reconcile(self, deployment_id: str, *, probe_nodes: bool = True) -> dict[str, Any] | None:
        if probe_nodes:
            for node in self.repo.list_nodes():
                await self.probe_node(node["id"])
        deployment = self.repo.deployment_context(deployment_id)
        if not deployment:
            return None
        if deployment["state"] == "DELETING":
            await self._delete(deployment)
            return self.repo.get_deployment(deployment_id, include_replicas=True)
        for replica in deployment["replicas"]:
            if replica["state"] not in TERMINAL:
                await self._refresh(deployment, replica)
        deployment = self.repo.deployment_context(deployment_id)
        assert deployment
        live = [r for r in deployment["replicas"] if r["state"] not in TERMINAL]
        while len(live) < deployment["desired_replicas"]:
            live.append(await self._create(deployment))
        return self.repo.get_deployment(deployment_id, include_replicas=True)

    def _placement(self, deployment: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
        count, runtime = deployment["gpu_count_per_replica"], deployment["runtime"]
        profile = deployment.get("runtime_profile")
        for node in self.repo.list_nodes():
            if node["state"] != "ONLINE":
                continue
            advertised_runtime = (node.get("inventory") or {}).get("runtime")
            if advertised_runtime and advertised_runtime != runtime:
                continue
            if profile:
                architecture = str((node.get("inventory") or {}).get("architecture") or "")
                if architecture not in profile["architectures"]:
                    continue
                present_kinds = {str(item.get("kind") or "").lower() for item in (node.get("inventory") or {}).get("accelerators", [])}
                required_kinds = {str(item).lower() for item in profile["accelerator_kinds"]}
                if required_kinds and not required_kinds.issubset(present_kinds):
                    continue
            location = None
            if deployment.get("artifact_id"):
                location = self.repo.get_artifact_location(deployment["artifact_id"], node["id"])
                workspace = self.repo.get_storage_pool(deployment["workspace_storage_pool_id"])
                artifact_pool = self.repo.get_storage_pool(location["storage_pool_id"]) if location else None
                if (not location or not workspace or workspace["node_id"] != node["id"] or workspace["state"] != "READY"
                        or workspace["purpose"] != "workspace" or workspace["runtime_access"] != "rw"
                        or not artifact_pool or artifact_pool["state"] != "READY" or artifact_pool["runtime_access"] not in {"ro", "rw"}):
                    continue
            used = self.repo.assigned_gpus(node["id"])
            candidates = []
            for gpu in (node.get("inventory") or {}).get("accelerators", []):
                gpu_uuid = gpu.get("uuid")
                healthy = str(gpu.get("state", "available")).lower() in {"available", "healthy", "online"}
                if gpu_uuid and healthy and gpu_uuid not in used:
                    candidates.append(gpu_uuid)
            if len(candidates) >= count:
                return node, candidates[:count], location
        if deployment.get("artifact_id"):
            raise NodeRuntimeError("no online compatible node has a READY artifact location, writable workspace and free accelerator")
        raise NodeRuntimeError(f"no online node has {count} unallocated GPU UUID(s)")

    async def _create(self, deployment: dict[str, Any]) -> dict[str, Any]:
        epoch = self.repo.next_epoch(deployment["id"])
        replica_id = f"{deployment['id']}-r{epoch}-{uuid.uuid4().hex[:6]}"
        self.repo.create_replica(deployment["id"], replica_id, epoch, deployment["max_concurrency"])
        try:
            node, gpu_uuids, location = self._placement(deployment)
        except NodeRuntimeError as exc:
            state = "WAITING_FOR_ARTIFACT" if deployment.get("artifact_id") else "FAILED"
            self.repo.update_replica(replica_id, state=state, failure_reason=str(exc))
            return self.repo.get_replica(replica_id)  # type: ignore[return-value]
        return await self._provision(deployment, replica_id, node, gpu_uuids, location)

    async def _provision(self, deployment: dict[str, Any], replica_id: str, node: dict[str, Any],
                         gpu_uuids: list[str], location: dict[str, Any] | None) -> dict[str, Any]:
        deployment["artifact_location"] = location
        self.repo.update_replica(replica_id, node_id=node["id"], state="PROVISIONING", assigned_gpu_uuids_json=gpu_uuids)
        try:
            observed = await self.runtime.create(node, deployment, replica_id, gpu_uuids)
        except Exception as exc:
            self.repo.update_replica(replica_id, state="FAILED", failure_reason=f"runtime creation failed: {str(exc)[:300]}")
            raise
        runtime_id = observed.get("runtime_id") or observed.get("id")
        if not runtime_id:
            self.repo.update_replica(replica_id, state="FAILED", failure_reason="node agent omitted runtime_id")
            raise NodeRuntimeError("node agent omitted runtime_id")
        self.repo.update_replica(replica_id, runtime_id=runtime_id, ssh_port=observed.get("ssh_port"),
                                 container_state=observed.get("state"), state="STARTING")
        return self.repo.get_replica(replica_id)  # type: ignore[return-value]

    async def _refresh(self, deployment: dict[str, Any], replica: dict[str, Any]) -> None:
        if replica["state"] == "WAITING_FOR_ARTIFACT":
            try:
                node, gpu_uuids, location = self._placement(deployment)
            except NodeRuntimeError as exc:
                self.repo.update_replica(replica["id"], failure_reason=str(exc))
                return
            await self._provision(deployment, replica["id"], node, gpu_uuids, location)
            return
        node = self.repo.get_node(replica.get("node_id")) if replica.get("node_id") else None
        if not node or node["state"] != "ONLINE":
            await self.runtime.close(replica["id"])
            self.repo.update_replica(replica["id"], state="UNHEALTHY", endpoint_healthy=0, model_runtime_ready=0, failure_reason="node is offline")
            return
        if not replica.get("runtime_id"):
            self.repo.update_replica(replica["id"], state="FAILED", failure_reason="replica has no runtime id")
            return
        try:
            observed = await self.runtime.get(node, replica["runtime_id"])
        except Exception as exc:
            await self.runtime.close(replica["id"])
            self.repo.update_replica(replica["id"], state="UNHEALTHY", endpoint_healthy=0, model_runtime_ready=0, failure_reason=f"runtime lookup failed: {str(exc)[:300]}")
            return
        container_state = str(observed.get("state") or "unknown").lower()
        if container_state in {"exited", "dead", "error", "failed", "missing", "removed"}:
            await self.runtime.close(replica["id"])
            try:
                await self.runtime.delete(node, replica["runtime_id"])
            except NodeRuntimeError:
                pass
            log_tail = str(observed.get("recent_logs") or "").strip()[-2000:]
            detail = f"container entered {container_state} (exit_code={observed.get('exit_code')})"
            if log_tail:
                detail += f": {log_tail}"
            self.repo.update_replica(replica["id"], state="FAILED", container_state=container_state,
                                     endpoint_healthy=0, model_runtime_ready=0, failure_reason=detail)
            return
        if container_state != "running":
            self.repo.update_replica(replica["id"], state="STARTING", container_state=container_state,
                                     endpoint_healthy=0, model_runtime_ready=0, failure_reason=f"container is {container_state}")
            return
        try:
            endpoint = await self.runtime.endpoint(replica["id"], node, observed)
            health, model, detail = await self._probe(
                endpoint,
                deployment["served_model_id"],
                require_model_identity=deployment["runtime"] != "mlx",
            )
        except Exception as exc:
            endpoint, health, model, detail = None, False, False, f"runtime probe failed: {type(exc).__name__}: {str(exc)[:300]}"
        ready = health and model
        self.repo.update_replica(replica["id"], state="READY" if ready else "UNHEALTHY", endpoint=endpoint,
                                 ssh_port=observed.get("ssh_port"), container_state=container_state,
                                 endpoint_healthy=int(health), model_runtime_ready=int(model), last_health_at=utcnow(),
                                 failure_reason=None if ready else detail)

    async def _probe(
        self, endpoint: str, served_model_id: str, *, require_model_identity: bool = True
    ) -> tuple[bool, bool, str]:
        async with httpx.AsyncClient(timeout=self.probe_timeout) as client:
            health = await client.get(endpoint + "/health")
            if health.status_code >= 400:
                return False, False, f"/health returned {health.status_code}"
            models = await client.get(endpoint + "/v1/models")
            if models.status_code >= 400:
                return True, False, f"/v1/models returned {models.status_code}"
            try:
                ids = {item.get("id") for item in models.json().get("data", [])}
            except (ValueError, AttributeError):
                return True, False, "/v1/models returned invalid JSON"
            # mlx-lm only enumerates models in the global Hugging Face cache.
            # An immutable local snapshot can therefore be loaded and healthy
            # while absent from its /v1/models response.  The MLX server loads
            # the configured model before opening its listener, so health plus
            # a valid models response is the readiness boundary for that runtime.
            if require_model_identity and served_model_id not in ids:
                return True, False, f"served model {served_model_id!r} is absent"
        return True, True, "ready"

    async def _delete(self, deployment: dict[str, Any]) -> None:
        for replica in deployment["replicas"]:
            if replica["state"] in TERMINAL:
                continue
            self.repo.update_replica(replica["id"], state="DRAINING", endpoint_healthy=0, model_runtime_ready=0, failure_reason="deployment deletion requested")
            await self.runtime.close(replica["id"])
            node = self.repo.get_node(replica.get("node_id")) if replica.get("node_id") else None
            if node and replica.get("runtime_id"):
                try:
                    await self.runtime.delete(node, replica["runtime_id"])
                except NodeRuntimeError as exc:
                    self.repo.update_replica(replica["id"], state="FAILED", failure_reason=f"runtime deletion failed: {str(exc)[:300]}")
                    continue
            self.repo.update_replica(replica["id"], state="RELEASED", container_state="removed")
        self.repo.mark_deleted_if_empty(deployment["id"])
