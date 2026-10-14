from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database, decode_row, utcnow


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def upsert_node(self, value: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute(
                """INSERT INTO nodes(id,name,host,agent_url,agent_key_env,platform,network_zone,state,inventory_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'UNKNOWN','{}',?,?) ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,host=excluded.host,agent_url=excluded.agent_url,agent_key_env=excluded.agent_key_env,
                   platform=excluded.platform,network_zone=excluded.network_zone,archived_at=NULL,updated_at=excluded.updated_at""",
                (value["id"], value["name"], value["host"], str(value["agent_url"]).rstrip("/"), value["agent_key_env"],
                 value.get("platform"), value.get("network_zone"), now, now),
            )
        return self.get_node(value["id"])  # type: ignore[return-value]

    def create_enrollment_token(self, token_id: str, token_hash: str, expires_at: str,
                                uses: int) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO enrollment_tokens(id,token_hash,expires_at,uses_remaining,created_at) VALUES(?,?,?,?,?)",
                (token_id, token_hash, expires_at, uses, now),
            )
        return self.get_enrollment_token(token_id)  # type: ignore[return-value]

    def get_enrollment_token(self, token_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute(
                "SELECT id,expires_at,uses_remaining,created_at,last_used_at,revoked_at FROM enrollment_tokens WHERE id=?",
                (token_id,),
            ).fetchone())

    def list_enrollment_tokens(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [decode_row(row) for row in conn.execute(
                "SELECT id,expires_at,uses_remaining,created_at,last_used_at,revoked_at "
                "FROM enrollment_tokens ORDER BY created_at DESC"
            )]  # type: ignore[misc]

    def consume_enrollment_token(self, token: str) -> str | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = utcnow()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id FROM enrollment_tokens WHERE token_hash=? AND revoked_at IS NULL "
                "AND uses_remaining>0 AND expires_at>?",
                (token_hash, now),
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE enrollment_tokens SET uses_remaining=uses_remaining-1,last_used_at=? "
                "WHERE id=? AND uses_remaining>0 AND revoked_at IS NULL AND expires_at>?",
                (now, row["id"], now),
            ).rowcount
            return str(row["id"]) if changed else None

    def revoke_enrollment_token(self, token_id: str) -> bool:
        with self.database.connect() as conn:
            return conn.execute(
                "UPDATE enrollment_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (utcnow(), token_id),
            ).rowcount > 0

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("SELECT * FROM nodes WHERE id=? AND archived_at IS NULL", (node_id,)).fetchone())

    def list_nodes(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            where = "" if include_archived else " WHERE archived_at IS NULL"
            return [decode_row(row) for row in conn.execute("SELECT * FROM nodes" + where + " ORDER BY id")]  # type: ignore[misc]

    def upsert_storage_pool(self, node_id: str, value: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        quota_bytes = int(value["quota_gib"] * 1024**3) if value.get("quota_gib") else None
        min_free_bytes = int(value.get("min_free_gib", 0) * 1024**3)
        with self.database.connect() as conn:
            conn.execute(
                """INSERT INTO storage_pools(id,node_id,host_path,container_path,purpose,agent_access,runtime_access,
                   quota_bytes,min_free_bytes,state,capacity_bytes,free_bytes,used_bytes,failure_reason,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   host_path=excluded.host_path,container_path=excluded.container_path,purpose=excluded.purpose,
                   agent_access=excluded.agent_access,runtime_access=excluded.runtime_access,quota_bytes=excluded.quota_bytes,
                   min_free_bytes=excluded.min_free_bytes,state=excluded.state,capacity_bytes=excluded.capacity_bytes,
                   free_bytes=excluded.free_bytes,used_bytes=excluded.used_bytes,failure_reason=excluded.failure_reason,updated_at=excluded.updated_at""",
                (value["id"], node_id, observation["host_path"], value["container_path"], value["purpose"],
                 value["agent_access"], value["runtime_access"], quota_bytes, min_free_bytes,
                 observation.get("state", "READY"), observation.get("capacity_bytes"), observation.get("free_bytes"),
                 observation.get("used_bytes"), observation.get("failure_reason"), now, now),
            )
        return self.get_storage_pool(value["id"])  # type: ignore[return-value]

    def get_storage_pool(self, pool_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("SELECT * FROM storage_pools WHERE id=?", (pool_id,)).fetchone())

    def list_storage_pools(self, node_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            query, args = "SELECT * FROM storage_pools", ()
            if node_id:
                query, args = query + " WHERE node_id=?", (node_id,)
            return [decode_row(row) for row in conn.execute(query + " ORDER BY id", args)]  # type: ignore[misc]

    def revoke_storage_pool(self, pool_id: str) -> bool:
        with self.database.connect() as conn:
            now = utcnow()
            changed = conn.execute("UPDATE storage_pools SET state='REVOKED',updated_at=? WHERE id=? AND state!='REVOKED'",
                                   (now, pool_id)).rowcount > 0
            conn.execute("UPDATE artifact_locations SET state='REVOKED',failure_reason='storage pool revoked',updated_at=? WHERE storage_pool_id=?",
                         (now, pool_id))
            return changed

    def storage_pool_in_use(self, pool_id: str) -> bool:
        with self.database.connect() as conn:
            workspace = conn.execute("SELECT 1 FROM deployments WHERE workspace_storage_pool_id=? AND state IN ('ACTIVE','DELETING') LIMIT 1",
                                     (pool_id,)).fetchone()
            artifact = conn.execute("""SELECT 1 FROM deployments d JOIN artifact_locations l ON l.artifact_id=d.artifact_id
                WHERE l.storage_pool_id=? AND d.state IN ('ACTIVE','DELETING') LIMIT 1""", (pool_id,)).fetchone()
            return bool(workspace or artifact)

    def update_node_observation(self, node_id: str, *, state: str, inventory: dict[str, Any] | None = None, reason: str | None = None) -> None:
        with self.database.connect() as conn:
            if inventory is None:
                conn.execute("UPDATE nodes SET state=?,failure_reason=?,updated_at=? WHERE id=?", (state, reason, utcnow(), node_id))
            else:
                now = utcnow()
                conn.execute("UPDATE nodes SET state=?,inventory_json=?,last_seen_at=?,failure_reason=?,updated_at=? WHERE id=?",
                             (state, json.dumps(inventory), now, reason, now, node_id))

    def upsert_model(self, value: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute(
                """INSERT INTO models(id,display_name,family,task,capabilities_json,repository,revision,parameter_count,precision,context_length,license,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   display_name=excluded.display_name,family=excluded.family,task=excluded.task,
                   capabilities_json=excluded.capabilities_json,parameter_count=excluded.parameter_count,
                   precision=excluded.precision,context_length=excluded.context_length,license=excluded.license,
                   archived_at=NULL,updated_at=excluded.updated_at WHERE models.repository=excluded.repository AND models.revision=excluded.revision""",
                (value["id"], value["display_name"], value.get("family"), value["task"], json.dumps(value["capabilities"]),
                 value["repository"], value["revision"], value.get("parameter_count"), value.get("precision"),
                 value.get("context_length"), value.get("license"), now, now),
            )
            row = conn.execute("SELECT repository,revision FROM models WHERE id=?", (value["id"],)).fetchone()
            if row and (row["repository"] != value["repository"] or row["revision"] != value["revision"]):
                raise ValueError("model repository and immutable revision cannot be changed")
        return self.get_model(value["id"])  # type: ignore[return-value]

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("SELECT * FROM models WHERE id=? AND archived_at IS NULL", (model_id,)).fetchone())

    def list_models(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            where = "" if include_archived else " WHERE archived_at IS NULL"
            return [decode_row(row) for row in conn.execute("SELECT * FROM models" + where + " ORDER BY id")]  # type: ignore[misc]

    def upsert_runtime_profile(self, value: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute("""INSERT INTO runtime_profiles(id,name,runtime,image,architectures_json,accelerator_kinds_json,
                allowed_args_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,runtime=excluded.runtime,image=excluded.image,architectures_json=excluded.architectures_json,
                accelerator_kinds_json=excluded.accelerator_kinds_json,allowed_args_json=excluded.allowed_args_json,
                enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (value["id"], value["name"], value["runtime"], value["image"], json.dumps(value["architectures"]),
                 json.dumps(value["accelerator_kinds"]), json.dumps(value["allowed_args"]), int(value["enabled"]), now, now))
        return self.get_runtime_profile(value["id"])  # type: ignore[return-value]

    def get_runtime_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("SELECT * FROM runtime_profiles WHERE id=?", (profile_id,)).fetchone())

    def list_runtime_profiles(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [decode_row(row) for row in conn.execute("SELECT * FROM runtime_profiles ORDER BY id")]  # type: ignore[misc]

    def upsert_artifact(self, value: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            existing = conn.execute("SELECT checksum FROM model_artifacts WHERE id=?", (value["id"],)).fetchone()
            if existing and existing["checksum"] != value["checksum"]:
                raise ValueError("artifact id already refers to different content")
            conn.execute("""INSERT INTO model_artifacts(id,model_id,repository,revision,format,size_bytes,checksum,metadata_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET model_id=COALESCE(excluded.model_id,model_artifacts.model_id),
                metadata_json=excluded.metadata_json,size_bytes=excluded.size_bytes,updated_at=excluded.updated_at""",
                (value["id"], value.get("model_id"), value.get("repository"), value.get("revision"), value["format"],
                 value["size_bytes"], value["checksum"], json.dumps(value.get("metadata") or {}), now, now))
        return self.get_artifact(value["id"])  # type: ignore[return-value]

    def add_artifact_location(self, artifact_id: str, node_id: str, pool_id: str, relative_path: str,
                              *, state: str = "READY", failure_reason: str | None = None) -> dict[str, Any]:
        identity = f"{artifact_id}\0{node_id}\0{pool_id}\0{relative_path}".encode()
        location_id = "location-" + hashlib.sha256(identity).hexdigest()[:24]
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute("""INSERT INTO artifact_locations(id,artifact_id,node_id,storage_pool_id,relative_path,state,last_verified_at,
                failure_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id,node_id,storage_pool_id,relative_path)
                DO UPDATE SET state=excluded.state,last_verified_at=excluded.last_verified_at,failure_reason=excluded.failure_reason,
                updated_at=excluded.updated_at""", (location_id, artifact_id, node_id, pool_id, relative_path, state, now,
                failure_reason, now, now))
        return self.get_artifact_location(artifact_id, node_id)  # type: ignore[return-value]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            value = decode_row(conn.execute("SELECT * FROM model_artifacts WHERE id=?", (artifact_id,)).fetchone())
            if value:
                value["locations"] = [decode_row(row) for row in conn.execute(
                    "SELECT * FROM artifact_locations WHERE artifact_id=? ORDER BY node_id", (artifact_id,))]
            return value

    def attach_artifact_model(self, artifact_id: str, model_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            conn.execute("UPDATE model_artifacts SET model_id=?,updated_at=? WHERE id=?", (model_id, utcnow(), artifact_id))
        return self.get_artifact(artifact_id)

    def list_artifacts(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM model_artifacts ORDER BY id")]
        return [self.get_artifact(item) for item in ids]  # type: ignore[misc]

    def get_artifact_location(self, artifact_id: str, node_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("""SELECT * FROM artifact_locations WHERE artifact_id=? AND node_id=?
                AND state='READY' ORDER BY last_verified_at DESC LIMIT 1""", (artifact_id, node_id)).fetchone())

    def update_artifact_location(self, location_id: str, state: str, failure_reason: str | None) -> None:
        with self.database.connect() as conn:
            conn.execute("UPDATE artifact_locations SET state=?,last_verified_at=?,failure_reason=?,updated_at=? WHERE id=?",
                         (state, utcnow(), failure_reason, utcnow(), location_id))

    def create_job(self, table: str, value: dict[str, Any]) -> dict[str, Any]:
        if table not in {"scan_jobs", "transfer_jobs"}:
            raise ValueError("invalid job table")
        now = utcnow()
        with self.database.connect() as conn:
            if table == "scan_jobs":
                conn.execute("""INSERT INTO scan_jobs(id,node_id,storage_pool_id,agent_job_id,relative_path,state,progress_json,
                    result_json,failure_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (value["id"], value["node_id"], value["storage_pool_id"], value["agent_job_id"], value["relative_path"],
                     value["state"], json.dumps(value.get("progress") or {}), json.dumps(value.get("result") or []),
                     value.get("failure_reason"), now, now))
            else:
                conn.execute("""INSERT INTO transfer_jobs(id,node_id,storage_pool_id,agent_job_id,kind,repository,revision,
                    relative_path,state,progress_json,artifact_id,failure_reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (value["id"], value["node_id"], value["storage_pool_id"],
                    value["agent_job_id"], value["kind"], value.get("repository"), value.get("revision"), value.get("relative_path"),
                    value["state"], json.dumps(value.get("progress") or {}), value.get("artifact_id"), value.get("failure_reason"), now, now))
        return self.get_job(table, value["id"])  # type: ignore[return-value]

    def get_job(self, table: str, job_id: str) -> dict[str, Any] | None:
        if table not in {"scan_jobs", "transfer_jobs"}:
            raise ValueError("invalid job table")
        with self.database.connect() as conn:
            return decode_row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (job_id,)).fetchone())

    def get_job_by_agent(self, table: str, node_id: str, agent_job_id: str) -> dict[str, Any] | None:
        if table not in {"scan_jobs", "transfer_jobs"}:
            raise ValueError("invalid job table")
        with self.database.connect() as conn:
            return decode_row(conn.execute(f"SELECT * FROM {table} WHERE node_id=? AND agent_job_id=?",
                                           (node_id, agent_job_id)).fetchone())

    def list_jobs(self, table: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        if table not in {"scan_jobs", "transfer_jobs"}:
            raise ValueError("invalid job table")
        with self.database.connect() as conn:
            where = "" if include_archived else " WHERE archived_at IS NULL"
            return [decode_row(row) for row in conn.execute(f"SELECT * FROM {table}{where} ORDER BY created_at DESC")]  # type: ignore[misc]

    def update_job(self, table: str, job_id: str, observed: dict[str, Any], artifact_id: str | None = None) -> None:
        if table not in {"scan_jobs", "transfer_jobs"}:
            raise ValueError("invalid job table")
        fields: dict[str, Any] = {"state": observed["state"], "progress_json": json.dumps(observed.get("progress") or {}),
                                  "failure_reason": observed.get("failure_reason"), "updated_at": utcnow()}
        if table == "scan_jobs":
            fields["result_json"] = json.dumps(observed.get("result") or [])
        elif artifact_id:
            fields["artifact_id"] = artifact_id
        with self.database.connect() as conn:
            conn.execute(f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                         (*fields.values(), job_id))

    def create_deployment(self, value: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute(
                """INSERT INTO deployments(id,name,model_id,served_model_id,runtime,runtime_image,runtime_args_json,
                   gpu_count_per_replica,cpu_cores_per_replica,memory_gib_per_replica,max_concurrency,
                   desired_replicas,state,created_at,updated_at,artifact_id,runtime_profile_id,workspace_storage_pool_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?,?)""",
                (value["id"], value["name"], value["model_id"], value["served_model_id"], value["runtime"],
                 value["runtime_image"], json.dumps(value["runtime_args"]), value["gpu_count_per_replica"],
                 value.get("cpu_cores_per_replica"), value.get("memory_gib_per_replica"), value["max_concurrency"],
                 value["desired_replicas"], now, now, value.get("artifact_id"), value.get("runtime_profile_id"),
                 value.get("workspace_storage_pool_id")),
            )
        return self.get_deployment(value["id"], include_replicas=True)  # type: ignore[return-value]

    def get_deployment(self, deployment_id: str, *, include_replicas: bool = False,
                       include_archived: bool = False) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            result = decode_row(conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone())
            if result and include_replicas:
                archived = "" if include_archived else " AND archived_at IS NULL"
                result["replicas"] = [decode_row(r) for r in conn.execute(
                    "SELECT * FROM replicas WHERE deployment_id=?" + archived + " ORDER BY created_at", (deployment_id,))]
            return result

    def deployment_context(self, deployment_id: str) -> dict[str, Any] | None:
        deployment = self.get_deployment(deployment_id, include_replicas=True)
        if deployment:
            deployment["model"] = self.get_model(deployment["model_id"])
            deployment["artifact"] = self.get_artifact(deployment["artifact_id"]) if deployment.get("artifact_id") else None
            deployment["runtime_profile"] = self.get_runtime_profile(deployment["runtime_profile_id"]) if deployment.get("runtime_profile_id") else None
            deployment["workspace_storage_pool"] = self.get_storage_pool(deployment["workspace_storage_pool_id"]) if deployment.get("workspace_storage_pool_id") else None
        return deployment

    def list_deployments(self, *, active_only: bool = False, include_archived: bool = False) -> list[dict[str, Any]]:
        clauses = []
        if active_only:
            clauses.append("state IN ('ACTIVE','DELETING')")
        if not include_archived:
            clauses.append("archived_at IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM deployments" + where + " ORDER BY created_at")]
        return [self.get_deployment(item, include_replicas=True, include_archived=include_archived) for item in ids]  # type: ignore[misc]

    def mark_deleting(self, deployment_id: str) -> bool:
        with self.database.connect() as conn:
            return conn.execute("UPDATE deployments SET desired_replicas=0,state='DELETING',updated_at=? WHERE id=? AND state!='DELETED'", (utcnow(), deployment_id)).rowcount > 0

    def mark_deleted_if_empty(self, deployment_id: str) -> None:
        with self.database.connect() as conn:
            live = conn.execute("SELECT COUNT(*) FROM replicas WHERE deployment_id=? AND state NOT IN ('RELEASED','FAILED')", (deployment_id,)).fetchone()[0]
            if live == 0:
                conn.execute("UPDATE deployments SET state='DELETED',updated_at=? WHERE id=?", (utcnow(), deployment_id))

    def create_replica(self, deployment_id: str, replica_id: str, epoch: int, max_concurrency: int) -> dict[str, Any]:
        now = utcnow()
        with self.database.connect() as conn:
            conn.execute("""INSERT INTO replicas(id,deployment_id,instance_epoch,state,assigned_gpu_uuids_json,max_concurrency,created_at,updated_at)
                            VALUES(?,?,?,'PENDING','[]',?,?,?)""", (replica_id, deployment_id, epoch, max_concurrency, now, now))
        return self.get_replica(replica_id)  # type: ignore[return-value]

    def get_replica(self, replica_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return decode_row(conn.execute("SELECT * FROM replicas WHERE id=?", (replica_id,)).fetchone())

    def update_replica(self, replica_id: str, **fields: Any) -> None:
        allowed = {"node_id", "runtime_id", "state", "endpoint", "ssh_port", "container_state", "assigned_gpu_uuids_json",
                   "endpoint_healthy", "model_runtime_ready", "last_health_at", "failure_reason"}
        if set(fields) - allowed:
            raise ValueError("unsupported replica update")
        values = {key: json.dumps(value) if key == "assigned_gpu_uuids_json" and not isinstance(value, str) else value for key, value in fields.items()}
        values["updated_at"] = utcnow()
        with self.database.connect() as conn:
            conn.execute("UPDATE replicas SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), replica_id))

    def next_epoch(self, deployment_id: str) -> int:
        with self.database.connect() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(instance_epoch),0)+1 FROM replicas WHERE deployment_id=?", (deployment_id,)).fetchone()[0])

    def assigned_gpus(self, node_id: str) -> set[str]:
        with self.database.connect() as conn:
            rows = conn.execute("SELECT assigned_gpu_uuids_json FROM replicas WHERE node_id=? AND state NOT IN ('FAILED','RELEASED')", (node_id,))
            return {uuid for row in rows for uuid in json.loads(row[0] or "[]")}

    def ready_models(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT DISTINCT m.id,m.created_at FROM models m JOIN deployments d ON d.model_id=m.id
                JOIN replicas r ON r.deployment_id=d.id JOIN nodes n ON n.id=r.node_id WHERE d.state='ACTIVE' AND n.state='ONLINE'
                AND r.state='READY' AND r.container_state='running' AND r.endpoint_healthy=1 AND r.model_runtime_ready=1""")]

    def acquire_replica(self, model_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT r.*,d.served_model_id,d.id AS deployment_key FROM replicas r JOIN deployments d ON d.id=r.deployment_id
                JOIN nodes n ON n.id=r.node_id WHERE d.model_id=? AND d.state='ACTIVE' AND n.state='ONLINE' AND r.state='READY'
                AND r.container_state='running' AND r.endpoint_healthy=1 AND r.model_runtime_ready=1
                AND r.active_requests<r.max_concurrency ORDER BY r.active_requests,r.created_at LIMIT 1""", (model_id,)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE replicas SET active_requests=active_requests+1,queue_depth=queue_depth+1 WHERE id=?", (row["id"],))
            return decode_row(row)

    def release_replica(self, replica_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute("UPDATE replicas SET active_requests=MAX(0,active_requests-1),queue_depth=MAX(0,queue_depth-1),updated_at=? WHERE id=?", (utcnow(), replica_id))

    def route_log(self, request_id: str, deployment_id: str, replica_id: str | None, epoch: int | None, outcome: str, latency_ms: float | None, detail: str | None = None) -> None:
        with self.database.connect() as conn:
            conn.execute("INSERT INTO route_decisions VALUES(?,?,?,?,?,?,?,?)", (request_id, deployment_id, replica_id, epoch, outcome, latency_ms, detail, utcnow()))

    def archive_stale(self, *, terminal_replica_hours: float = 24,
                      deleted_deployment_hours: float = 24,
                      completed_job_hours: float = 168) -> dict[str, int]:
        """Archive operationally terminal rows without deleting audit history."""
        now = datetime.now(timezone.utc)
        replica_cutoff = (now - timedelta(hours=terminal_replica_hours)).isoformat()
        deployment_cutoff = (now - timedelta(hours=deleted_deployment_hours)).isoformat()
        job_cutoff = (now - timedelta(hours=completed_job_hours)).isoformat()
        archived_at = now.isoformat()
        with self.database.connect() as conn:
            replicas = conn.execute(
                """UPDATE replicas SET archived_at=? WHERE archived_at IS NULL
                   AND state IN ('FAILED','RELEASED') AND updated_at<=?""",
                (archived_at, replica_cutoff),
            ).rowcount
            deployments = conn.execute(
                """UPDATE deployments SET archived_at=? WHERE archived_at IS NULL
                   AND state='DELETED' AND updated_at<=?""",
                (archived_at, deployment_cutoff),
            ).rowcount
            # A deleted Deployment and every Replica below it are one history unit.
            cascaded = conn.execute(
                """UPDATE replicas SET archived_at=? WHERE archived_at IS NULL AND deployment_id IN
                   (SELECT id FROM deployments WHERE archived_at IS NOT NULL)""",
                (archived_at,),
            ).rowcount
            jobs = 0
            for table in ("scan_jobs", "transfer_jobs"):
                jobs += conn.execute(
                    f"""UPDATE {table} SET archived_at=? WHERE archived_at IS NULL
                         AND state IN ('COMPLETED','FAILED','CANCELLED') AND updated_at<=?""",
                    (archived_at, job_cutoff),
                ).rowcount
        return {"replicas": replicas + cascaded, "deployments": deployments, "jobs": jobs}

    def archive_node(self, node_id: str) -> bool:
        with self.database.connect() as conn:
            node = conn.execute("SELECT state FROM nodes WHERE id=? AND archived_at IS NULL", (node_id,)).fetchone()
            if not node:
                return False
            if node["state"] == "ONLINE":
                raise ValueError("an ONLINE node cannot be archived")
            live = conn.execute("""SELECT 1 FROM replicas r JOIN deployments d ON d.id=r.deployment_id
                WHERE r.node_id=? AND d.state IN ('ACTIVE','DELETING')
                AND r.state NOT IN ('FAILED','RELEASED') LIMIT 1""", (node_id,)).fetchone()
            if live:
                raise ValueError("node still has a live Deployment/Replica")
            return conn.execute("UPDATE nodes SET archived_at=?,updated_at=? WHERE id=?",
                                (utcnow(), utcnow(), node_id)).rowcount > 0

    def archive_model(self, model_id: str) -> bool:
        with self.database.connect() as conn:
            active = conn.execute("SELECT 1 FROM deployments WHERE model_id=? AND state IN ('ACTIVE','DELETING') LIMIT 1",
                                  (model_id,)).fetchone()
            if active:
                raise ValueError("model still has an active Deployment")
            return conn.execute("UPDATE models SET archived_at=?,updated_at=? WHERE id=? AND archived_at IS NULL",
                                (utcnow(), utcnow(), model_id)).rowcount > 0

    def archive_summary(self) -> dict[str, int]:
        with self.database.connect() as conn:
            return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE archived_at IS NOT NULL").fetchone()[0])
                    for table in ("nodes", "models", "deployments", "replicas", "scan_jobs", "transfer_jobs")}

    def catalog(self, *, include_archived: bool = False) -> dict[str, Any]:
        models = self.list_models(include_archived=include_archived)
        deployments = self.list_deployments(include_archived=include_archived)
        nodes = {n["id"]: n for n in self.list_nodes(include_archived=include_archived)}
        for model in models:
            model["deployments"] = [d for d in deployments if d["model_id"] == model["id"]]
            for deployment in model["deployments"]:
                for replica in deployment["replicas"]:
                    node = nodes.get(replica.get("node_id"))
                    replica["device"] = node
                    replica["node_online"] = bool(node and node["state"] == "ONLINE")
                    replica["container_running"] = replica.get("container_state") == "running"
                    replica["schedulable"] = bool(replica["state"] == "READY" and replica["node_online"] and replica["container_running"] and replica["endpoint_healthy"] and replica["model_runtime_ready"])
                    if not replica["schedulable"] and not replica.get("failure_reason"):
                        replica["failure_reason"] = "replica has not passed every readiness gate"
            replicas = [r for d in model["deployments"] for r in d["replicas"]]
            model["ready_replicas"] = sum(r.get("schedulable", False) for r in replicas)
            model["schedulable_capacity"] = sum(r["max_concurrency"] for r in replicas if r.get("schedulable"))
        return {"models": models, "nodes": list(nodes.values()), "storage_pools": self.list_storage_pools(),
                "artifacts": self.list_artifacts(), "runtime_profiles": self.list_runtime_profiles(),
                "scan_jobs": self.list_jobs("scan_jobs", include_archived=include_archived),
                "transfer_jobs": self.list_jobs("transfer_jobs", include_archived=include_archived),
                "archive_summary": self.archive_summary()}
