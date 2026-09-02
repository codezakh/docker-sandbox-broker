import json
import os
import subprocess
from pathlib import Path

import docker
import pytest
from docker.errors import ImageNotFound

pytestmark = [pytest.mark.docker, pytest.mark.e2e, pytest.mark.devcontainer]


def describe_development_container_to_host_broker_boundary():
    def it_runs_a_sandbox_without_exposing_the_host_docker_socket(host_broker_runtime):
        """Creates, executes, and deletes through a read-only mounted broker socket."""
        runtime, client = host_broker_runtime
        image = os.environ.get("DSB_DEV_CONTAINER_IMAGE", "harbor-exploration-project")
        _require_image(image)
        repository = Path(__file__).parents[2]
        program = """
import os
from docker_sandbox_broker import BrokerClient
from docker_sandbox_broker.models import CreateSandboxRequest

with BrokerClient(os.environ["DSB_AUTH_TOKEN"], uds=os.environ["DSB_SOCKET"]) as client:
    sandbox = client.create(CreateSandboxRequest(image="bash:5.2"))
    try:
        result = sandbox.process.exec("printf dev-container-to-host-ok")
        print(result.stdout)
    finally:
        sandbox.delete()
"""

        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=host",
                "--env-file",
                str(runtime.container_env),
                "-v",
                f"{runtime.directory}:{runtime.directory}:ro",
                "-v",
                f"{repository}:{repository}:ro",
                "-e",
                f"PYTHONPATH={repository / 'src'}",
                "--entrypoint",
                "python",
                image,
                "-c",
                program,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stdout.strip() == "dev-container-to-host-ok"
        assert client.list() == []

    def it_runs_harbors_oracle_and_verifier_inside_the_development_container(
        host_broker_runtime, tmp_path
    ):
        """Runs Harbor's CLI in the dev container while Docker remains host-brokered."""
        runtime, client = host_broker_runtime
        image = os.environ.get("DSB_DEV_CONTAINER_IMAGE", "harbor-exploration-project")
        _require_image(image)
        repository = Path(__file__).parents[2]
        harbor_root = _harbor_checkout(repository)
        dataset = repository / "tests/fixtures/harbor-dataset"
        work_dir = tmp_path / "container-work"
        jobs_dir = work_dir / "jobs"
        work_dir.mkdir()
        config_path = work_dir / "harbor.json"
        config_path.write_text(
            json.dumps(
                {
                    "job_name": "broker-dev-container-harbor-e2e",
                    "jobs_dir": str(jobs_dir),
                    "n_attempts": 1,
                    "n_concurrent_trials": 1,
                    "environment": {
                        "import_path": "docker_sandbox_broker.harbor:LocalBrokerEnvironment"
                    },
                    "agents": [{"name": "oracle"}],
                    "datasets": [{"path": str(dataset), "task_names": ["tiny-oracle"]}],
                }
            )
        )
        python_path = f"{repository / 'src'}:{harbor_root / 'harbor/src'}"

        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=host",
                "--env-file",
                str(runtime.container_env),
                "-v",
                f"{runtime.directory}:{runtime.directory}:ro",
                "-v",
                f"{repository}:{repository}:ro",
                "-v",
                f"{harbor_root}:{harbor_root}:ro",
                "-v",
                f"{work_dir}:{work_dir}",
                "-e",
                f"PYTHONPATH={python_path}",
                "--entrypoint",
                "bash",
                image,
                "-c",
                (
                    'uv pip install --quiet --no-deps --python "$(command -v python)" '
                    '-e "$1/harbor" && '
                    'python -m harbor.cli.main run --config "$2" --yes --quiet'
                ),
                "broker-container-test",
                str(harbor_root),
                str(config_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert _recorded_rewards(jobs_dir) == [1.0]
        assert client.list() == []


def _require_image(image):
    docker_client = docker.from_env()
    try:
        try:
            docker_client.images.get(image)
        except ImageNotFound:
            pytest.skip(f"development container image is not present: {image}")
    finally:
        docker_client.close()


def _harbor_checkout(repository):
    configured = os.environ.get("DSB_HARBOR_ROOT")
    checkout = Path(configured) if configured else repository.parent / "harbor-exploration"
    if not (checkout / "harbor/src/harbor").is_dir():
        pytest.skip("set DSB_HARBOR_ROOT to a harbor-exploration checkout")
    return checkout


def _recorded_rewards(jobs_dir):
    rewards = []
    for result_path in jobs_dir.rglob("result.json"):
        result = json.loads(result_path.read_text())
        verifier_result = result.get("verifier_result") or {}
        if reward := verifier_result.get("rewards"):
            rewards.extend(float(value) for value in reward.values())
    return rewards
