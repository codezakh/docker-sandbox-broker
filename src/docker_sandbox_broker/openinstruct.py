"""OpenInstruct sandbox backend backed by the local broker.

OpenInstruct is an optional consumer. Import this module only from an
environment where OpenInstruct is installed.
"""

from __future__ import annotations

import os
import re

from open_instruct.environments.backends import (
    ExecutionResult,
    SandboxBackend,
    SandboxOOMError,
)

from docker_sandbox_broker.client import BrokerClient, Sandbox
from docker_sandbox_broker.logging import get_logger
from docker_sandbox_broker.models import CreateSandboxRequest, ResourceLimits


class LocalBrokerBackend(SandboxBackend):
    """Run an OpenInstruct sandbox through the host Docker broker."""

    def __init__(
        self,
        image: str,
        timeout: int = 120,
        mem_limit: str = "4g",
        **_kwargs,
    ):
        token = os.environ.get("DSB_AUTH_TOKEN")
        if not token:
            raise RuntimeError("DSB_AUTH_TOKEN must be set for the local broker backend")
        self._token = token
        self._socket_path = os.environ.get("DSB_SOCKET")
        self._base_url = os.environ.get("DSB_URL", "http://localhost")
        self._image = image
        self._timeout = timeout
        self._memory_mb = self._parse_memory_mb(mem_limit)
        self._client: BrokerClient | None = None
        self._sandbox: Sandbox | None = None
        self._log = get_logger().bind(component="openinstruct_backend")

    @staticmethod
    def _parse_memory_mb(mem_limit: str | None) -> int:
        if not mem_limit:
            return 4096
        match = re.fullmatch(r"\s*(\d+)\s*([gGmM]?)\s*", str(mem_limit))
        if not match:
            raise ValueError(f"invalid broker memory limit: {mem_limit!r}")
        value, unit = int(match.group(1)), match.group(2).lower()
        return value * 1024 if unit == "g" else value

    def start(self) -> None:
        if self._sandbox is not None:
            raise RuntimeError("Local broker sandbox is already started")
        client = BrokerClient(
            self._token,
            base_url=self._base_url,
            uds=self._socket_path,
        )
        request = CreateSandboxRequest(
            image=self._image,
            resources=ResourceLimits(memory_mb=self._memory_mb, cpus=1),
            run_id="open-instruct-rl",
        )
        try:
            sandbox = client.create(request)
        except Exception:
            client.close()
            raise
        self._client = client
        self._sandbox = sandbox
        self._log.info("sandbox_ready", sandbox_id=sandbox.id)

    def close(self) -> None:
        sandbox = self._sandbox
        client = self._client
        self._sandbox = None
        self._client = None
        try:
            if sandbox is not None:
                sandbox.delete()
        except Exception as error:
            self._log.warning(
                "sandbox_delete_failed",
                sandbox_id=sandbox.id,
                error=str(error),
            )
        finally:
            if client is not None:
                client.close()

    def run_command(self, command: str, timeout: int | None = None) -> ExecutionResult:
        sandbox = self._require_sandbox()
        result = sandbox.process.exec(command, timeout=timeout or self._timeout)
        if result.oom_killed:
            raise SandboxOOMError(f"Local broker sandbox {sandbox.id} was OOM-killed")
        return ExecutionResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )

    def write_file(self, path: str, content: str | bytes) -> None:
        self._require_sandbox().fs.upload_file(content, path)

    def read_file(self, path: str, binary: bool = False) -> str | bytes:
        return self._require_sandbox().fs.download_file(path, binary=binary)

    def put_archive(self, root: str, tar_bytes: bytes) -> None:
        self._require_sandbox().fs.upload_archive(tar_bytes, root)

    def _require_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("Local broker sandbox not started. Call start() first.")
        return self._sandbox
