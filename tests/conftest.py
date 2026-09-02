from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.errors import RuntimeOperationError
from docker_sandbox_broker.models import CreateSandboxRequest, ExecRequest, ExecResult
from docker_sandbox_broker.runtime import RuntimeSandbox

AUTH_TOKEN = "test-token-that-is-long-enough"


@dataclass
class FakeEntry:
    request: CreateSandboxRequest
    files: dict[str, bytes] = field(default_factory=dict)
    archives: list[tuple[str, bytes]] = field(default_factory=list)
    deleted: bool = False


class FakeRuntime:
    def __init__(self):
        self.entries: dict[str, FakeEntry] = {}

    def build_image(self, build_id, dockerfile, context):
        return f"fake-build:{build_id.lower()}"

    def create(self, sandbox_id, request):
        runtime_id = f"runtime-{sandbox_id}"
        self.entries[runtime_id] = FakeEntry(request=request)
        return RuntimeSandbox(id=runtime_id, state="running")

    def inspect(self, runtime_id):
        entry = self.entries[runtime_id]
        state = "exited" if entry.deleted else "running"
        return RuntimeSandbox(id=runtime_id, state=state)

    def exec(self, runtime_id, request: ExecRequest, output_limit):
        if runtime_id not in self.entries:
            raise RuntimeOperationError("missing runtime")
        return ExecResult(
            stdout=request.command[:output_limit],
            stderr="",
            exit_code=0,
        )

    def write_file(self, runtime_id, path, content):
        self.entries[runtime_id].files[path] = content

    def read_file(self, runtime_id, path, limit):
        return self.entries[runtime_id].files[path][:limit]

    def put_archive(self, runtime_id, root, content):
        self.entries[runtime_id].archives.append((root, content))

    def delete(self, runtime_id, sandbox_id, broker_id):
        self.entries[runtime_id].deleted = True


@pytest.fixture
def fake_runtime():
    return FakeRuntime()


@pytest.fixture
def settings():
    return BrokerSettings(auth_token=AUTH_TOKEN, broker_id="test-broker")


@pytest.fixture
def api_client(settings, fake_runtime):
    app = create_app(settings=settings, runtime=fake_runtime)
    with TestClient(app, headers={"Authorization": f"Bearer {AUTH_TOKEN}"}) as client:
        yield client
