"""Timing logs containing operation names and durations, never request payloads."""

from functools import wraps
from time import monotonic

from structlog.contextvars import bound_contextvars
from ulid import ULID

from docker_sandbox_broker.logging import get_logger


def timed_operation(operation: str):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            started = monotonic()
            outcome = "error"
            try:
                result = function(*args, **kwargs)
                outcome = "ok"
                return result
            finally:
                get_logger().info(
                    "runtime_operation",
                    operation=operation,
                    duration_seconds=monotonic() - started,
                    outcome=outcome,
                )

        return measured

    return decorate


class RequestTiming:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        with bound_contextvars(request_id=str(ULID())):
            await self._http(scope, receive, send)

    async def _http(self, scope, receive, send):
        started = monotonic()
        status = 500

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            route = scope.get("route")
            get_logger().info(
                "request_finished",
                method=scope["method"],
                route=getattr(route, "path", "unmatched"),
                status=status,
                duration_seconds=monotonic() - started,
            )
