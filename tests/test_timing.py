import asyncio

import httpx
import pytest
from structlog.contextvars import merge_contextvars
from structlog.testing import capture_logs

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.errors import RuntimeOperationError
from docker_sandbox_broker.timing import timed_operation


def describe_operation_timing():
    def it_correlates_build_queue_worker_and_request_logs_without_payloads(settings, fake_runtime):
        """Queue, runtime and HTTP timings share a request ID without logging sensitive inputs."""
        fake_runtime.build_image = timed_operation("docker_build")(fake_runtime.build_image)

        async def exercise():
            with capture_logs(processors=[merge_contextvars]) as logs:
                app = create_app(settings, fake_runtime)
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url="http://test",
                        headers={"Authorization": f"Bearer {settings.auth_token}"},
                    ) as client,
                ):
                    response = await client.post(
                        "/v1/images/build?dockerfile=secret-selector", content=b"secret-context"
                    )
                    assert response.status_code == 201
                    response = await client.get("/unknown-secret-path?token=secret-query")
                    assert response.status_code == 404
                return logs

        logs = asyncio.run(exercise())
        events = {item["event"]: item for item in logs if item.get("route") != "unmatched"}
        measured = [
            events[name]
            for name in (
                "build_admitted",
                "build_started",
                "runtime_operation",
                "build_finished",
                "request_finished",
            )
        ]
        assert len({item["request_id"] for item in measured}) == 1
        assert events["build_started"]["queue_seconds"] >= 0
        assert events["build_finished"]["execution_seconds"] >= 0
        assert events["runtime_operation"]["duration_seconds"] >= 0
        assert events["request_finished"]["route"] == "/v1/images/build"
        assert events["request_finished"]["status"] == 201
        assert "secret-" not in str(logs)
        assert settings.auth_token not in str(logs)

    def it_records_failed_operation_duration_without_exception_contents():
        """Failed operations retain their error outcome without logging the exception payload."""

        @timed_operation("docker_build")
        def fail():
            raise RuntimeOperationError("secret exception contents")

        with capture_logs() as logs, pytest.raises(RuntimeOperationError):
            fail()
        assert len(logs) == 1
        assert logs[0]["outcome"] == "error"
        assert logs[0]["duration_seconds"] >= 0
        assert "secret" not in str(logs)
