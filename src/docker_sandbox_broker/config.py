"""Broker configuration."""

import os
from pathlib import Path

from pydantic import BaseModel, Field


class BrokerSettings(BaseModel):
    auth_token: str = Field(min_length=16)
    broker_id: str = Field(default="default", min_length=1, max_length=64)
    allow_docker_enabled: bool = False
    max_memory_mb: int = Field(default=16_384, ge=64)
    max_cpus: float = Field(default=8.0, gt=0)
    max_upload_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_download_bytes: int = Field(default=128 * 1024 * 1024, ge=1024)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)

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
