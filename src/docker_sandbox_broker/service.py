"""Application service coordinating policy, identity, and runtime ownership."""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from ulid import ULID

from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.errors import (
    PayloadTooLargeError,
    PolicyViolationError,
    SandboxNotFoundError,
)
from docker_sandbox_broker.logging import get_logger
from docker_sandbox_broker.models import (
    CreateSandboxRequest,
    ExecRequest,
    ExecResult,
    SandboxState,
    SandboxView,
)
from docker_sandbox_broker.runtime import SandboxRuntime


@dataclass(frozen=True)
class SandboxRecord:
    id: str
    runtime_id: str
    request: CreateSandboxRequest
    created_at: datetime


class BrokerService:
    def __init__(self, settings: BrokerSettings, runtime: SandboxRuntime):
        self.settings = settings
        self._runtime = runtime
        self._records: dict[str, SandboxRecord] = {}
        self._lock = RLock()
        self._log = get_logger().bind(component="broker_service", broker_id=settings.broker_id)

    def build_image(self, dockerfile: str, context: bytes) -> str:
        self._validate_upload_size(context)
        build_id = str(ULID())
        image = self._runtime.build_image(build_id, dockerfile, context)
        self._log.info("image_built", build_id=build_id, image=image)
        return image

    def create(self, request: CreateSandboxRequest) -> SandboxView:
        self._validate_request(request)
        sandbox_id = str(ULID())
        created_at = datetime.now(UTC)
        runtime = self._runtime.create(sandbox_id, request)
        record = SandboxRecord(sandbox_id, runtime.id, request, created_at)
        with self._lock:
            self._records[sandbox_id] = record
        self._log.info("sandbox_created", sandbox_id=sandbox_id, image=request.image)
        return self._view(record, runtime.state)

    def get(self, sandbox_id: str) -> SandboxView:
        record = self._record(sandbox_id)
        runtime = self._runtime.inspect(record.runtime_id)
        return self._view(record, runtime.state)

    def list(self) -> list[SandboxView]:
        with self._lock:
            ids = list(self._records)
        return [self.get(sandbox_id) for sandbox_id in ids]

    def exec(self, sandbox_id: str, request: ExecRequest) -> ExecResult:
        record = self._record(sandbox_id)
        return self._runtime.exec(record.runtime_id, request, self.settings.max_output_bytes)

    def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        self._validate_upload_size(content)
        record = self._record(sandbox_id)
        self._runtime.write_file(record.runtime_id, path, content)

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        record = self._record(sandbox_id)
        return self._runtime.read_file(record.runtime_id, path, self.settings.max_download_bytes)

    def put_archive(self, sandbox_id: str, root: str, content: bytes) -> None:
        self._validate_upload_size(content)
        record = self._record(sandbox_id)
        self._runtime.put_archive(record.runtime_id, root, content)

    def delete(self, sandbox_id: str) -> None:
        with self._lock:
            record = self._records.get(sandbox_id)
        if record is None:
            return
        self._runtime.delete(record.runtime_id, sandbox_id, self.settings.broker_id)
        with self._lock:
            self._records.pop(sandbox_id, None)
        self._log.info("sandbox_deleted", sandbox_id=sandbox_id)

    def _record(self, sandbox_id: str) -> SandboxRecord:
        with self._lock:
            record = self._records.get(sandbox_id)
        if record is None:
            raise SandboxNotFoundError(f"unknown sandbox: {sandbox_id}")
        return record

    def _validate_request(self, request: CreateSandboxRequest) -> None:
        resources = request.resources
        if resources.memory_mb and resources.memory_mb > self.settings.max_memory_mb:
            raise PolicyViolationError(
                f"requested memory exceeds broker limit of {self.settings.max_memory_mb} MiB"
            )
        if resources.cpus and resources.cpus > self.settings.max_cpus:
            raise PolicyViolationError(
                f"requested CPUs exceed broker limit of {self.settings.max_cpus}"
            )
        if request.features.docker and not self.settings.allow_docker_enabled:
            raise PolicyViolationError("Docker-enabled sandboxes are disabled")
        if request.features.docker and not request.image.startswith("docker:"):
            raise PolicyViolationError(
                "Docker-enabled sandboxes must use an approved docker:* image"
            )

    def _validate_upload_size(self, content: bytes) -> None:
        if len(content) > self.settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"upload exceeds broker limit of {self.settings.max_upload_bytes} bytes"
            )

    @staticmethod
    def _view(record: SandboxRecord, runtime_state: str) -> SandboxView:
        state = SandboxState.RUNNING if runtime_state == "running" else SandboxState.STOPPED
        return SandboxView(
            id=record.id,
            image=record.request.image,
            state=state,
            run_id=record.request.run_id,
            docker_enabled=record.request.features.docker,
            created_at=record.created_at,
        )
