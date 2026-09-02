from types import SimpleNamespace

import pytest

pytest.importorskip("harbor.environments", reason="Harbor consumer environment is optional")

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

import docker_sandbox_broker.harbor as harbor_adapter


class FakeFilesystem:
    def __init__(self):
        self.files = {}
        self.archives = []

    def upload_file(self, content, path):
        self.files[path] = content

    def download_file(self, path, *, binary=True):
        content = self.files[path]
        return content if binary else content.decode()

    def upload_archive(self, content, root):
        self.archives.append((root, content))


class FakeProcess:
    def __init__(self):
        self.calls = []

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(stdout=command, stderr="", exit_code=0)


class FakeSandbox:
    def __init__(self):
        self.process = FakeProcess()
        self.fs = FakeFilesystem()
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeClient:
    latest = None

    def __init__(self, *_args, **_kwargs):
        self.sandbox = FakeSandbox()
        self.request = None
        self.context = None
        self.closed = False
        FakeClient.latest = self

    def build_image(self, context):
        self.context = context
        return "broker-built:test"

    def create(self, request):
        self.request = request
        return self.sandbox

    def close(self):
        self.closed = True


def describe_harbor_local_broker_environment():
    @pytest.fixture
    def anyio_backend():
        return "asyncio"

    @pytest.fixture
    def environment(tmp_path, monkeypatch):
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir()
        (environment_dir / "Dockerfile").write_text("FROM alpine:3.20\nWORKDIR /workspace\n")
        monkeypatch.setenv("DSB_AUTH_TOKEN", "test-token-long-enough")
        monkeypatch.setenv("DSB_SOCKET", "/tmp/test-broker.sock")
        monkeypatch.setattr(harbor_adapter, "BrokerClient", FakeClient)
        return harbor_adapter.LocalBrokerEnvironment(
            environment_dir=environment_dir,
            environment_name="tiny-task",
            session_id="tiny-task__test__env",
            trial_paths=TrialPaths(tmp_path / "trial"),
            task_env_config=EnvironmentConfig(cpus=1, memory_mb=128),
        )

    @pytest.mark.anyio
    async def it_maps_harbor_build_and_lifecycle_to_provider_operations(environment):
        """Maps Harbor image build and lifecycle calls to broker operations."""
        await environment.start(force_build=False)
        sandbox = FakeClient.latest.sandbox

        await environment.stop(delete=True)

        assert FakeClient.latest.context
        assert FakeClient.latest.request.image == "broker-built:test"
        assert FakeClient.latest.request.resources.cpus == 1
        assert FakeClient.latest.request.resources.memory_mb == 128
        assert sandbox.deleted
        assert FakeClient.latest.closed

    @pytest.mark.anyio
    async def it_maps_harbor_process_and_filesystem_operations(environment, tmp_path):
        """Maps Harbor process and filesystem calls without task orchestration."""
        await environment.start(force_build=False)
        source = tmp_path / "message.txt"
        source.write_bytes(b"hello")

        result = await environment.exec("printf hello", timeout_sec=17)
        await environment.upload_file(source, "/workspace/message.txt")
        FakeClient.latest.sandbox.fs.files["/workspace/result.txt"] = b"result"
        target = tmp_path / "result.txt"
        await environment.download_file("/workspace/result.txt", target)

        assert result.return_code == 0
        assert target.read_bytes() == b"result"
        command, kwargs = next(
            call for call in FakeClient.latest.sandbox.process.calls if call[0] == "printf hello"
        )
        assert command == "printf hello"
        assert kwargs["cwd"] == "/workspace"
        assert kwargs["timeout"] == 17
