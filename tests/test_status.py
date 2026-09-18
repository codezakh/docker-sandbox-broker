import asyncio
from unittest.mock import Mock

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from test_build_scheduling import BlockedBuild, client_for, until

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.build_executor import BuildExecutor
from docker_sandbox_broker.client import BrokerClient
from docker_sandbox_broker.errors import RuntimeOperationError


def describe_broker_status():
    @pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
    def it_requires_the_existing_broker_token(api_client, authorization):
        """Status rejects missing or incorrect credentials."""
        response = api_client.get("/v1/status", headers={"Authorization": authorization})
        assert response.status_code == 401

    def it_reads_only_memory_and_exposes_no_credentials(settings):
        """Idle status works with a runtime that implements no Docker operations."""
        with TestClient(
            create_app(settings, object()),
            headers={"Authorization": f"Bearer {settings.auth_token}"},
        ) as client:
            response = client.get("/v1/status")
        assert response.status_code == 200
        data = response.json()
        assert data["broker_id"] == settings.broker_id
        assert data["uptime_seconds"] >= 0
        assert data["builds"] == {
            "accepting": True,
            "workers": 4,
            "queue_limit": 64,
            "max_context_bytes": 256 * 1024**2,
            "active": 0,
            "queued": 0,
            "context_bytes": 0,
            "oldest_running_seconds": None,
            "oldest_queued_seconds": None,
            "last_finished_seconds_ago": None,
            "succeeded_total": 0,
            "failed_total": 0,
            "rejected_total": 0,
            "cancelled_queued_total": 0,
        }
        assert settings.auth_token not in response.text

    def it_stays_responsive_under_saturation_and_reports_outcomes(settings, fake_runtime):
        """Reports build activity and outcomes without waiting for workers."""

        async def exercise():
            settings.build_workers = settings.build_queue_size = 1
            blocked = BlockedBuild()
            fake_runtime.build_image = blocked
            async with client_for(settings, fake_runtime) as client:

                async def status():
                    response = await asyncio.wait_for(client.get("/v1/status"), 1)
                    assert response.status_code == 200
                    return response.json()["builds"]

                first = asyncio.create_task(client.post("/v1/images/build", content=b"secret-one"))
                await until(lambda: blocked.started == 1)
                second = asyncio.create_task(client.post("/v1/images/build", content=b"secret-two"))
                try:
                    for _ in range(100):
                        snapshot = await status()
                        if snapshot["queued"] == 1:
                            break
                        await asyncio.sleep(0.001)
                    assert snapshot["active"] == snapshot["queued"] == 1
                    assert snapshot["context_bytes"] == 20
                    assert (
                        snapshot["oldest_running_seconds"] >= snapshot["oldest_queued_seconds"] >= 0
                    )
                    assert "secret" not in str(snapshot)
                    limiter = anyio.to_thread.current_default_thread_limiter()
                    original = limiter.total_tokens
                    limiter.total_tokens = 1
                    try:
                        async with limiter:
                            assert (await status())["active"] == 1
                    finally:
                        limiter.total_tokens = original
                    assert (
                        await client.post("/v1/images/build", content=b"third")
                    ).status_code == 503
                    assert (await status())["rejected_total"] == 1
                finally:
                    blocked.release.set()
                    await asyncio.gather(first, second)
                fake_runtime.build_image = Mock(side_effect=RuntimeOperationError("failure"))
                assert (await client.post("/v1/images/build", content=b"bad")).status_code == 502
                done = await status()
                assert done["active"] == done["queued"] == done["context_bytes"] == 0
                assert done["oldest_running_seconds"] is done["oldest_queued_seconds"] is None
                assert done["succeeded_total"] == 2
                assert done["failed_total"] == 1
                assert done["last_finished_seconds_ago"] >= 0

        asyncio.run(exercise())

    def it_reports_exact_ages_and_cancellation_without_losing_running_work(monkeypatch):
        """Monotonic ages and counters survive queued and running waiter cancellation."""

        async def exercise():
            clock = [100.0]
            monkeypatch.setattr("docker_sandbox_broker.build_executor.monotonic", lambda: clock[0])
            executor = BuildExecutor(1, 1, 100)
            blocked = BlockedBuild()
            first = asyncio.create_task(executor.run(blocked, "Dockerfile", b"one"))
            await until(lambda: blocked.started == 1)
            clock[0] = 200.0
            second = asyncio.create_task(executor.run(blocked, "Dockerfile", b"two"))
            try:
                await until(lambda: executor.snapshot().queued == 1)
                clock[0] = 300.0
                snapshot = executor.snapshot()
                assert snapshot.oldest_running_seconds == 200
                assert snapshot.oldest_queued_seconds == 100
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                snapshot = executor.snapshot()
                assert snapshot.active == 1
                assert snapshot.context_bytes == 3
                assert snapshot.queued == 0
                assert snapshot.cancelled_queued_total == 1
                assert snapshot.succeeded_total == snapshot.failed_total == 0
            finally:
                blocked.release.set()
                await executor.close()
            snapshot = executor.snapshot()
            assert snapshot.active == snapshot.queued == snapshot.context_bytes == 0
            assert snapshot.succeeded_total == 1
            assert snapshot.accepting is False
            assert snapshot.last_finished_seconds_ago == 0

        asyncio.run(exercise())

    def it_offers_a_typed_client_method_with_a_short_timeout(api_client):
        """The Python client sends existing credentials and validates the status response."""

        def respond(request):
            assert request.url.path == "/v1/status"
            assert request.headers["Authorization"] == "Bearer client-token"
            assert request.extensions["timeout"]["read"] == 2.0
            return httpx.Response(200, json=api_client.get("/v1/status").json())

        with BrokerClient("client-token", transport=httpx.MockTransport(respond)) as client:
            snapshot = client.status(timeout=2)
        assert snapshot.builds.workers == 4
