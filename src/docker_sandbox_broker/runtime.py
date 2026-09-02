"""Docker runtime implementation hidden behind the broker API."""

import contextlib
import io
import os
import shlex
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from docker_sandbox_broker.errors import (
    PayloadTooLargeError,
    RuntimeOperationError,
    SandboxOwnershipError,
)
from docker_sandbox_broker.image_cache import ImageCache
from docker_sandbox_broker.models import CreateSandboxRequest, ExecRequest, ExecResult

MANAGED_BY_LABEL = "me.zaidkhan.docker-sandbox-broker.managed"
BROKER_ID_LABEL = "me.zaidkhan.docker-sandbox-broker.broker-id"
SANDBOX_ID_LABEL = "me.zaidkhan.docker-sandbox-broker.sandbox-id"
RUN_ID_LABEL = "me.zaidkhan.docker-sandbox-broker.run-id"
DOCKER_ENABLED_LABEL = "me.zaidkhan.docker-sandbox-broker.docker-enabled"
TRANSFER_DIR = "/broker-transfer"


@dataclass(frozen=True)
class RuntimeSandbox:
    id: str
    state: str


class SandboxRuntime(Protocol):
    def build_image(self, build_id: str, dockerfile: str, context: bytes) -> str: ...

    def create(self, sandbox_id: str, request: CreateSandboxRequest) -> RuntimeSandbox: ...

    def inspect(self, runtime_id: str) -> RuntimeSandbox: ...

    def exec(self, runtime_id: str, request: ExecRequest, output_limit: int) -> ExecResult: ...

    def write_file(self, runtime_id: str, path: str, content: bytes) -> None: ...

    def read_file(self, runtime_id: str, path: str, limit: int) -> bytes: ...

    def put_archive(self, runtime_id: str, root: str, content: bytes) -> None: ...

    def delete(self, runtime_id: str, sandbox_id: str, broker_id: str) -> None: ...


class DockerRuntime:
    """Own only containers carrying this broker's exact ownership labels."""

    def __init__(
        self,
        broker_id: str,
        client=None,
        *,
        state_path: Path | None = None,
        image_gc_min_free_bytes: int = 20 * 1024**3,
        image_gc_target_free_bytes: int = 40 * 1024**3,
        image_gc_min_age_seconds: int = 300,
        image_cache: ImageCache | None = None,
    ):
        self._broker_id = broker_id
        self._client = client or docker.from_env(timeout=300)
        active_state_path = state_path or (
            Path("/var/tmp") / f"docker-sandbox-broker-{os.getuid()}" / broker_id / "images.json"
        )
        self._image_cache = image_cache or ImageCache(
            self._client,
            active_state_path,
            image_gc_min_free_bytes,
            image_gc_target_free_bytes,
            image_gc_min_age_seconds,
        )

    def build_image(self, build_id: str, dockerfile: str, context: bytes) -> str:
        _validate_tar(context)
        tag = f"docker-sandbox-broker/{self._broker_id}:{build_id.lower()}"
        self._image_cache.collect_if_needed()
        try:
            image = self._build_image(tag, dockerfile, context)
        except DockerException as error:
            if not _out_of_space(error):
                raise RuntimeOperationError(f"could not build sandbox image: {error}") from error
            self._image_cache.collect_if_needed(emergency=True)
            try:
                image = self._build_image(tag, dockerfile, context)
            except DockerException as retry_error:
                raise RuntimeOperationError(
                    f"could not build sandbox image: {retry_error}"
                ) from retry_error
        self._image_cache.record(tag, image.id, "built")
        return tag

    def _build_image(self, tag: str, dockerfile: str, context: bytes):
        image, _logs = self._client.images.build(
            fileobj=io.BytesIO(context),
            custom_context=True,
            dockerfile=dockerfile,
            tag=tag,
            labels={
                MANAGED_BY_LABEL: "true",
                BROKER_ID_LABEL: self._broker_id,
            },
            rm=True,
            forcerm=True,
        )
        return image

    def create(self, sandbox_id: str, request: CreateSandboxRequest) -> RuntimeSandbox:
        image_id = self._ensure_image(request.image)
        labels = {
            MANAGED_BY_LABEL: "true",
            BROKER_ID_LABEL: self._broker_id,
            SANDBOX_ID_LABEL: sandbox_id,
        }
        if request.run_id:
            labels[RUN_ID_LABEL] = request.run_id
        if request.features.docker:
            labels[DOCKER_ENABLED_LABEL] = "true"
        kwargs = self._container_options(request, labels)
        container = None
        with self._image_cache.reserve(image_id):
            try:
                container = self._client.containers.create(request.image, **kwargs)
                container.start()
                container.reload()
            except DockerException as error:
                if container is not None:
                    container.remove(force=True)
                raise RuntimeOperationError(f"could not create sandbox: {error}") from error
        return RuntimeSandbox(id=container.id, state=_state(container))

    def inspect(self, runtime_id: str) -> RuntimeSandbox:
        container = self._container(runtime_id)
        container.reload()
        return RuntimeSandbox(id=container.id, state=_state(container))

    def exec(self, runtime_id: str, request: ExecRequest, output_limit: int) -> ExecResult:
        container = self._container(runtime_id)
        command = _wrapped_command(request.command, request.timeout_seconds)
        try:
            result = container.exec_run(
                ["sh", "-lc", command],
                demux=True,
                environment=request.environment or None,
                workdir=request.cwd,
                user=request.user,
            )
            container.reload()
        except DockerException as error:
            raise RuntimeOperationError(f"sandbox command failed: {error}") from error
        stdout_bytes, stderr_bytes = result.output or (b"", b"")
        stdout = _decode_limited(stdout_bytes, output_limit)
        stderr = _decode_limited(stderr_bytes, output_limit)
        timed_out = result.exit_code == 124
        if timed_out:
            stderr = f"Command timed out after {request.timeout_seconds}s.\n{stderr}"
        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=result.exit_code,
            timed_out=timed_out,
            oom_killed=bool(container.attrs.get("State", {}).get("OOMKilled")),
        )

    def write_file(self, runtime_id: str, path: str, content: bytes) -> None:
        target = _absolute_path(path)
        container = self._container(runtime_id)
        if _docker_enabled(container):
            staged = f"{TRANSFER_DIR}/{uuid4().hex}"
            _put_file(container, TRANSFER_DIR, PurePosixPath(staged).name, content)
            try:
                _exec_checked(
                    container,
                    f"mkdir -p {shlex.quote(str(target.parent))} && "
                    f"mv {shlex.quote(staged)} {shlex.quote(str(target))}",
                )
            finally:
                _exec_cleanup(container, staged)
            return
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(target.name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        self.put_archive(runtime_id, str(target.parent), archive.getvalue())

    def read_file(self, runtime_id: str, path: str, limit: int) -> bytes:
        target = _absolute_path(path)
        container = self._container(runtime_id)
        staged = None
        if _docker_enabled(container):
            staged = f"{TRANSFER_DIR}/{uuid4().hex}"
            _exec_checked(
                container,
                f"cp {shlex.quote(str(target))} {shlex.quote(staged)}",
            )
            target = PurePosixPath(staged)
        try:
            chunks, _ = container.get_archive(str(target))
            archive_bytes = _read_chunks(chunks, limit)
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
                members = [member for member in tar.getmembers() if member.isfile()]
                if len(members) != 1:
                    raise RuntimeOperationError(f"expected one regular file at {path}")
                extracted = tar.extractfile(members[0])
                if extracted is None:
                    raise RuntimeOperationError(f"could not read file at {path}")
                return _read_limited(extracted, limit)
        except NotFound as error:
            raise RuntimeOperationError(f"file does not exist: {path}") from error
        except tarfile.TarError as error:
            raise RuntimeOperationError(f"invalid archive returned for {path}") from error
        finally:
            if staged is not None:
                _exec_cleanup(container, staged)

    def put_archive(self, runtime_id: str, root: str, content: bytes) -> None:
        target = _absolute_path(root)
        _validate_tar(content)
        container = self._container(runtime_id)
        if _docker_enabled(container):
            staged = f"{TRANSFER_DIR}/{uuid4().hex}.tar"
            _put_file(container, TRANSFER_DIR, PurePosixPath(staged).name, content)
            try:
                _exec_checked(
                    container,
                    f"mkdir -p {shlex.quote(str(target))} && "
                    f"tar -xf {shlex.quote(staged)} -C {shlex.quote(str(target))}",
                )
            finally:
                _exec_cleanup(container, staged)
            return
        try:
            accepted = container.put_archive(str(target), content)
        except DockerException as error:
            raise RuntimeOperationError(f"archive upload failed: {error}") from error
        if not accepted:
            raise RuntimeOperationError("Docker rejected archive upload")

    def delete(self, runtime_id: str, sandbox_id: str, broker_id: str) -> None:
        try:
            container = self._client.containers.get(runtime_id)
        except NotFound:
            return
        labels = container.labels or {}
        expected = {
            MANAGED_BY_LABEL: "true",
            BROKER_ID_LABEL: broker_id,
            SANDBOX_ID_LABEL: sandbox_id,
        }
        if any(labels.get(key) != value for key, value in expected.items()):
            raise SandboxOwnershipError(
                f"refusing to delete container {runtime_id}: ownership labels do not match"
            )
        try:
            container.remove(force=True, v=True)
        except NotFound:
            return
        except DockerException as error:
            raise RuntimeOperationError(f"could not delete sandbox: {error}") from error
        self._image_cache.collect_if_needed()

    def _ensure_image(self, image: str) -> str:
        try:
            existing = self._client.images.get(image)
        except ImageNotFound:
            self._image_cache.collect_if_needed()
            try:
                pulled = self._client.images.pull(image)
            except DockerException as error:
                if not _out_of_space(error):
                    raise RuntimeOperationError(f"could not pull image {image}: {error}") from error
                self._image_cache.collect_if_needed(emergency=True)
                try:
                    pulled = self._client.images.pull(image)
                except DockerException as retry_error:
                    raise RuntimeOperationError(
                        f"could not pull image {image}: {retry_error}"
                    ) from retry_error
            self._image_cache.record(image, pulled.id, "pulled")
            return pulled.id
        self._image_cache.touch(image, existing.id)
        return existing.id

    def _container(self, runtime_id: str):
        try:
            return self._client.containers.get(runtime_id)
        except NotFound as error:
            raise RuntimeOperationError(f"runtime container is missing: {runtime_id}") from error

    @staticmethod
    def _container_options(request: CreateSandboxRequest, labels: dict[str, str]) -> dict:
        command = request.command
        if command is None and not request.features.docker:
            command = ["sleep", "infinity"]
        options = {
            "command": command,
            "detach": True,
            "auto_remove": False,
            "labels": labels,
            "environment": request.environment or None,
            "privileged": request.features.docker,
        }
        if request.features.docker:
            options["network_mode"] = "host"
            options["volumes"] = [TRANSFER_DIR]
        if request.resources.memory_mb is not None:
            options["mem_limit"] = f"{request.resources.memory_mb}m"
            options["memswap_limit"] = f"{request.resources.memory_mb}m"
        if request.resources.cpus is not None:
            options["nano_cpus"] = int(request.resources.cpus * 1_000_000_000)
        return options


def _docker_enabled(container) -> bool:
    return (container.labels or {}).get(DOCKER_ENABLED_LABEL) == "true"


def _out_of_space(error: DockerException) -> bool:
    return "no space left on device" in str(error).lower()


def _put_file(container, root: str, name: str, content: bytes) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(content))
    try:
        accepted = container.put_archive(root, archive.getvalue())
    except DockerException as error:
        raise RuntimeOperationError(f"transfer staging failed: {error}") from error
    if not accepted:
        raise RuntimeOperationError("Docker rejected transfer staging")


def _exec_checked(container, command: str) -> None:
    try:
        result = container.exec_run(["sh", "-lc", command], demux=True, user="root")
    except DockerException as error:
        raise RuntimeOperationError(f"transfer command failed: {error}") from error
    if result.exit_code != 0:
        stdout, stderr = result.output or (b"", b"")
        detail = (stderr or stdout or b"unknown error").decode(errors="replace")
        raise RuntimeOperationError(f"transfer command failed: {detail}")


def _exec_cleanup(container, path: str) -> None:
    with contextlib.suppress(DockerException):
        container.exec_run(["sh", "-lc", f"rm -f {shlex.quote(path)}"], user="root")


def _state(container) -> str:
    return str(container.attrs.get("State", {}).get("Status", container.status))


def _wrapped_command(command: str, timeout_seconds: int | None) -> str:
    if timeout_seconds is None:
        return command
    timeout = shlex.quote(str(timeout_seconds))
    return f"timeout -s TERM -k 10 {timeout} sh -lc {shlex.quote(command)}"


def _decode_limited(content: bytes | None, limit: int) -> str:
    return (content or b"")[:limit].decode("utf-8", errors="replace")


def _absolute_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeOperationError(f"path must be absolute and cannot traverse parents: {raw}")
    return path


def _validate_tar(content: bytes) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as tar:
            for member in tar.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise RuntimeOperationError(f"unsafe archive path: {member.name}")
                if member.isdev() or member.isfifo():
                    raise RuntimeOperationError(f"unsupported archive entry: {member.name}")
    except tarfile.TarError as error:
        raise RuntimeOperationError("request body is not a valid tar archive") from error


def _read_chunks(chunks, limit: int) -> bytes:
    result = bytearray()
    for chunk in chunks:
        result.extend(chunk)
        if len(result) > limit:
            raise PayloadTooLargeError(f"download exceeds {limit} bytes")
    return bytes(result)


def _read_limited(stream, limit: int) -> bytes:
    result = stream.read(limit + 1)
    if len(result) > limit:
        raise PayloadTooLargeError(f"file exceeds {limit} bytes")
    return result
