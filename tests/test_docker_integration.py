import io
import tarfile
import time

import pytest

from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.models import (
    CreateSandboxRequest,
    ExecRequest,
    ResourceLimits,
    SandboxFeatures,
)
from docker_sandbox_broker.runtime import DockerRuntime
from docker_sandbox_broker.service import BrokerService

pytestmark = pytest.mark.docker


@pytest.fixture
def direct_service():
    settings = BrokerSettings(auth_token="integration-token-long-enough", broker_id="integration")
    return BrokerService(settings, DockerRuntime(settings.broker_id))


@pytest.fixture
def dind_service():
    settings = BrokerSettings(
        auth_token="integration-token-long-enough",
        broker_id="integration",
        allow_docker_enabled=True,
    )
    return BrokerService(settings, DockerRuntime(settings.broker_id))


def describe_direct_sandbox_contract():
    def it_builds_a_tiny_harbor_style_docker_context(direct_service):
        """Builds and executes a tiny Harbor-style Dockerfile without leaking resources."""
        context = io.BytesIO()
        with tarfile.open(fileobj=context, mode="w") as archive:
            content = b"FROM alpine:3.20\nRUN printf built-ok > /marker.txt\n"
            member = tarfile.TarInfo("Dockerfile")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

        image = direct_service.build_image("Dockerfile", context.getvalue())
        sandbox = None
        try:
            sandbox = direct_service.create(CreateSandboxRequest(image=image))
            result = direct_service.exec(sandbox.id, ExecRequest(command="cat /marker.txt"))
        finally:
            if sandbox is not None:
                direct_service.delete(sandbox.id)
            direct_service._runtime._client.images.remove(image, force=True)

        assert result.stdout == "built-ok"

    def it_executes_and_transfers_files_without_leaking_a_container(direct_service):
        """Executes and transfers files, then leaves no direct task container."""
        sandbox = direct_service.create(CreateSandboxRequest(image="alpine:3.20"))
        try:
            result = direct_service.exec(sandbox.id, ExecRequest(command="printf direct-ok"))
            direct_service.write_file(sandbox.id, "/tmp/message.txt", b"file-ok")
            content = direct_service.read_file(sandbox.id, "/tmp/message.txt")
        finally:
            direct_service.delete(sandbox.id)

        assert result.stdout == "direct-ok"
        assert content == b"file-ok"


def describe_docker_enabled_contract():
    def it_runs_a_nested_container_with_registry_access(dind_service):
        """Runs a nested container with registry access, then removes its controller."""
        request = CreateSandboxRequest(
            image="docker:24-dind",
            features=SandboxFeatures(docker=True),
            resources=ResourceLimits(memory_mb=1024, cpus=1),
        )
        sandbox = dind_service.create(request)
        try:
            _wait_for_inner_docker(dind_service, sandbox.id)
            dind_service.write_file(sandbox.id, "/tmp/outer-file.txt", b"outer-file-ok")
            outer_file = dind_service.exec(
                sandbox.id,
                ExecRequest(command="cat /tmp/outer-file.txt", timeout_seconds=30),
            )
            result = dind_service.exec(
                sandbox.id,
                ExecRequest(
                    command="docker run --rm alpine:3.20 sh -c 'echo nested-ok'",
                    timeout_seconds=120,
                ),
            )
            compose = dind_service.exec(
                sandbox.id,
                ExecRequest(command="docker compose version", timeout_seconds=30),
            )
        finally:
            dind_service.delete(sandbox.id)

        assert result.stdout.strip() == "nested-ok"
        assert result.exit_code == 0
        assert (
            outer_file.stdout,
            outer_file.stderr,
            outer_file.exit_code,
        ) == ("outer-file-ok", "", 0)
        assert "Docker Compose version" in compose.stdout


def _wait_for_inner_docker(service, sandbox_id):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = service.exec(
            sandbox_id,
            ExecRequest(command="docker info >/dev/null 2>&1", timeout_seconds=5),
        )
        if result.exit_code == 0:
            return
        time.sleep(1)
    raise AssertionError(
        "expected the Docker-enabled sandbox to expose a ready inner daemon within 45 seconds"
    )
