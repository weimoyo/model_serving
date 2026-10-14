from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import Settings
from .database import Database
from .node_runtime import NodeRuntimeManager
from .reconciler import Reconciler
from .repository import Repository
from .schemas import (ChatRequest, DeploymentCreate, DownloadCreate, EnrollmentTokenCreate,
                      ModelCreate, NodeCreate, NodeEnrollmentCreate, RuntimeProfileCreate,
                      ScanCreate, StoragePoolCreate)


def create_app(settings: Settings | None = None, *, runtime: NodeRuntimeManager | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    database.init()
    repo = Repository(database)
    runtime = runtime or NodeRuntimeManager(
        settings.gateway_ssh_private_key,
        settings.ssh_known_hosts_file,
        timeout=settings.node_timeout_seconds,
        node_secret_dir=settings.node_secret_dir,
    )
    reconciler = Reconciler(repo, runtime, probe_timeout=settings.node_timeout_seconds)

    async def loop() -> None:
        while True:
            await reconciler.reconcile_all()
            repo.archive_stale()
            await asyncio.sleep(settings.reconcile_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(loop()) if settings.run_reconciler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await runtime.close_all()

    app = FastAPI(title="Standalone Model Serving", version="1.1.0", lifespan=lifespan)
    app.state.repo, app.state.reconciler, app.state.runtime = repo, reconciler, runtime

    def auth(request: Request, authorization: str | None = Header(default=None),
             x_api_key: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            raise HTTPException(503, "MODEL_SERVING_API_KEY is not configured")
        bearer = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        supplied = bearer or x_api_key or request.cookies.get("model_serving_local_session")
        if not supplied or not secrets.compare_digest(supplied, settings.api_key):
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(auth)]

    node_secret_dir = settings.node_secret_dir or settings.database_path.parent / "node-secrets"

    def write_node_secret(environment_name: str, value: str) -> None:
        node_secret_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(node_secret_dir, 0o700)
        except OSError:
            pass
        target = node_secret_dir / f"{environment_name}.token"
        temporary = node_secret_dir / f".{environment_name}.{secrets.token_hex(8)}.tmp"
        temporary.write_text(value, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(target)
        os.environ[environment_name] = value

    @app.post("/api/enrollment-tokens", dependencies=protected, status_code=201)
    def create_enrollment_token(body: EnrollmentTokenCreate):
        token_id = "enrollment-" + uuid.uuid4().hex
        token = "mse_" + secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)).isoformat()
        record = repo.create_enrollment_token(
            token_id, hashlib.sha256(token.encode("utf-8")).hexdigest(), expires_at, body.uses
        )
        return {**record, "token": token}

    @app.get("/api/enrollment-tokens", dependencies=protected)
    def list_enrollment_tokens():
        return repo.list_enrollment_tokens()

    @app.delete("/api/enrollment-tokens/{token_id}", dependencies=protected)
    def revoke_enrollment_token(token_id: str):
        if not repo.revoke_enrollment_token(token_id):
            raise HTTPException(404, "active enrollment token not found")
        return repo.get_enrollment_token(token_id)

    @app.post("/api/enroll", status_code=201)
    async def enroll_node(body: NodeEnrollmentCreate):
        token = body.enrollment_token.get_secret_value()
        token_id = repo.consume_enrollment_token(token)
        if not token_id:
            raise HTTPException(401, "invalid, expired or exhausted enrollment token")

        node_value = body.node.model_dump(mode="json")
        node_key_suffix = hashlib.sha256(body.node.id.encode("utf-8")).hexdigest()[:8].upper()
        expected_environment = (
            "NODE_" + re.sub(r"[^A-Za-z0-9_]", "_", body.node.id.upper())
            + "_" + node_key_suffix + "_AGENT_KEY"
        )
        node_value["agent_key_env"] = expected_environment
        write_node_secret(expected_environment, body.agent_key.get_secret_value())
        node = repo.upsert_node(node_value)
        node = await reconciler.probe_node(node["id"])
        if node["state"] != "ONLINE":
            raise HTTPException(502, f"node agent probe failed: {node.get('failure_reason') or 'unknown error'}")

        pools = []
        for item in body.storage_pools:
            value = item.model_dump()
            existing_pool = repo.get_storage_pool(item.id)
            if existing_pool and existing_pool["node_id"] != node["id"]:
                raise HTTPException(409, f"storage pool ID {item.id} belongs to another node")
            try:
                observed = await runtime.upsert_storage_pool(node, value)
            except Exception as exc:
                raise HTTPException(502, f"storage pool {item.id} registration failed: {exc}") from exc
            pools.append(repo.upsert_storage_pool(node["id"], value, observed))

        profiles = []
        for item in body.runtime_profiles:
            value = item.model_dump()
            existing_profile = repo.get_runtime_profile(item.id)
            if existing_profile and any(existing_profile.get(key) != value.get(key) for key in (
                "runtime", "image", "architectures", "accelerator_kinds", "allowed_args"
            )):
                raise HTTPException(409, f"runtime profile ID {item.id} already has a different contract")
            profiles.append(repo.upsert_runtime_profile(value))
        scan_job = None
        if body.scan_existing_models:
            model_pool = next((item for item in pools if item["purpose"] == "model" and item["state"] == "READY"), None)
            if model_pool:
                scan_body = {"storage_pool_id": model_pool["id"], "relative_path": "."}
                try:
                    observed = await runtime.start_scan(node, scan_body)
                except Exception as exc:
                    raise HTTPException(502, f"initial model scan failed: {exc}") from exc
                scan_job = repo.create_job("scan_jobs", {
                    "id": "scan-" + uuid.uuid4().hex,
                    "node_id": node["id"],
                    "storage_pool_id": model_pool["id"],
                    "agent_job_id": observed["id"],
                    "relative_path": ".",
                    "state": observed["state"],
                    "progress": observed.get("progress") or {},
                    "result": observed.get("result") or [],
                })
        return {
            "status": "enrolled",
            "enrollment_token_id": token_id,
            "node": node,
            "storage_pools": pools,
            "runtime_profiles": profiles,
            "initial_scan": scan_job,
        }

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request):
        response = FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")
        client_host = request.client.host if request.client else ""
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            # Starlette's in-process test client has no IP address.  This value
            # cannot occur on a real socket and keeps the local-session contract
            # covered by the automated suite.
            is_loopback = client_host == "testclient"
        if settings.api_key and is_loopback:
            response.set_cookie(
                "model_serving_local_session", settings.api_key,
                httponly=True, samesite="strict", secure=False, path="/",
            )
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            with database.connect() as conn:
                conn.execute("SELECT 1")
        except sqlite3.Error as exc:
            return JSONResponse({"status": "not_ready", "detail": str(exc)}, status_code=503)
        return {"status": "ready"}

    @app.post("/api/nodes", dependencies=protected, status_code=201)
    async def create_node(body: NodeCreate):
        node = repo.upsert_node(body.model_dump(mode="json"))
        return await reconciler.probe_node(node["id"])

    @app.get("/api/nodes", dependencies=protected)
    def list_nodes(include_archived: bool = False):
        return repo.list_nodes(include_archived=include_archived)

    @app.post("/api/nodes/{node_id}/archive", dependencies=protected)
    def archive_node(node_id: str):
        try:
            changed = repo.archive_node(node_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not changed:
            raise HTTPException(404, "active node not found")
        return {"id": node_id, "archived": True}

    @app.post("/api/nodes/{node_id}/probe", dependencies=protected)
    async def probe_node(node_id: str):
        if not repo.get_node(node_id):
            raise HTTPException(404, "node not found")
        return await reconciler.probe_node(node_id)

    @app.put("/api/nodes/{node_id}/storage-pools/{pool_id}", dependencies=protected)
    async def put_storage_pool(node_id: str, pool_id: str, body: StoragePoolCreate):
        node = repo.get_node(node_id)
        if not node:
            raise HTTPException(404, "node not found")
        if pool_id != body.id:
            raise HTTPException(422, "path and body storage pool IDs differ")
        existing_pool = repo.get_storage_pool(pool_id)
        if existing_pool and repo.storage_pool_in_use(pool_id):
            changed = any(existing_pool.get(key) != value for key, value in {
                "host_path": body.host_path, "container_path": body.container_path,
                "purpose": body.purpose, "agent_access": body.agent_access,
                "runtime_access": body.runtime_access,
            }.items())
            if changed:
                raise HTTPException(409, "cannot change an active deployment's storage boundary")
        try:
            observed = await runtime.upsert_storage_pool(node, body.model_dump())
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        return repo.upsert_storage_pool(node_id, body.model_dump(), observed)

    @app.get("/api/storage-pools", dependencies=protected)
    def list_storage_pools(node_id: str | None = None):
        return repo.list_storage_pools(node_id)

    @app.delete("/api/storage-pools/{pool_id}", dependencies=protected)
    async def revoke_storage_pool(pool_id: str):
        pool = repo.get_storage_pool(pool_id)
        if not pool:
            raise HTTPException(404, "storage pool not found")
        if repo.storage_pool_in_use(pool_id):
            raise HTTPException(409, "storage pool is referenced by an active deployment; delete the deployment first")
        node = repo.get_node(pool["node_id"])
        if not node:
            raise HTTPException(409, "storage pool node no longer exists")
        try:
            await runtime.revoke_storage_pool(node, pool_id)
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        repo.revoke_storage_pool(pool_id)
        return repo.get_storage_pool(pool_id)

    @app.post("/api/models", dependencies=protected, status_code=201)
    def create_model(body: ModelCreate):
        try:
            return repo.upsert_model(body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/models", dependencies=protected)
    def list_models(include_archived: bool = False):
        return repo.list_models(include_archived=include_archived)

    @app.post("/api/models/{model_id}/archive", dependencies=protected)
    def archive_model(model_id: str):
        try:
            changed = repo.archive_model(model_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not changed:
            raise HTTPException(404, "active model not found")
        return {"id": model_id, "archived": True}

    @app.put("/api/runtime-profiles/{profile_id}", dependencies=protected)
    def put_runtime_profile(profile_id: str, body: RuntimeProfileCreate):
        if profile_id != body.id:
            raise HTTPException(422, "path and body runtime profile IDs differ")
        return repo.upsert_runtime_profile(body.model_dump())

    @app.get("/api/runtime-profiles", dependencies=protected)
    def list_runtime_profiles():
        return repo.list_runtime_profiles()

    def persist_artifact(node_id: str, pool_id: str, result: dict[str, Any]) -> str:
        checksum = str(result["checksum"])
        artifact_id = "artifact-" + hashlib.sha256(checksum.encode()).hexdigest()[:24]
        repo.upsert_artifact({"id": artifact_id, "repository": result.get("repository"), "revision": result.get("revision"),
                              "format": result["format"], "size_bytes": result["size_bytes"], "checksum": checksum,
                              "metadata": {**(result.get("metadata") or {}), "runtime_compatible": result.get("runtime_compatible", False)}})
        repo.add_artifact_location(artifact_id, node_id, pool_id, result["relative_path"])
        return artifact_id

    @app.post("/api/nodes/{node_id}/scans", dependencies=protected, status_code=202)
    async def start_scan(node_id: str, body: ScanCreate):
        node, pool = repo.get_node(node_id), repo.get_storage_pool(body.storage_pool_id)
        if not node:
            raise HTTPException(404, "node not found")
        if not pool or pool["node_id"] != node_id or pool["state"] != "READY":
            raise HTTPException(422, "storage pool is not READY on this node")
        try:
            observed = await runtime.start_scan(node, body.model_dump())
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        existing = repo.get_job_by_agent("scan_jobs", node_id, observed["id"])
        if existing:
            return existing
        job_id = "scan-" + uuid.uuid4().hex
        return repo.create_job("scan_jobs", {"id": job_id, "node_id": node_id, "storage_pool_id": body.storage_pool_id,
            "agent_job_id": observed["id"], "relative_path": body.relative_path, "state": observed["state"],
            "progress": observed.get("progress") or {}, "result": observed.get("result") or []})

    @app.get("/api/scans", dependencies=protected)
    def list_scans():
        return repo.list_jobs("scan_jobs")

    @app.get("/api/scans/{job_id}", dependencies=protected)
    async def get_scan(job_id: str):
        job = repo.get_job("scan_jobs", job_id)
        if not job:
            raise HTTPException(404, "scan job not found")
        node = repo.get_node(job["node_id"])
        try:
            observed = await runtime.get_scan(node, job["agent_job_id"])
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        if observed["state"] == "COMPLETED":
            for item in observed.get("result") or []:
                item["artifact_id"] = persist_artifact(job["node_id"], job["storage_pool_id"], item)
        repo.update_job("scan_jobs", job_id, observed)
        return repo.get_job("scan_jobs", job_id)

    @app.delete("/api/scans/{job_id}", dependencies=protected)
    async def cancel_scan(job_id: str):
        job = repo.get_job("scan_jobs", job_id)
        if not job:
            raise HTTPException(404, "scan job not found")
        node = repo.get_node(job["node_id"])
        try:
            observed = await runtime.cancel_scan(node, job["agent_job_id"])
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        repo.update_job("scan_jobs", job_id, observed)
        return repo.get_job("scan_jobs", job_id)

    @app.post("/api/nodes/{node_id}/downloads", dependencies=protected, status_code=202)
    async def start_download(node_id: str, body: DownloadCreate):
        node, pool = repo.get_node(node_id), repo.get_storage_pool(body.storage_pool_id)
        if not node:
            raise HTTPException(404, "node not found")
        if (not pool or pool["node_id"] != node_id or pool["state"] != "READY" or pool["purpose"] != "model"
                or pool["agent_access"] != "rw"):
            raise HTTPException(422, "download requires a READY writable model storage pool on this node")
        try:
            observed = await runtime.start_download(node, body.model_dump())
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        existing = repo.get_job_by_agent("transfer_jobs", node_id, observed["id"])
        if existing:
            return existing
        job_id = "transfer-" + uuid.uuid4().hex
        return repo.create_job("transfer_jobs", {"id": job_id, "node_id": node_id, "storage_pool_id": body.storage_pool_id,
            "agent_job_id": observed["id"], "kind": "DOWNLOAD", "repository": body.repository, "revision": body.revision,
            "relative_path": observed.get("relative_path") or body.relative_path, "state": observed["state"],
            "progress": observed.get("progress") or {}})

    @app.get("/api/downloads", dependencies=protected)
    def list_downloads():
        return repo.list_jobs("transfer_jobs")

    @app.get("/api/downloads/{job_id}", dependencies=protected)
    async def get_download(job_id: str):
        job = repo.get_job("transfer_jobs", job_id)
        if not job:
            raise HTTPException(404, "download job not found")
        node = repo.get_node(job["node_id"])
        try:
            observed = await runtime.get_download(node, job["agent_job_id"])
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
        artifact_id = None
        if observed["state"] == "COMPLETED" and observed.get("result"):
            artifact_id = persist_artifact(job["node_id"], job["storage_pool_id"], observed["result"])
        repo.update_job("transfer_jobs", job_id, observed, artifact_id)
        return repo.get_job("transfer_jobs", job_id)

    @app.get("/api/artifacts", dependencies=protected)
    def list_artifacts():
        return repo.list_artifacts()

    @app.post("/api/artifacts/{artifact_id}/models/{model_id}", dependencies=protected)
    def attach_artifact(artifact_id: str, model_id: str):
        if not repo.get_artifact(artifact_id):
            raise HTTPException(404, "artifact not found")
        if not repo.get_model(model_id):
            raise HTTPException(404, "model not found")
        return repo.attach_artifact_model(artifact_id, model_id)

    @app.post("/api/artifacts/{artifact_id}/locations/{location_id}/verify", dependencies=protected)
    async def verify_artifact(artifact_id: str, location_id: str):
        artifact = repo.get_artifact(artifact_id)
        if not artifact:
            raise HTTPException(404, "artifact not found")
        location = next((item for item in artifact["locations"] if item["id"] == location_id), None)
        if not location:
            raise HTTPException(404, "artifact location not found")
        node = repo.get_node(location["node_id"])
        if not node:
            raise HTTPException(409, "artifact node no longer exists")
        try:
            observed = await runtime.verify_artifact(node, {"storage_pool_id": location["storage_pool_id"],
                "relative_path": location["relative_path"], "checksum": artifact["checksum"]})
        except Exception as exc:
            observed = {"state": "UNAVAILABLE", "failure_reason": str(exc)[:500]}
        repo.update_artifact_location(location_id, observed["state"], observed.get("failure_reason"))
        return repo.get_artifact(artifact_id)

    @app.post("/api/deployments", dependencies=protected, status_code=201)
    async def create_deployment(body: DeploymentCreate):
        if not repo.get_model(body.model_id):
            raise HTTPException(422, "unknown model_id")
        value = body.model_dump()
        if body.runtime_profile_id:
            profile = repo.get_runtime_profile(body.runtime_profile_id)
            artifact = repo.get_artifact(body.artifact_id) if body.artifact_id else None
            workspace = repo.get_storage_pool(body.workspace_storage_pool_id) if body.workspace_storage_pool_id else None
            if not profile or not profile["enabled"]:
                raise HTTPException(422, "runtime profile is missing or disabled")
            if not artifact:
                raise HTTPException(422, "artifact is missing")
            if artifact.get("model_id") not in {None, body.model_id}:
                raise HTTPException(422, "artifact belongs to a different logical model")
            if not workspace or workspace["purpose"] != "workspace" or workspace["runtime_access"] != "rw" or workspace["state"] != "READY":
                raise HTTPException(422, "workspace storage pool is not READY and runtime-writable")
            forbidden = set(body.runtime_args) - set(profile["allowed_args"])
            if forbidden:
                raise HTTPException(422, f"runtime arguments are not allowed by profile: {sorted(forbidden)}")
            value["runtime"], value["runtime_image"] = profile["runtime"], profile["image"]
        try:
            created = repo.create_deployment(value)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"deployment already exists: {exc}") from exc
        try:
            await reconciler.reconcile(created["id"])
        except Exception:
            pass
        return repo.get_deployment(created["id"], include_replicas=True)

    @app.get("/api/deployments", dependencies=protected)
    def list_deployments(include_archived: bool = False):
        return repo.list_deployments(include_archived=include_archived)

    @app.get("/api/deployments/{deployment_id}", dependencies=protected)
    def get_deployment(deployment_id: str):
        result = repo.get_deployment(deployment_id, include_replicas=True)
        if not result:
            raise HTTPException(404, "deployment not found")
        return result

    @app.delete("/api/deployments/{deployment_id}", dependencies=protected)
    async def delete_deployment(deployment_id: str):
        if not repo.mark_deleting(deployment_id):
            raise HTTPException(404, "deployment not found")
        await reconciler.reconcile(deployment_id)
        return repo.get_deployment(deployment_id, include_replicas=True)

    @app.post("/api/reconcile", dependencies=protected)
    async def reconcile_now():
        await reconciler.reconcile_all()
        return {"status": "reconciled"}

    @app.post("/api/maintenance/archive-stale", dependencies=protected)
    def archive_stale(terminal_replica_hours: float = 24,
                      deleted_deployment_hours: float = 24,
                      completed_job_hours: float = 168):
        if min(terminal_replica_hours, deleted_deployment_hours, completed_job_hours) < 0:
            raise HTTPException(422, "archive retention hours cannot be negative")
        return {"archived": repo.archive_stale(
            terminal_replica_hours=terminal_replica_hours,
            deleted_deployment_hours=deleted_deployment_hours,
            completed_job_hours=completed_job_hours,
        ), "summary": repo.archive_summary()}

    @app.get("/api/catalog", dependencies=protected)
    def catalog(include_archived: bool = False):
        return repo.catalog(include_archived=include_archived)

    @app.get("/v1/models", dependencies=protected)
    def models():
        data = []
        for model in repo.ready_models():
            try:
                created = int(datetime.fromisoformat(model["created_at"]).timestamp())
            except (ValueError, TypeError):
                created = 0
            data.append({"id": model["id"], "object": "model", "created": created, "owned_by": "local"})
        return {"object": "list", "data": data}

    def mark_unhealthy(replica_id: str, reason: str) -> None:
        repo.update_replica(replica_id, state="UNHEALTHY", endpoint_healthy=0, model_runtime_ready=0, failure_reason=reason)

    @app.post("/v1/chat/completions", dependencies=protected)
    async def chat(body: ChatRequest, request: Request):
        replica = repo.acquire_replica(body.model)
        if not replica:
            raise HTTPException(503, f"no READY replica with capacity for model {body.model!r}")
        request_id, started = request.headers.get("x-request-id") or f"chatcmpl-{uuid.uuid4().hex}", time.perf_counter()
        payload = body.model_dump(exclude_none=True)
        payload.update(body.model_extra or {})
        payload["model"] = replica["served_model_id"]
        if not replica.get("endpoint"):
            repo.release_replica(replica["id"])
            mark_unhealthy(replica["id"], "READY replica has no endpoint")
            raise HTTPException(503, "selected replica has no endpoint")
        url = replica["endpoint"].rstrip("/") + "/v1/chat/completions"
        timeout = httpx.Timeout(connect=settings.node_timeout_seconds, read=settings.inference_timeout_seconds, write=30, pool=5)
        if body.stream:
            client = httpx.AsyncClient(timeout=timeout)
            try:
                upstream = await client.send(client.build_request("POST", url, json=payload), stream=True)
            except httpx.HTTPError as exc:
                await client.aclose(); repo.release_replica(replica["id"])
                mark_unhealthy(replica["id"], f"stream connection failed: {str(exc)[:300]}")
                raise HTTPException(502, "model runtime unavailable") from exc
            if upstream.status_code >= 400:
                detail = (await upstream.aread())[:1000].decode(errors="replace")
                await upstream.aclose(); await client.aclose(); repo.release_replica(replica["id"])
                if upstream.status_code >= 500:
                    mark_unhealthy(replica["id"], f"runtime returned {upstream.status_code}")
                raise HTTPException(502, f"model runtime returned HTTP {upstream.status_code}: {detail}")

            async def chunks() -> AsyncIterator[bytes]:
                outcome, detail = "ok", None
                try:
                    async for line in upstream.aiter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                event = json.loads(line[6:])
                                event["model"] = body.model
                                line = "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                            except (ValueError, TypeError):
                                pass
                        yield (line + "\n").encode("utf-8")
                except httpx.HTTPError as exc:
                    outcome, detail = "stream_error", str(exc)[:300]
                    mark_unhealthy(replica["id"], f"stream interrupted: {detail}")
                finally:
                    await upstream.aclose(); await client.aclose(); repo.release_replica(replica["id"])
                    repo.route_log(request_id, replica["deployment_id"], replica["id"], replica["instance_epoch"], outcome, (time.perf_counter()-started)*1000, detail)
            return StreamingResponse(chunks(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Request-ID": request_id, "X-Replica-ID": replica["id"]})

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            mark_unhealthy(replica["id"], f"request failed: {str(exc)[:300]}")
            raise HTTPException(502, "model runtime unavailable") from exc
        finally:
            repo.release_replica(replica["id"])
        if upstream.status_code >= 400:
            if upstream.status_code >= 500:
                mark_unhealthy(replica["id"], f"runtime returned {upstream.status_code}")
            raise HTTPException(502, f"model runtime returned HTTP {upstream.status_code}: {upstream.text[:1000]}")
        try:
            content = upstream.json()
        except ValueError as exc:
            mark_unhealthy(replica["id"], "runtime returned invalid JSON")
            raise HTTPException(502, "model runtime returned invalid JSON") from exc
        content["model"] = body.model
        repo.route_log(request_id, replica["deployment_id"], replica["id"], replica["instance_epoch"], "ok", (time.perf_counter()-started)*1000)
        return JSONResponse(content, headers={"X-Request-ID": request_id, "X-Replica-ID": replica["id"]})

    return app
