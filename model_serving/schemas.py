from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator, model_validator


ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


class NodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    agent_url: HttpUrl
    agent_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    platform: str | None = None
    network_zone: str | None = None


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    family: str | None = None
    task: Literal["chat", "completion", "embedding", "rerank", "other"] = "chat"
    capabilities: list[str] = Field(default_factory=lambda: ["chat"])
    repository: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    parameter_count: int | None = Field(default=None, ge=0)
    precision: str | None = None
    context_length: int | None = Field(default=None, ge=1)
    license: str | None = None

    @field_validator("revision")
    @classmethod
    def immutable_revision(cls, value: str) -> str:
        if value.lower() in {"main", "master", "latest", "head"}:
            raise ValueError("revision must be immutable")
        return value


class StoragePoolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID_PATTERN)
    host_path: str = Field(min_length=1, max_length=1000)
    container_path: str = Field(min_length=1, max_length=500)
    purpose: Literal["model", "workspace", "cache", "output"]
    agent_access: Literal["ro", "rw"]
    runtime_access: Literal["none", "ro", "rw"]
    quota_gib: float | None = Field(default=None, gt=0)
    min_free_gib: float = Field(default=1, ge=0)


class EnrollmentTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_minutes: int = Field(default=60, ge=5, le=1440)
    uses: int = Field(default=1, ge=1, le=20)


class ScanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_pool_id: str = Field(pattern=ID_PATTERN)
    relative_path: str = Field(default=".", min_length=1, max_length=1000)


class DownloadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_pool_id: str = Field(pattern=ID_PATTERN)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    relative_path: str | None = Field(default=None, max_length=1000)

    @field_validator("revision")
    @classmethod
    def immutable_revision(cls, value: str) -> str:
        if value.lower() in {"main", "master", "latest", "head"}:
            raise ValueError("revision must be immutable")
        return value


class RuntimeProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    runtime: Literal["vllm", "mlx"]
    image: str = Field(min_length=1, max_length=500)
    architectures: list[str] = Field(min_length=1)
    accelerator_kinds: list[str] = Field(default_factory=list)
    allowed_args: list[str] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def immutable_container_image(self) -> "RuntimeProfileCreate":
        digest = self.image.rsplit("@sha256:", 1)[-1] if "@sha256:" in self.image else self.image.removeprefix("sha256:")
        immutable = len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest)
        if self.runtime == "vllm" and not immutable:
            raise ValueError("vLLM runtime profile image must use an immutable sha256 digest")
        return self


class NodeEnrollmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_token: SecretStr
    node: NodeCreate
    agent_key: SecretStr = Field(min_length=32, max_length=512)
    storage_pools: list[StoragePoolCreate] = Field(default_factory=list, max_length=16)
    runtime_profiles: list[RuntimeProfileCreate] = Field(default_factory=list, max_length=8)
    scan_existing_models: bool = True

    @model_validator(mode="after")
    def unique_resources(self) -> "NodeEnrollmentCreate":
        pool_ids = [item.id for item in self.storage_pools]
        profile_ids = [item.id for item in self.runtime_profiles]
        if len(pool_ids) != len(set(pool_ids)):
            raise ValueError("storage pool IDs must be unique")
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("runtime profile IDs must be unique")
        if str(self.node.agent_url.host).strip("[]") != self.node.host.strip("[]"):
            raise ValueError("node host must match the Agent URL host")
        return self


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    model_id: str = Field(pattern=ID_PATTERN)
    served_model_id: str = Field(min_length=1, max_length=256)
    desired_replicas: int = Field(default=1, ge=1, le=1)
    artifact_id: str | None = Field(default=None, pattern=ID_PATTERN)
    runtime_profile_id: str | None = Field(default=None, pattern=ID_PATTERN)
    workspace_storage_pool_id: str | None = Field(default=None, pattern=ID_PATTERN)
    # Legacy direct-repository deployments remain readable, but new managed
    # deployments use artifact/profile/pool IDs and never accept host paths.
    runtime: Literal["vllm", "mlx"] = "vllm"
    runtime_image: str | None = Field(default=None, min_length=1)
    runtime_args: dict[str, Any] = Field(default_factory=dict)
    gpu_count_per_replica: int = Field(default=1, ge=1)
    cpu_cores_per_replica: int | None = Field(default=None, ge=1)
    memory_gib_per_replica: int | None = Field(default=None, ge=1)
    max_concurrency: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def complete_managed_reference(self) -> "DeploymentCreate":
        managed = (self.artifact_id, self.runtime_profile_id, self.workspace_storage_pool_id)
        if any(managed) and not all(managed):
            raise ValueError("artifact_id, runtime_profile_id and workspace_storage_pool_id must be supplied together")
        if not all(managed) and not self.runtime_image:
            raise ValueError("managed resource IDs or runtime_image are required")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1)
    messages: list[dict[str, Any]]
    stream: bool = False
