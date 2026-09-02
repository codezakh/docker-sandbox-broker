"""Harbor custom environment backed by the local sandbox broker.

Importing the core broker package does not require Harbor. Configure Harbor
with ``docker_sandbox_broker.harbor:LocalBrokerEnvironment``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml
from harbor.constants import MAIN_SERVICE_NAME
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.compose_service_ops import ComposeServiceOpsMixin
from harbor.environments.definition import (
    effective_exec_cwd,
    parse_dockerfile_workdir,
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.environments.dind_compose import DinDComposeOps
from harbor.environments.docker import (
    COMPOSE_BUILD_PATH,
    COMPOSE_NO_NETWORK_PATH,
    COMPOSE_PREBUILT_PATH,
    ENV_COMPOSE_NAME,
    RESOURCES_COMPOSE_NAME,
    self_bind_mount,
    write_env_compose_file,
    write_mounts_compose_file,
    write_resources_compose_file,
)
from harbor.environments.docker.compose_env import ComposeInfraEnvVars, merge_compose_env
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
)
from harbor.models.trial.config import ResourceMode

from docker_sandbox_broker.client import BrokerClient, Sandbox
from docker_sandbox_broker.models import CreateSandboxRequest, ResourceLimits, SandboxFeatures


class _BrokerCompose(DinDComposeOps):
    """Harbor's shared Compose operations over a broker Docker-enabled sandbox."""

    _COMPOSE_DIR = "/harbor/compose"
    _ENVIRONMENT_DIR = "/harbor/environment"
    _MOUNTS_COMPOSE_NAME = "docker-compose-mounts.json"
    _HOST_NETWORK_COMPOSE_NAME = "docker-compose-host-network.yaml"
    _DOCKER_DAEMON_POLL_LIMIT = 30

    def __init__(self, env: LocalBrokerEnvironment):
        self._env = env
        self._use_prebuilt = False

    async def start(self, force_build: bool) -> None:
        await self._create_controller()
        await self._wait_for_docker_daemon()
        await self._host_exec(
            f"mkdir -p {self._COMPOSE_DIR} {self._ENVIRONMENT_DIR}", timeout_sec=10
        )
        await self._stage_harbor_overlays()
        await self._stage_dir_to_host(self._env.environment_dir, self._ENVIRONMENT_DIR)
        await self._stage_extra_compose_files()
        await self._stage_mounts()
        self._use_prebuilt = should_use_prebuilt_docker_image(
            self._env.environment_dir,
            docker_image=self._env.task_env_config.docker_image,
            force_build=force_build,
        )
        await self._stage_host_network_overlay()
        built = await self._compose_exec(
            ["build"], timeout_sec=round(self._env.task_env_config.build_timeout_sec)
        )
        self._require_success(built, "docker compose build")
        started = await self._compose_exec(["up", "-d"], timeout_sec=120)
        self._require_success(started, "docker compose up")
        await self._wait_for_main_container()

    async def stop(self) -> None:
        if self._env._sandbox is not None:
            try:
                await self._compose_exec(["down", "--remove-orphans"], timeout_sec=30)
            except Exception as error:
                self._env.logger.warning("docker compose down failed: %s", error)
        await self._env._delete_controller()

    async def _create_controller(self) -> None:
        configured_memory = self._env._effective_memory_mb or 1024
        request = CreateSandboxRequest(
            image=os.environ.get("DSB_DIND_IMAGE", "docker:24-dind"),
            features=SandboxFeatures(docker=True),
            resources=ResourceLimits(
                cpus=self._env._effective_cpus,
                memory_mb=max(1024, configured_memory),
            ),
            run_id=self._env.session_id,
        )
        self._env._sandbox = await asyncio.to_thread(self._env._client.create, request)

    async def _stage_harbor_overlays(self) -> None:
        for path in (COMPOSE_BUILD_PATH, COMPOSE_PREBUILT_PATH, COMPOSE_NO_NETWORK_PATH):
            await self._stage_file_to_host(path, f"{self._COMPOSE_DIR}/{path.name}")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            resources = temp / RESOURCES_COMPOSE_NAME
            write_resources_compose_file(
                resources,
                cpu_request=self._env._resource_request_value("cpu", auto_mode=ResourceMode.LIMIT),
                cpu_limit=self._env._resource_limit_value("cpu", auto_mode=ResourceMode.LIMIT),
                memory_request_mb=self._env._resource_request_value(
                    "memory", auto_mode=ResourceMode.LIMIT
                ),
                memory_limit_mb=self._env._resource_limit_value(
                    "memory", auto_mode=ResourceMode.LIMIT
                ),
            )
            environment = temp / ENV_COMPOSE_NAME
            write_env_compose_file(environment, self._env._startup_env())
            await self._stage_file_to_host(
                resources, f"{self._COMPOSE_DIR}/{RESOURCES_COMPOSE_NAME}"
            )
            await self._stage_file_to_host(environment, f"{self._COMPOSE_DIR}/{ENV_COMPOSE_NAME}")

    async def _stage_extra_compose_files(self) -> None:
        for index, source in enumerate(self._env.extra_docker_compose_paths):
            target = f"{self._COMPOSE_DIR}/docker-compose-extra-{index}.yaml"
            await self._stage_file_to_host(source, target)

    async def _stage_mounts(self) -> None:
        volumes = [
            self_bind_mount(mount) if mount.get("type") == "bind" else mount
            for mount in self._env._mounts
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / self._MOUNTS_COMPOSE_NAME
            write_mounts_compose_file(local_path, volumes)
            await self._stage_file_to_host(
                local_path, f"{self._COMPOSE_DIR}/{self._MOUNTS_COMPOSE_NAME}"
            )
        bind_sources = [mount["source"] for mount in volumes if mount.get("type") == "bind"]
        if bind_sources:
            paths = " ".join(shlex.quote(path) for path in bind_sources)
            await self._host_exec(f"mkdir -p {paths} && chmod 777 {paths}")

    async def _stage_host_network_overlay(self) -> None:
        services = self._compose_services()
        overlay = {
            "services": {
                service: self._host_network_service(service, has_build, services)
                for service, has_build in services.items()
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / self._HOST_NETWORK_COMPOSE_NAME
            local_path.write_text(yaml.safe_dump(overlay, sort_keys=False))
            await self._stage_file_to_host(
                local_path,
                f"{self._COMPOSE_DIR}/{self._HOST_NETWORK_COMPOSE_NAME}",
            )

    @staticmethod
    def _host_network_service(
        service: str, has_build: bool, services: dict[str, bool]
    ) -> dict[str, object]:
        config: dict[str, object] = {"network_mode": "host"}
        peers = [peer for peer in services if peer != service]
        if peers:
            config["extra_hosts"] = [f"{peer}:127.0.0.1" for peer in peers]
        if has_build:
            config["build"] = {"network": "host"}
        return config

    def _compose_services(self) -> dict[str, bool]:
        services = {}
        paths = [
            self._env.environment_dir / "docker-compose.yaml",
            *self._env.extra_docker_compose_paths,
        ]
        for path in paths:
            if not path.exists():
                continue
            document = yaml.safe_load(path.read_text()) or {}
            for name, config in document.get("services", {}).items():
                has_build = isinstance(config, dict) and "build" in config
                services[name] = services.get(name, False) or has_build
        services[MAIN_SERVICE_NAME] = not self._use_prebuilt
        return services

    async def _wait_for_docker_daemon(self) -> None:
        last_output = ""
        for _ in range(self._DOCKER_DAEMON_POLL_LIMIT):
            result = await self._host_exec("docker info", timeout_sec=10)
            if result.return_code == 0:
                return
            last_output = (result.stdout or "") + (result.stderr or "")
            await asyncio.sleep(2)
        raise RuntimeError(f"Docker daemon did not become ready: {last_output}")

    async def _wait_for_main_container(self) -> None:
        for _ in range(30):
            result = await self._compose_exec(
                ["exec", "-T", MAIN_SERVICE_NAME, "true"], timeout_sec=10
            )
            if result.return_code == 0:
                return
            await asyncio.sleep(2)
        raise RuntimeError("Compose main service did not become ready")

    async def _compose_exec(
        self, subcommand: list[str], timeout_sec: int | None = None
    ) -> ExecResult:
        return await self._env._host_exec(
            self._compose_command(subcommand),
            timeout_sec=timeout_sec,
            env=self._compose_environment(),
        )

    async def _host_exec(self, command: str, timeout_sec: int | None = None) -> ExecResult:
        return await self._env._host_exec(command, timeout_sec=timeout_sec)

    async def _stage_file_to_host(self, source_path: Path | str, host_path: str) -> None:
        await self._env._host_upload_file(source_path, host_path)

    async def _stage_dir_to_host(self, source_dir: Path | str, host_dir: str) -> None:
        await self._env._host_upload_dir(source_dir, host_dir)

    async def _fetch_file_from_host(self, host_path: str, target_path: Path | str) -> None:
        await self._env._host_download_file(host_path, target_path)

    async def _fetch_dir_from_host(self, host_dir: str, target_dir: Path | str) -> None:
        await self._env._host_download_dir(host_dir, target_dir)

    def _compose_command(self, subcommand: list[str]) -> str:
        parts = [
            "docker",
            "compose",
            "-p",
            self._env.session_id.lower().replace(".", "-"),
            "--project-directory",
            self._ENVIRONMENT_DIR,
            *self._compose_file_flags(),
            *subcommand,
        ]
        return shlex.join(parts)

    def _compose_file_flags(self) -> list[str]:
        mode_file = (
            "docker-compose-prebuilt.yaml" if self._use_prebuilt else "docker-compose-build.yaml"
        )
        files = [
            f"{self._COMPOSE_DIR}/{RESOURCES_COMPOSE_NAME}",
            f"{self._COMPOSE_DIR}/{mode_file}",
            f"{self._COMPOSE_DIR}/{self._MOUNTS_COMPOSE_NAME}",
        ]
        if (self._env.environment_dir / "docker-compose.yaml").exists():
            files.append(f"{self._ENVIRONMENT_DIR}/docker-compose.yaml")
        files.extend(
            f"{self._COMPOSE_DIR}/docker-compose-extra-{index}.yaml"
            for index, _ in enumerate(self._env.extra_docker_compose_paths)
        )
        files.append(f"{self._COMPOSE_DIR}/{ENV_COMPOSE_NAME}")
        files.append(f"{self._COMPOSE_DIR}/{self._HOST_NETWORK_COMPOSE_NAME}")
        if self._env._network_disabled:
            files.append(f"{self._COMPOSE_DIR}/{COMPOSE_NO_NETWORK_PATH.name}")
        return [part for path in files for part in ("-f", path)]

    def _compose_environment(self) -> dict[str, str]:
        referenced = self._referenced_environment()
        referenced.update(self._env._startup_env())
        infrastructure = ComposeInfraEnvVars(
            main_image_name=f"hb__{self._env.environment_name}",
            context_dir=self._ENVIRONMENT_DIR,
            prebuilt_image_name=self._env.task_env_config.docker_image
            if self._use_prebuilt
            else None,
            cpus=self._env._effective_cpus,
            memory=f"{self._env._effective_memory_mb}M" if self._env._effective_memory_mb else None,
        ).to_env_dict()
        return merge_compose_env(
            user_env=referenced, infra_env=infrastructure, logger=self._env.logger
        )

    def _referenced_environment(self) -> dict[str, str]:
        paths = [
            self._env.environment_dir / "docker-compose.yaml",
            *self._env.extra_docker_compose_paths,
        ]
        content = "\n".join(path.read_text() for path in paths if path.exists())
        matches = re.findall(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)\b",
            content,
        )
        names = {first or second for first, second in matches}
        return {name: os.environ[name] for name in names if name in os.environ}

    @staticmethod
    def _require_success(result: ExecResult, operation: str) -> None:
        if result.return_code != 0:
            raise RuntimeError(f"{operation} failed: {result.stdout} {result.stderr}")


class LocalBrokerEnvironment(ComposeServiceOpsMixin, BaseEnvironment):
    """Run direct and Docker Compose Harbor tasks through the host broker."""

    def __init__(self, *args, **kwargs):
        token = os.environ.get("DSB_AUTH_TOKEN")
        if not token:
            raise RuntimeError("DSB_AUTH_TOKEN must be set for the local broker")
        environment_dir = Path(kwargs.get("environment_dir", args[0] if args else "."))
        extra_compose = kwargs.get("extra_docker_compose") or []
        self._compose_mode = bool(
            (environment_dir / "docker-compose.yaml").exists() or extra_compose
        )
        self._client = BrokerClient(
            token,
            base_url=os.environ.get("DSB_URL", "http://localhost"),
            uds=os.environ.get("DSB_SOCKET"),
        )
        self._sandbox: Sandbox | None = None
        self._dockerfile_workdir: str | None = None
        self._compose: _BrokerCompose | None = None
        super().__init__(*args, **kwargs)
        if self._compose_mode:
            self._compose = _BrokerCompose(self)

    @staticmethod
    def type() -> str:
        return "local_broker"

    @classmethod
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(docker_compose=True)

    @property
    def _uses_compose(self) -> bool:
        return self._compose_mode

    def _validate_definition(self) -> None:
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            extra_docker_compose_paths=self.extra_docker_compose_paths,
        )

    async def start(self, force_build: bool) -> None:
        if self._compose is not None:
            await self._compose.start(force_build)
            return
        await self._start_direct(force_build)

    async def _start_direct(self, force_build: bool) -> None:
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
            image = await asyncio.to_thread(
                self._client.build_image,
                context,
                timeout=self.task_env_config.build_timeout_sec + 30,
            )
        request = CreateSandboxRequest(
            image=image,
            environment=self._startup_env(),
            resources=ResourceLimits(
                cpus=self._effective_cpus, memory_mb=self._effective_memory_mb
            ),
            run_id=self.session_id,
        )
        self._sandbox = await asyncio.to_thread(self._client.create, request)
        if workdir := self.task_env_config.workdir:
            await self.exec(f"mkdir -p {shlex.quote(workdir)}", user="root")
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    async def stop(self, delete: bool) -> None:
        if self._compose is not None:
            await self._compose.stop()
        else:
            await self._delete_controller()
        self._client.close()

    async def _delete_controller(self) -> None:
        if self._sandbox is not None:
            await asyncio.to_thread(self._sandbox.delete)
            self._sandbox = None

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        if self._compose is not None:
            await self._compose.upload_file(source_path, target_path)
            return
        await self._host_upload_file(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        if self._compose is not None:
            await self._compose.upload_dir(source_dir, target_dir)
            return
        await self._host_upload_dir(source_dir, target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if self._compose is not None:
            await self._compose.download_file(source_path, target_path)
            return
        await self._host_download_file(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if self._compose is not None:
            await self._compose.download_dir(source_dir, target_dir)
            return
        await self._host_download_dir(source_dir, target_dir)

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        effective_cwd = effective_exec_cwd(
            cwd, self.task_env_config.workdir, self._dockerfile_workdir
        )
        merged_env = self._merge_env(env)
        if self._compose is not None:
            return await self._compose.exec(
                command, cwd=effective_cwd, env=merged_env, timeout_sec=timeout_sec, user=user
            )
        return await self._host_exec(
            command, cwd=effective_cwd, env=merged_env, timeout_sec=timeout_sec, user=user
        )

    def _compose_service_transport(self, service: str | None):
        if self._compose is None:
            raise self._compose_unsupported(service)
        return self._compose

    async def _host_exec(
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
            cwd=cwd,
            env=env,
            user=str(user) if user is not None else None,
        )
        return ExecResult(stdout=result.stdout, stderr=result.stderr, return_code=result.exit_code)

    async def _host_upload_file(self, source_path: Path | str, target_path: str) -> None:
        parent = str(PurePosixPath(target_path).parent)
        created = await self._host_exec(f"mkdir -p {shlex.quote(parent)}", user="root")
        self._require_host_success(created, "target directory creation")
        content = await asyncio.to_thread(Path(source_path).read_bytes)
        await asyncio.to_thread(self._active_sandbox().fs.upload_file, content, target_path)

    async def _host_upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        archive = await asyncio.to_thread(
            lambda: pack_dir_to_bytes(source_dir, compress=False).getvalue()
        )
        archive_path = f"/tmp/.harbor-upload-{uuid4().hex}.tar"
        await asyncio.to_thread(
            self._active_sandbox().fs.upload_file,
            archive,
            archive_path,
        )
        try:
            unpacked = await self._host_exec(
                f"mkdir -p {shlex.quote(target_dir)} && "
                f"tar -xf {shlex.quote(archive_path)} -C {shlex.quote(target_dir)}",
                timeout_sec=120,
                user="root",
            )
            self._require_host_success(unpacked, "directory upload extraction")
        finally:
            await self._host_exec(f"rm -f {shlex.quote(archive_path)}", user="root")

    async def _host_download_file(self, source_path: str, target_path: Path | str) -> None:
        content = await asyncio.to_thread(self._active_sandbox().fs.download_file, source_path)
        await asyncio.to_thread(Path(target_path).write_bytes, content)

    async def _host_download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        archive_path = str(PurePosixPath("/tmp") / f".harbor-download-{uuid4().hex}.tar.gz")
        packed = await self._host_exec(
            remote_pack_command(source_dir, archive_path), timeout_sec=120, user="root"
        )
        self._require_host_success(packed, "directory archive")
        try:
            archive = await asyncio.to_thread(self._active_sandbox().fs.download_file, archive_path)
            await asyncio.to_thread(extract_dir_from_bytes, archive, target_dir)
        finally:
            await self._host_exec(f"rm -f {shlex.quote(archive_path)}", user="root")

    def _active_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            raise RuntimeError("Local broker sandbox has not been started")
        return self._sandbox

    @staticmethod
    def _require_host_success(result: ExecResult, operation: str) -> None:
        if result.return_code != 0:
            raise RuntimeError(f"{operation} failed: {result.stderr or result.stdout}")
