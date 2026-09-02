"""Harbor custom environment backed by the local sandbox broker.

This module is intentionally optional: importing the core broker package does
not require Harbor. Configure Harbor with the fully-qualified class path
``docker_sandbox_broker.harbor:LocalBrokerEnvironment``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path, PurePosixPath
from uuid import uuid4

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    effective_exec_cwd,
    parse_dockerfile_workdir,
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
)

from docker_sandbox_broker.client import BrokerClient, Sandbox
from docker_sandbox_broker.models import CreateSandboxRequest, ResourceLimits


class LocalBrokerEnvironment(BaseEnvironment):
    """Run a single-container Harbor task through the host broker."""

    def __init__(self, *args, **kwargs):
        token = os.environ.get("DSB_AUTH_TOKEN")
        if not token:
            raise RuntimeError("DSB_AUTH_TOKEN must be set for the local broker")
        self._client = BrokerClient(
            token,
            base_url=os.environ.get("DSB_URL", "http://localhost"),
            uds=os.environ.get("DSB_SOCKET"),
        )
        self._sandbox: Sandbox | None = None
        self._dockerfile_workdir: str | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> str:
        return "local_broker"

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities()

    def _validate_definition(self) -> None:
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise NotImplementedError(
                "LocalBrokerEnvironment currently supports Dockerfile and prebuilt-image "
                "tasks; Docker Compose support will use the broker's Docker-enabled mode"
            )
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )

    async def start(self, force_build: bool) -> None:
        docker_image = self.task_env_config.docker_image
        use_prebuilt = should_use_prebuilt_docker_image(
            self.environment_dir,
            docker_image=docker_image,
            force_build=force_build,
        )
        if use_prebuilt and docker_image:
            image = docker_image
        else:
            dockerfile = self.environment_dir / "Dockerfile"
            self._dockerfile_workdir = parse_dockerfile_workdir(dockerfile)
            context = pack_dir_to_bytes(self.environment_dir, compress=False).getvalue()
            image = await asyncio.to_thread(self._client.build_image, context)

        request = CreateSandboxRequest(
            image=image,
            environment=self._startup_env(),
            resources=ResourceLimits(
                cpus=self._effective_cpus,
                memory_mb=self._effective_memory_mb,
            ),
            run_id=self.session_id,
        )
        self._sandbox = await asyncio.to_thread(self._client.create, request)

        if workdir := self.task_env_config.workdir:
            await self.exec(f"mkdir -p {shlex.quote(workdir)}", user="root")
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    async def stop(self, delete: bool) -> None:
        if self._sandbox is not None:
            await asyncio.to_thread(self._sandbox.delete)
            self._sandbox = None
        self._client.close()

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        content = await asyncio.to_thread(Path(source_path).read_bytes)
        await asyncio.to_thread(self._active_sandbox().fs.upload_file, content, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        archive = await asyncio.to_thread(
            lambda: pack_dir_to_bytes(source_dir, compress=False).getvalue()
        )
        await asyncio.to_thread(
            self._active_sandbox().fs.upload_archive,
            archive,
            target_dir,
        )

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        content = await asyncio.to_thread(
            self._active_sandbox().fs.download_file,
            source_path,
        )
        await asyncio.to_thread(Path(target_path).write_bytes, content)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        archive_path = str(PurePosixPath("/tmp") / f".harbor-download-{uuid4().hex}.tar.gz")
        packed = await self.exec(
            remote_pack_command(source_dir, archive_path),
            timeout_sec=120,
            user="root",
        )
        if packed.return_code != 0:
            raise RuntimeError(packed.stderr or packed.stdout or "directory archive failed")
        try:
            archive = await asyncio.to_thread(
                self._active_sandbox().fs.download_file,
                archive_path,
            )
            await asyncio.to_thread(extract_dir_from_bytes, archive, target_dir)
        finally:
            await self.exec(f"rm -f {shlex.quote(archive_path)}", user="root")

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        result = await asyncio.to_thread(
            self._active_sandbox().process.exec,
            command,
            timeout=timeout_sec,
            cwd=effective_exec_cwd(
                cwd,
                self.task_env_config.workdir,
                self._dockerfile_workdir,
            ),
            env=self._merge_env(env),
            user=str(user) if user is not None else None,
        )
        return ExecResult(
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.exit_code,
        )

    def _active_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("Local broker sandbox has not been started")
        return self._sandbox
