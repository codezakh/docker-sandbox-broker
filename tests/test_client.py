import httpx

from docker_sandbox_broker.client import BrokerClient, BrokerClientError
from docker_sandbox_broker.models import CreateSandboxRequest


class ProviderTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self._response_for(request)
        if response is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return response

    @staticmethod
    def _response_for(request):
        if request.url.path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "01JTEST0000000000000000000",
                    "image": "alpine:3.20",
                    "state": "running",
                    "run_id": None,
                    "docker_enabled": False,
                    "created_at": "2026-09-02T00:00:00Z",
                },
            )
        if request.url.path.endswith("/exec"):
            return httpx.Response(
                200,
                json={
                    "stdout": "hello",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "oom_killed": False,
                },
            )
        if request.method == "PUT":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith("/files"):
            return httpx.Response(200, content=b"hello")
        if request.method == "DELETE":
            return httpx.Response(204)
        return None


def policy_error_response(_request):
    return httpx.Response(
        422,
        json={"code": "policy_violation", "message": "denied", "retryable": False},
    )


def describe_provider_shaped_client():
    def it_exposes_process_and_filesystem_operations():
        """Exposes provider-shaped process, filesystem, and lifecycle operations."""
        provider = ProviderTransport()

        with BrokerClient(
            "test-token",
            transport=httpx.MockTransport(provider),
        ) as client:
            sandbox = client.create(CreateSandboxRequest(image="alpine:3.20"))
            result = sandbox.process.exec("echo hello")
            sandbox.fs.upload_file("hello", "/workspace/hello.txt")
            content = sandbox.fs.download_file("/workspace/hello.txt")
            sandbox.delete()

        assert result.stdout == "hello"
        assert content == b"hello"
        assert [request.method for request in provider.requests] == [
            "POST",
            "POST",
            "PUT",
            "GET",
            "DELETE",
        ]

    def it_waits_for_an_exec_as_long_as_its_command_timeout():
        """Waits past a timed exec's kill deadline and keeps the default for untimed execs."""
        provider = ProviderTransport()

        with BrokerClient("test-token", transport=httpx.MockTransport(provider)) as client:
            sandbox = client.create(CreateSandboxRequest(image="alpine:3.20"))
            sandbox.process.exec("make test", timeout=600)
            sandbox.process.exec("echo hello")

        timed, untimed = (
            request.extensions["timeout"]["read"] for request in provider.requests[1:]
        )
        assert timed > 600
        assert untimed == 300

    def it_applies_a_per_call_timeout_to_file_transfers():
        """Uses a caller's timeout for uploads and downloads, and the default otherwise."""
        provider = ProviderTransport()

        with BrokerClient("test-token", transport=httpx.MockTransport(provider)) as client:
            sandbox = client.create(CreateSandboxRequest(image="alpine:3.20"))
            sandbox.fs.upload_file("hello", "/workspace/a.txt", timeout=900)
            sandbox.fs.download_file("/workspace/a.txt", timeout=901)
            sandbox.fs.upload_archive(b"", timeout=902)
            sandbox.fs.upload_file("hello", "/workspace/b.txt")

        reads = [request.extensions["timeout"]["read"] for request in provider.requests[1:]]
        assert reads == [900, 901, 902, 300]

    def it_exposes_typed_provider_errors():
        """Exposes stable provider error codes to consumer adapters."""

        with BrokerClient(
            "test-token", transport=httpx.MockTransport(policy_error_response)
        ) as client:
            try:
                client.create(CreateSandboxRequest(image="alpine:3.20"))
            except BrokerClientError as error:
                assert error.code == "policy_violation"
                assert not error.retryable
            else:
                raise AssertionError("expected BrokerClientError")
