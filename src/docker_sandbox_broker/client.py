"""Small provider-shaped synchronous Python client."""

from pathlib import Path
from typing import Any

import httpx

from docker_sandbox_broker.models import (
    CreateSandboxRequest,
    ExecRequest,
    ExecResult,
    SandboxView,
)


class BrokerClientError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrokerClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "http://localhost",
        uds: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        active_transport = transport
        if active_transport is None and uds is not None:
            active_transport = httpx.HTTPTransport(uds=str(uds))
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=active_transport,
            timeout=300,
        )

    def create(self, request: CreateSandboxRequest) -> "Sandbox":
        response = self._request("POST", "/v1/sandboxes", json=request.model_dump())
        return Sandbox(self, SandboxView.model_validate(response.json()))

    def build_image(self, context: bytes, dockerfile: str = "Dockerfile") -> str:
        response = self._request(
            "POST",
            "/v1/images/build",
            params={"dockerfile": dockerfile},
            content=context,
            headers={"Content-Type": "application/x-tar"},
        )
        return str(response.json()["image"])

    def get(self, sandbox_id: str) -> SandboxView:
        response = self._request("GET", f"/v1/sandboxes/{sandbox_id}")
        return SandboxView.model_validate(response.json())

    def list(self) -> list[SandboxView]:
        response = self._request("GET", "/v1/sandboxes")
        return [SandboxView.model_validate(item) for item in response.json()]

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        if response.is_success:
            return response
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
        raise BrokerClientError(
            code=body.get("code", "http_error"),
            message=body.get("message", body.get("detail", response.text)),
            retryable=body.get("retryable", False),
        )

    def __enter__(self) -> "BrokerClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class Sandbox:
    def __init__(self, client: BrokerClient, view: SandboxView):
        self._client = client
        self.view = view
        self.process = SandboxProcess(client, view.id)
        self.fs = SandboxFilesystem(client, view.id)

    @property
    def id(self) -> str:
        return self.view.id

    def refresh(self) -> SandboxView:
        self.view = self._client.get(self.id)
        return self.view

    def delete(self) -> None:
        self._client._request("DELETE", f"/v1/sandboxes/{self.id}")


class SandboxProcess:
    def __init__(self, client: BrokerClient, sandbox_id: str):
        self._client = client
        self._sandbox_id = sandbox_id

    def exec(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
    ) -> ExecResult:
        request = ExecRequest(
            command=command,
            timeout_seconds=timeout,
            cwd=cwd,
            environment=env or {},
            user=user,
        )
        response = self._client._request(
            "POST",
            f"/v1/sandboxes/{self._sandbox_id}/exec",
            json=request.model_dump(),
        )
        return ExecResult.model_validate(response.json())


class SandboxFilesystem:
    def __init__(self, client: BrokerClient, sandbox_id: str):
        self._client = client
        self._sandbox_id = sandbox_id

    def upload_file(self, content: str | bytes, path: str) -> None:
        data = content.encode() if isinstance(content, str) else content
        self._client._request(
            "PUT",
            f"/v1/sandboxes/{self._sandbox_id}/files",
            params={"path": path},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    def download_file(self, path: str, *, binary: bool = True) -> str | bytes:
        response = self._client._request(
            "GET",
            f"/v1/sandboxes/{self._sandbox_id}/files",
            params={"path": path},
        )
        return response.content if binary else response.text

    def upload_archive(self, content: bytes, root: str = "/") -> None:
        self._client._request(
            "PUT",
            f"/v1/sandboxes/{self._sandbox_id}/archives",
            params={"root": root},
            content=content,
            headers={"Content-Type": "application/x-tar"},
        )
