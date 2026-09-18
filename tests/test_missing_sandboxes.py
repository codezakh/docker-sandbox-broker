from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from docker.errors import DockerException, NotFound
from fastapi.testclient import TestClient

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.runtime import DockerRuntime, RuntimeSandbox


@pytest.fixture
def missing_client(settings, tmp_path):
    healthy = SimpleNamespace(
        id="healthy", status="running", reload=Mock(), attrs={"State": {"Status": "running"}}
    )
    containers = SimpleNamespace(get=Mock(return_value=healthy))
    runtime = DockerRuntime(
        "test-broker",
        client=SimpleNamespace(containers=containers),
        state_path=tmp_path / "images.json",
    )
    with patch.object(
        runtime,
        "create",
        side_effect=[RuntimeSandbox("healthy", "running"), RuntimeSandbox("missing", "running")],
    ):
        app = create_app(settings=settings, runtime=runtime)
        with TestClient(app, headers={"Authorization": f"Bearer {settings.auth_token}"}) as client:
            ids = [
                client.post("/v1/sandboxes", json={"image": "test:image"}).json()["id"]
                for _ in range(2)
            ]
            yield client, containers, healthy, ids


def describe_missing_sandboxes():
    @pytest.mark.parametrize("stage", ["get", "reload"])
    def it_lists_healthy_and_missing_containers(missing_client, stage):
        """A Docker 404 during either inspection step leaves the complete list readable."""
        client, containers, healthy, ids = missing_client
        missing = SimpleNamespace(reload=Mock(side_effect=NotFound("gone")))

        def get(runtime_id):
            if runtime_id == "healthy":
                return healthy
            if stage == "get":
                raise NotFound("gone")
            return missing

        containers.get.side_effect = get
        response = client.get("/v1/sandboxes")
        assert response.status_code == 200
        assert {item["id"]: item["state"] for item in response.json()} == {
            ids[0]: "running",
            ids[1]: "missing",
        }
        assert client.get(f"/v1/sandboxes/{ids[1]}").json()["state"] == "missing"

    def it_deletes_a_missing_container_idempotently(missing_client):
        """A missing Docker container can be deleted twice through the HTTP endpoint."""
        client, containers, _healthy, ids = missing_client
        containers.get.side_effect = NotFound("gone")
        assert client.delete(f"/v1/sandboxes/{ids[1]}").status_code == 204
        assert client.delete(f"/v1/sandboxes/{ids[1]}").status_code == 204
        containers.get.side_effect = None
        response = client.get("/v1/sandboxes")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [ids[0]]

    def it_survives_a_delete_during_listing(missing_client):
        """Deleting a snapshotted record during listing cannot fail the whole request."""
        client, containers, healthy, ids = missing_client

        def get(runtime_id):
            if runtime_id == "missing":
                raise NotFound("gone")
            assert client.delete(f"/v1/sandboxes/{ids[1]}").status_code == 204
            return healthy

        containers.get.side_effect = get
        response = client.get("/v1/sandboxes")
        assert response.status_code == 200
        assert {item["id"]: item["state"] for item in response.json()} == {
            ids[0]: "running",
            ids[1]: "missing",
        }

    @pytest.mark.parametrize("operation", ["list", "delete"])
    def it_preserves_real_docker_errors(missing_client, operation):
        """Daemon failures remain typed errors rather than successful missing results."""
        client, containers, _healthy, ids = missing_client
        containers.get.side_effect = DockerException("daemon unavailable")
        response = (
            client.get("/v1/sandboxes")
            if operation == "list"
            else client.delete(f"/v1/sandboxes/{ids[1]}")
        )
        assert response.status_code == 502
        assert response.json()["code"] == "runtime_operation_failed"
        containers.get.side_effect = None
        assert len(client.get("/v1/sandboxes").json()) == 2
