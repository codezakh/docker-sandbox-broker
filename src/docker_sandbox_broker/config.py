"""Broker configuration."""

import os
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


def default_state_dir() -> Path:
    return Path("/var/tmp") / f"docker-sandbox-broker-{os.getuid()}"


class BrokerSettings(BaseModel):
    auth_token: str = Field(min_length=16)
    broker_id: str = Field(default="default", min_length=1, max_length=64)
    allow_docker_enabled: bool = False
    max_memory_mb: int = Field(default=16_384, ge=64)
    max_cpus: float = Field(default=8.0, gt=0)
    max_upload_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_download_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    state_dir: Path = Field(default_factory=default_state_dir)
    image_gc_min_free_mb: int = Field(default=20 * 1024, ge=0)
    image_gc_target_free_mb: int = Field(default=40 * 1024, ge=0)
    image_gc_min_age_seconds: int = Field(default=300, ge=0)
    sandbox_ttl_seconds: int = Field(default=1800, ge=0)
    """Default lifetime for a sandbox whose request does not set one. 0 disables
    expiry entirely, which returns the broker to relying on clients to delete
    what they create."""
    sandbox_sweep_interval_seconds: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def image_gc_target_exceeds_minimum(self) -> "BrokerSettings":
        if self.image_gc_target_free_mb < self.image_gc_min_free_mb:
            raise ValueError("image GC target free space must be at least its minimum")
        return self

    @classmethod
    def from_environment(cls) -> "BrokerSettings":
        token = os.environ.get("DSB_AUTH_TOKEN")
        if token is None:
            raise RuntimeError("DSB_AUTH_TOKEN must be set and contain at least 16 characters")
        return cls(
            auth_token=token,
            broker_id=os.environ.get("DSB_BROKER_ID", "default"),
            allow_docker_enabled=_env_bool("DSB_ALLOW_DOCKER_ENABLED", False),
            max_memory_mb=int(os.environ.get("DSB_MAX_MEMORY_MB", "16384")),
            max_cpus=float(os.environ.get("DSB_MAX_CPUS", "8")),
            state_dir=Path(os.environ.get("DSB_STATE_DIR", default_state_dir())),
            image_gc_min_free_mb=int(os.environ.get("DSB_IMAGE_GC_MIN_FREE_MB", str(20 * 1024))),
            image_gc_target_free_mb=int(
                os.environ.get("DSB_IMAGE_GC_TARGET_FREE_MB", str(40 * 1024))
            ),
            image_gc_min_age_seconds=int(os.environ.get("DSB_IMAGE_GC_MIN_AGE_SECONDS", "300")),
            sandbox_ttl_seconds=int(os.environ.get("DSB_SANDBOX_TTL_SECONDS", "1800")),
            sandbox_sweep_interval_seconds=int(
                os.environ.get("DSB_SANDBOX_SWEEP_INTERVAL_SECONDS", "60")
            ),
        )


class ServerSettings(BaseModel):
    uds: Path | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
