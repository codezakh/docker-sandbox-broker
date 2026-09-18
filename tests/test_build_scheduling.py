import asyncio
from contextlib import asynccontextmanager
from threading import Event, Lock

import anyio
import httpx
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.build_executor import BuildExecutor
from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.errors import BuildCapacityError


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 3)


class BlockedBuild:
    def __init__(self):
        self.release = Event()
        self.lock = Lock()
        self.started = 0

    def __call__(self, *args):
        with self.lock:
            self.started += 1
        if not self.release.wait(10):
            raise TimeoutError("test did not release build")
        return "test:built"


@asynccontextmanager
async def client_for(settings, runtime):
    app = create_app(settings, runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.auth_token}"},
        ) as client,
    ):
        yield client


def describe_build_isolation():
    @pytest.mark.anyio
    async def it_keeps_lifecycle_requests_responsive_with_forty_build_waiters(
        settings, fake_runtime
    ):
        """Four blocked builds and forty queued builds do not consume request-worker capacity."""
        settings.build_workers = 4
        settings.build_queue_size = 40
        blocked = BlockedBuild()
        fake_runtime.build_image = blocked
        with capture_logs() as logs:
            async with client_for(settings, fake_runtime) as client:
                requests = [
                    asyncio.create_task(client.post("/v1/images/build", content=b"context"))
                    for _ in range(44)
                ]
                try:
                    await until(lambda: sum(x["event"] == "build_admitted" for x in logs) == 44)
                    await until(lambda: blocked.started == 4)
                    assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
                    assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
                    created = await asyncio.wait_for(
                        client.post("/v1/sandboxes", json={"image": "test:image"}), 1
                    )
                    assert created.status_code == 201
                    sandbox_id = created.json()["id"]
                    assert (
                        await asyncio.wait_for(client.get("/v1/sandboxes"), 1)
                    ).status_code == 200
                    executed = await asyncio.wait_for(
                        client.post(f"/v1/sandboxes/{sandbox_id}/exec", json={"command": "hello"}),
                        1,
                    )
                    assert executed.status_code == 200
                    assert (
                        await asyncio.wait_for(client.delete(f"/v1/sandboxes/{sandbox_id}"), 1)
                    ).status_code == 204
                    rejected = await client.post("/v1/images/build", content=b"context")
                    assert rejected.status_code == 503
                    assert rejected.json()["retryable"] is True
                    assert rejected.headers["Retry-After"] == "1"
                finally:
                    blocked.release.set()
                    results = await asyncio.gather(*requests)
                assert all(result.status_code == 201 for result in results)

    @pytest.mark.anyio
    async def it_serves_health_and_auth_when_the_default_pool_is_exhausted(settings, fake_runtime):
        """Health and invalid credentials require no synchronous worker thread."""
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        try:
            async with client_for(settings, fake_runtime) as client, limiter:
                assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
                response = await asyncio.wait_for(
                    client.get("/v1/sandboxes", headers={"Authorization": "Bearer wrong"}), 1
                )
                assert response.status_code == 401
        finally:
            limiter.total_tokens = original

    @pytest.mark.anyio
    async def it_limits_admitted_context_bytes(settings, fake_runtime):
        """The memory budget rejects additional bytes and oversized single contexts."""
        settings.build_workers = 1
        settings.build_max_inflight_mb = 1
        blocked = BlockedBuild()
        fake_runtime.build_image = blocked
        async with client_for(settings, fake_runtime) as client:
            request = asyncio.create_task(client.post("/v1/images/build", content=b"x" * 700_000))
            try:
                await until(lambda: blocked.started == 1)
                assert (
                    await client.post("/v1/images/build", content=b"x" * 700_000)
                ).status_code == 503
                assert (
                    await client.post("/v1/images/build", content=b"x" * 1_100_000)
                ).status_code == 413
            finally:
                blocked.release.set()
                assert (await request).status_code == 201
            assert (await client.post("/v1/images/build", content=b"ok")).status_code == 201


def describe_build_cancellation():
    @pytest.mark.anyio
    async def it_retains_running_capacity_after_the_waiter_is_cancelled():
        """A cancelled HTTP waiter cannot let another build exceed the worker budget."""
        executor = BuildExecutor(1, 0, 100)
        blocked = BlockedBuild()
        request = asyncio.create_task(executor.run(blocked, "Dockerfile", b"context"))
        try:
            await until(lambda: blocked.started == 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(BuildCapacityError):
                await executor.run(blocked, "Dockerfile", b"context")
            assert blocked.started == 1
        finally:
            blocked.release.set()
            await executor.close()

    @pytest.mark.anyio
    async def it_removes_cancelled_waiters_and_runs_their_replacements():
        """Cancelling a queued request returns its capacity without submitting its build."""
        executor = BuildExecutor(1, 1, 100)
        blocked = BlockedBuild()
        with capture_logs() as logs:
            first = asyncio.create_task(executor.run(blocked, "Dockerfile", b"one"))
            await until(lambda: blocked.started == 1)
            queued = asyncio.create_task(executor.run(blocked, "Dockerfile", b"two"))
            try:
                await until(lambda: sum(x["event"] == "build_admitted" for x in logs) == 2)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
                replacement = asyncio.create_task(executor.run(blocked, "Dockerfile", b"three"))
                await until(lambda: sum(x["event"] == "build_admitted" for x in logs) == 3)
                assert blocked.started == 1
                blocked.release.set()
                assert await replacement == "test:built"
                assert await first == "test:built"
                assert blocked.started == 2
            finally:
                blocked.release.set()
                await executor.close()

    @pytest.mark.anyio
    async def it_releases_failed_builds_and_drains_on_shutdown():
        """Errors release capacity and shutdown waits for admitted work to finish."""
        executor = BuildExecutor(1, 1, 100)

        def fail(*args):
            raise ValueError("test failure")

        with pytest.raises(ValueError):
            await executor.run(fail, "Dockerfile", b"x")
        blocked = BlockedBuild()
        request = asyncio.create_task(executor.run(blocked, "Dockerfile", b"x"))
        try:
            await until(lambda: blocked.started == 1)
            queued = asyncio.create_task(executor.run(blocked, "Dockerfile", b"queued"))
            await asyncio.sleep(0)
            closing = asyncio.create_task(executor.close())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(BuildCapacityError):
                await executor.run(blocked, "Dockerfile", b"x")
        finally:
            blocked.release.set()
            await request
            await queued
            await closing
        assert blocked.started == 2


def describe_build_configuration():
    @pytest.mark.parametrize(
        "field,value",
        [("build_workers", 0), ("build_queue_size", -1), ("build_max_inflight_mb", 0)],
    )
    def it_rejects_invalid_limits(field, value):
        """Rejects limits that would disable progress or make admission unbounded."""
        with pytest.raises(ValidationError):
            BrokerSettings(auth_token="test-token-long-enough", **{field: value})

    def it_reads_build_limits_from_environment(monkeypatch):
        """Loads independent worker, queue and byte limits from host configuration."""
        monkeypatch.setenv("DSB_AUTH_TOKEN", "test-token-long-enough")
        monkeypatch.setenv("DSB_BUILD_WORKERS", "2")
        monkeypatch.setenv("DSB_BUILD_QUEUE_SIZE", "0")
        monkeypatch.setenv("DSB_BUILD_MAX_INFLIGHT_MB", "64")
        settings = BrokerSettings.from_environment()
        assert (
            settings.build_workers,
            settings.build_queue_size,
            settings.build_max_inflight_mb,
        ) == (2, 0, 64)
