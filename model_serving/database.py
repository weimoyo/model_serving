from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                    agent_url TEXT NOT NULL, agent_key_env TEXT NOT NULL,
                    platform TEXT, network_zone TEXT, state TEXT NOT NULL,
                    inventory_json TEXT NOT NULL, last_seen_at TEXT,
                    failure_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, family TEXT,
                    task TEXT NOT NULL, capabilities_json TEXT NOT NULL,
                    repository TEXT NOT NULL, revision TEXT NOT NULL,
                    parameter_count INTEGER, precision TEXT, context_length INTEGER,
                    license TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL REFERENCES models(id),
                    served_model_id TEXT NOT NULL, runtime TEXT NOT NULL,
                    runtime_image TEXT NOT NULL, runtime_args_json TEXT NOT NULL,
                    gpu_count_per_replica INTEGER NOT NULL CHECK(gpu_count_per_replica > 0),
                    cpu_cores_per_replica INTEGER, memory_gib_per_replica INTEGER,
                    max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrency > 0),
                    desired_replicas INTEGER NOT NULL CHECK(desired_replicas >= 0),
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replicas (
                    id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
                    node_id TEXT REFERENCES nodes(id), runtime_id TEXT,
                    instance_epoch INTEGER NOT NULL CHECK(instance_epoch > 0),
                    state TEXT NOT NULL, endpoint TEXT, ssh_port INTEGER,
                    container_state TEXT, assigned_gpu_uuids_json TEXT NOT NULL,
                    queue_depth INTEGER NOT NULL DEFAULT 0 CHECK(queue_depth >= 0),
                    active_requests INTEGER NOT NULL DEFAULT 0 CHECK(active_requests >= 0),
                    max_concurrency INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrency > 0),
                    endpoint_healthy INTEGER NOT NULL DEFAULT 0,
                    model_runtime_ready INTEGER NOT NULL DEFAULT 0,
                    last_health_at TEXT, failure_reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS replicas_deployment_state ON replicas(deployment_id,state);
                CREATE TABLE IF NOT EXISTS route_decisions (
                    request_id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL,
                    replica_id TEXT, instance_epoch INTEGER, outcome TEXT NOT NULL,
                    latency_ms REAL, detail TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS storage_pools (
                    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                    host_path TEXT NOT NULL, container_path TEXT NOT NULL,
                    purpose TEXT NOT NULL, agent_access TEXT NOT NULL,
                    runtime_access TEXT NOT NULL, quota_bytes INTEGER,
                    min_free_bytes INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, capacity_bytes INTEGER, free_bytes INTEGER,
                    used_bytes INTEGER, failure_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(node_id, host_path)
                );
                CREATE INDEX IF NOT EXISTS storage_pools_node ON storage_pools(node_id,state);
                CREATE TABLE IF NOT EXISTS model_artifacts (
                    id TEXT PRIMARY KEY, model_id TEXT REFERENCES models(id),
                    repository TEXT, revision TEXT, format TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, checksum TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_locations (
                    id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES model_artifacts(id),
                    node_id TEXT NOT NULL REFERENCES nodes(id),
                    storage_pool_id TEXT NOT NULL REFERENCES storage_pools(id),
                    relative_path TEXT NOT NULL, state TEXT NOT NULL,
                    last_verified_at TEXT, failure_reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(artifact_id,node_id,storage_pool_id,relative_path)
                );
                CREATE INDEX IF NOT EXISTS artifact_locations_ready ON artifact_locations(artifact_id,node_id,state);
                CREATE TABLE IF NOT EXISTS runtime_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, runtime TEXT NOT NULL,
                    image TEXT NOT NULL, architectures_json TEXT NOT NULL,
                    accelerator_kinds_json TEXT NOT NULL, allowed_args_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfer_jobs (
                    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                    storage_pool_id TEXT NOT NULL REFERENCES storage_pools(id),
                    agent_job_id TEXT NOT NULL, kind TEXT NOT NULL,
                    repository TEXT, revision TEXT, relative_path TEXT,
                    state TEXT NOT NULL, progress_json TEXT NOT NULL,
                    artifact_id TEXT, failure_reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(node_id,agent_job_id)
                );
                CREATE TABLE IF NOT EXISTS scan_jobs (
                    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                    storage_pool_id TEXT NOT NULL REFERENCES storage_pools(id),
                    agent_job_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                    state TEXT NOT NULL, progress_json TEXT NOT NULL,
                    result_json TEXT NOT NULL, failure_reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(node_id,agent_job_id)
                );
                CREATE TABLE IF NOT EXISTS enrollment_tokens (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    uses_remaining INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS enrollment_tokens_active
                    ON enrollment_tokens(expires_at,uses_remaining,revoked_at);
                """
            )
            # Additive migrations keep existing standalone installations usable.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(deployments)")}
            for name, sql_type in (
                ("artifact_id", "TEXT"),
                ("runtime_profile_id", "TEXT"),
                ("workspace_storage_pool_id", "TEXT"),
            ):
                if name not in existing:
                    conn.execute(f"ALTER TABLE deployments ADD COLUMN {name} {sql_type}")
            pool_columns = {row[1] for row in conn.execute("PRAGMA table_info(storage_pools)")}
            if "used_bytes" not in pool_columns:
                conn.execute("ALTER TABLE storage_pools ADD COLUMN used_bytes INTEGER")
            # Archival is additive and preserves audit/history rows while keeping
            # operational queries and the Dashboard focused on live resources.
            for table in ("nodes", "models", "deployments", "replicas", "scan_jobs", "transfer_jobs"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "archived_at" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN archived_at TEXT")


def decode_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in ("inventory_json", "capabilities_json", "runtime_args_json", "assigned_gpu_uuids_json",
                "metadata_json", "architectures_json", "accelerator_kinds_json", "allowed_args_json",
                "progress_json", "result_json"):
        if key in value:
            raw = value.pop(key)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else ([] if "uuids" in key else {})
    for key in ("endpoint_healthy", "model_runtime_ready", "enabled"):
        if key in value:
            value[key] = bool(value[key])
    return value
