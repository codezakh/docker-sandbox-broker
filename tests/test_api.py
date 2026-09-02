from ulid import ULID

AUTH_TOKEN = "test-token-that-is-long-enough"


def describe_health():
    def it_is_available_without_credentials(api_client):
        """Reports protocol health without requiring credentials."""
        response = api_client.get("/health")

        assert response.status_code == 200
        assert response.json()["protocol_version"] == "v1"


def describe_authentication():
    def it_rejects_a_missing_token(api_client):
        """Rejects a sandbox request when its bearer token is missing."""
        response = api_client.get("/v1/sandboxes", headers={"Authorization": ""})

        assert response.status_code == 401

    def it_rejects_an_incorrect_token(api_client):
        """Rejects a sandbox request when its bearer token is incorrect."""
        response = api_client.get(
            "/v1/sandboxes",
            headers={"Authorization": "Bearer definitely-wrong"},
        )

        assert response.status_code == 401


def describe_sandbox_lifecycle():
    def _create(api_client, **overrides):
        request = {"image": "alpine:3.20", **overrides}
        return api_client.post("/v1/sandboxes", json=request)

    def it_assigns_a_ulid_and_lists_the_sandbox(api_client):
        """Assigns a ULID and exposes the new sandbox in the owned listing."""
        created = _create(api_client)

        assert created.status_code == 201
        sandbox_id = created.json()["id"]
        assert str(ULID.from_str(sandbox_id)) == sandbox_id
        listed = api_client.get("/v1/sandboxes").json()
        assert [item["id"] for item in listed] == [sandbox_id]

    def it_executes_a_command(api_client):
        """Executes a command through the selected sandbox."""
        sandbox_id = _create(api_client).json()["id"]

        response = api_client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": "printf hello"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "stdout": "printf hello",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "oom_killed": False,
        }

    def it_round_trips_a_file(api_client):
        """Preserves file bytes across upload and download."""
        sandbox_id = _create(api_client).json()["id"]

        uploaded = api_client.put(
            f"/v1/sandboxes/{sandbox_id}/files",
            params={"path": "/workspace/hello.txt"},
            content=b"hello",
            headers={
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
        downloaded = api_client.get(
            f"/v1/sandboxes/{sandbox_id}/files",
            params={"path": "/workspace/hello.txt"},
        )

        assert uploaded.status_code == 204
        assert downloaded.content == b"hello"

    def it_deletes_only_the_selected_sandbox(api_client):
        """Deletes the selected sandbox while preserving its sibling."""
        first = _create(api_client).json()["id"]
        second = _create(api_client).json()["id"]

        response = api_client.delete(f"/v1/sandboxes/{first}")

        assert response.status_code == 204
        assert [item["id"] for item in api_client.get("/v1/sandboxes").json()] == [second]

    def it_treats_repeated_deletion_as_success(api_client):
        """Treats repeated deletion as an idempotent successful operation."""
        sandbox_id = _create(api_client).json()["id"]

        first = api_client.delete(f"/v1/sandboxes/{sandbox_id}")
        repeated = api_client.delete(f"/v1/sandboxes/{sandbox_id}")

        assert first.status_code == 204
        assert repeated.status_code == 204

    def it_returns_a_typed_error_for_unknown_ids(api_client):
        """Returns a stable typed error for an unknown sandbox identity."""
        response = api_client.get("/v1/sandboxes/01AAAAAAAAAAAAAAAAAAAAAAAA")

        assert response.status_code == 404
        assert response.json()["code"] == "sandbox_not_found"


def describe_image_building():
    def it_delegates_a_safe_context_to_the_provider_runtime(api_client):
        """Builds an image from a provider-style Docker context archive."""
        response = api_client.post(
            "/v1/images/build",
            params={"dockerfile": "Dockerfile"},
            content=b"fake context accepted by the fake runtime",
            headers={
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "Content-Type": "application/x-tar",
            },
        )

        assert response.status_code == 201
        assert response.json()["image"].startswith("fake-build:")


def describe_resource_policy():
    def it_rejects_excess_memory(api_client):
        """Rejects memory requests above the configured broker ceiling."""
        response = api_client.post(
            "/v1/sandboxes",
            json={"image": "alpine:3.20", "resources": {"memory_mb": 99_999}},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "policy_violation"

    def it_rejects_docker_enabled_mode_by_default(api_client):
        """Rejects privileged Docker-enabled mode unless explicitly allowed."""
        response = api_client.post(
            "/v1/sandboxes",
            json={"image": "docker:24-dind", "features": {"docker": True}},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "policy_violation"
