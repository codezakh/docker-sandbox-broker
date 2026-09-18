import contextlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import docker
import pytest
from test_build_cache import context
from ulid import ULID

from docker_sandbox_broker.models import CreateSandboxRequest, ExecRequest
from docker_sandbox_broker.runtime import DockerRuntime

pytestmark = pytest.mark.docker


def describe_real_build_cache():
    def it_reuses_restarts_invalidates_and_rebuilds_real_images(tmp_path):
        """Verifies build counts and post-install output using actual Docker images."""
        client = docker.from_env(timeout=300)
        broker_id = f"pytest-build-{str(ULID()).lower()}"
        kwargs = dict(
            client=client,
            state_path=tmp_path / "images.json",
            image_gc_min_free_bytes=0,
            image_gc_target_free_bytes=0,
            image_gc_min_age_seconds=0,
        )
        runtime = DockerRuntime(broker_id, **kwargs)
        tags = set()
        try:
            with patch.object(client.api, "build", wraps=client.api.build) as builds:
                with ThreadPoolExecutor(8) as pool:
                    results = list(
                        pool.map(
                            lambda i: runtime.build_image(str(i), "Dockerfile", context()), range(8)
                        )
                    )
                tags.update(results)
                assert len(tags) == 1
                assert builds.call_count == 1
                tag = results[0]
                restarted = DockerRuntime(broker_id, **kwargs)
                assert restarted.build_image("restart", "Dockerfile", context()) == tag
                assert builds.call_count == 1
                _assert_marker(restarted, broker_id, tag, "first")
                changed = restarted.build_image(
                    "changed", "Dockerfile", context(b"echo changed > /marker")
                )
                tags.add(changed)
                assert changed != tag
                assert builds.call_count == 2
                _assert_marker(restarted, broker_id, changed, "changed")
                assert tag in restarted._image_cache.collect_if_needed(emergency=True)
                assert restarted.build_image("rebuild", "Dockerfile", context()) == tag
                assert builds.call_count == 3
                _assert_marker(restarted, broker_id, tag, "first")
        finally:
            for tag in tags:
                with contextlib.suppress(docker.errors.ImageNotFound):
                    client.images.remove(tag)
            client.close()


def _assert_marker(runtime, broker_id, tag, expected):
    sandbox_id = str(ULID())
    sandbox = runtime.create(sandbox_id, CreateSandboxRequest(image=tag))
    try:
        result = runtime.exec(sandbox.id, ExecRequest(command="cat /marker"), 1000)
        assert result.exit_code == 0
        assert result.stdout.strip() == expected
    finally:
        runtime.delete(sandbox.id, sandbox_id, broker_id)
