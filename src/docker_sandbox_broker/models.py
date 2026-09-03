"""Public API models shared by the broker and its Python client."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class SandboxState(StrEnum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"


class SandboxFeatures(BaseModel):
    docker: bool = False


class ResourceLimits(BaseModel):
    cpus: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, ge=64)


class CreateSandboxRequest(BaseModel):
    image: str = Field(min_length=1, max_length=512)
    command: list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    features: SandboxFeatures = Field(default_factory=SandboxFeatures)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    run_id: str | None = Field(default=None, max_length=128)
    ttl_seconds: int | None = Field(default=None, ge=1)
    """Delete this sandbox once it is this old, whatever the client does.

    A client that exits without deleting its sandboxes leaks them; nothing else
    reclaims them. Omitted, the broker applies its configured default. Set it
    above the longest task this sandbox will run, because the deadline is
    absolute and is not extended by activity."""

    @field_validator("command")
    @classmethod
    def command_is_not_empty(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or any(not part for part in value)):
            raise ValueError("command must contain non-empty arguments")
        return value


class SandboxView(BaseModel):
    id: str
    image: str
    state: SandboxState
    run_id: str | None
    docker_enabled: bool
    created_at: datetime
    expires_at: datetime | None = None
    """When the broker will delete this sandbox regardless of client activity,
    or None when expiry is disabled."""


class ExecRequest(BaseModel):
    command: str = Field(min_length=1)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    cwd: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    user: str | None = None


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    protocol_version: str = "v1"
    broker_id: str
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BuildImageResponse(BaseModel):
    image: str


class ErrorResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False
