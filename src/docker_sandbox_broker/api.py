"""FastAPI application exposing the versioned broker protocol."""

import secrets
from collections.abc import Callable

from fastapi import Body, Depends, FastAPI, Header, Query, Response
from fastapi.responses import JSONResponse

from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.errors import BrokerError
from docker_sandbox_broker.models import (
    BuildImageResponse,
    CreateSandboxRequest,
    ErrorResponse,
    ExecRequest,
    ExecResult,
    HealthResponse,
    SandboxView,
)
from docker_sandbox_broker.runtime import DockerRuntime, SandboxRuntime
from docker_sandbox_broker.service import BrokerService


def create_app(
    settings: BrokerSettings | None = None,
    runtime: SandboxRuntime | None = None,
) -> FastAPI:
    active_settings = settings or BrokerSettings.from_environment()
    active_runtime = runtime or DockerRuntime(
        active_settings.broker_id,
        state_path=active_settings.state_dir / active_settings.broker_id / "images.json",
        image_gc_min_free_bytes=active_settings.image_gc_min_free_mb * 1024 * 1024,
        image_gc_target_free_bytes=active_settings.image_gc_target_free_mb * 1024 * 1024,
        image_gc_min_age_seconds=active_settings.image_gc_min_age_seconds,
    )
    service = BrokerService(active_settings, active_runtime)
    authorize = _authorizer(active_settings.auth_token)

    app = FastAPI(title="Docker Sandbox Broker", version="0.1.0")
    app.state.service = service

    @app.exception_handler(BrokerError)
    async def broker_error_handler(_request, error: BrokerError):
        body = ErrorResponse(
            code=error.code,
            message=str(error),
            retryable=error.retryable,
        )
        return JSONResponse(status_code=error.status_code, content=body.model_dump())

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(broker_id=active_settings.broker_id)

    @app.post(
        "/v1/images/build",
        response_model=BuildImageResponse,
        status_code=201,
        dependencies=[Depends(authorize)],
    )
    def build_image(
        dockerfile: str = Query(default="Dockerfile"),
        context: bytes = Body(media_type="application/x-tar"),
    ) -> BuildImageResponse:
        return BuildImageResponse(image=service.build_image(dockerfile, context))

    @app.post(
        "/v1/sandboxes",
        response_model=SandboxView,
        status_code=201,
        dependencies=[Depends(authorize)],
    )
    def create_sandbox(request: CreateSandboxRequest) -> SandboxView:
        return service.create(request)

    @app.get(
        "/v1/sandboxes",
        response_model=list[SandboxView],
        dependencies=[Depends(authorize)],
    )
    def list_sandboxes() -> list[SandboxView]:
        return service.list()

    @app.get(
        "/v1/sandboxes/{sandbox_id}",
        response_model=SandboxView,
        dependencies=[Depends(authorize)],
    )
    def get_sandbox(sandbox_id: str) -> SandboxView:
        return service.get(sandbox_id)

    @app.post(
        "/v1/sandboxes/{sandbox_id}/exec",
        response_model=ExecResult,
        dependencies=[Depends(authorize)],
    )
    def exec_in_sandbox(sandbox_id: str, request: ExecRequest) -> ExecResult:
        return service.exec(sandbox_id, request)

    @app.put(
        "/v1/sandboxes/{sandbox_id}/files",
        status_code=204,
        dependencies=[Depends(authorize)],
    )
    def write_file(
        sandbox_id: str,
        path: str = Query(),
        content: bytes = Body(media_type="application/octet-stream"),
    ) -> Response:
        service.write_file(sandbox_id, path, content)
        return Response(status_code=204)

    @app.get(
        "/v1/sandboxes/{sandbox_id}/files",
        dependencies=[Depends(authorize)],
    )
    def read_file(sandbox_id: str, path: str = Query()) -> Response:
        content = service.read_file(sandbox_id, path)
        return Response(content=content, media_type="application/octet-stream")

    @app.put(
        "/v1/sandboxes/{sandbox_id}/archives",
        status_code=204,
        dependencies=[Depends(authorize)],
    )
    def put_archive(
        sandbox_id: str,
        root: str = Query(),
        content: bytes = Body(media_type="application/x-tar"),
    ) -> Response:
        service.put_archive(sandbox_id, root, content)
        return Response(status_code=204)

    @app.delete(
        "/v1/sandboxes/{sandbox_id}",
        status_code=204,
        dependencies=[Depends(authorize)],
    )
    def delete_sandbox(sandbox_id: str) -> Response:
        service.delete(sandbox_id)
        return Response(status_code=204)

    return app


def _authorizer(expected_token: str) -> Callable:
    def authorize(authorization: str | None = Header(default=None)) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        valid_scheme = scheme.lower() == "bearer"
        valid_token = bool(supplied) and secrets.compare_digest(supplied, expected_token)
        if not (valid_scheme and valid_token):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="invalid broker credentials")

    return authorize
