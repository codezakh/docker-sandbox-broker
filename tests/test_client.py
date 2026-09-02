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
