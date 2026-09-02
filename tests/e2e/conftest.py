import contextlib
import os
import subprocess
import sys
import time

import docker
import httpx
import pytest
from docker.errors import ImageNotFound
from ulid import ULID

from docker_sandbox_broker import BrokerClient


@pytest.fixture
def live_broker(tmp_path):
    token = "integration-token-that-is-long-enough"
    broker_id = f"pytest-live-{str(ULID()).lower()}"
    image_repository = f"docker-sandbox-broker/{broker_id}"
    socket_path = tmp_path / "broker.sock"
    log_path = tmp_path / "broker.log"
    environment = {
        **os.environ,
        "DSB_AUTH_TOKEN": token,
        "DSB_BROKER_ID": broker_id,
        "DSB_ALLOW_DOCKER_ENABLED": "true",
    }
    with log_path.open("w+") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "docker_sandbox_broker.cli", "--uds", str(socket_path)],
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_until_ready(process, socket_path, log_file)
            with BrokerClient(token, uds=socket_path) as client:
                yield client, socket_path, token
                for sandbox in client.list():
                    client.delete(sandbox.id)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            _remove_test_images(image_repository)


def _wait_until_ready(process, socket_path, log_file):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_file.seek(0)
            raise RuntimeError(f"broker exited during startup:\n{log_file.read()}")
        if socket_path.exists():
            transport = httpx.HTTPTransport(uds=str(socket_path))
            try:
                with httpx.Client(transport=transport, base_url="http://localhost") as client:
                    if client.get("/health").status_code == 200:
                        return
            except httpx.HTTPError:
                pass
        time.sleep(0.05)
    log_file.seek(0)
    raise TimeoutError(f"broker did not become ready:\n{log_file.read()}")


def _remove_test_images(repository):
    docker_client = docker.from_env()
    try:
        tags = {
            tag
            for image in docker_client.images.list(name=repository)
            for tag in image.tags
            if tag.partition(":")[0] == repository
        }
        for tag in tags:
            with contextlib.suppress(ImageNotFound):
                docker_client.images.remove(tag, noprune=True)
    finally:
        docker_client.close()
