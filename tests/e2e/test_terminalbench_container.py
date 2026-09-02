import json
import os
import subprocess
from pathlib import Path

import docker
import pytest
from docker.errors import ImageNotFound

pytestmark = [
    pytest.mark.docker,
    pytest.mark.e2e,
    pytest.mark.devcontainer,
    pytest.mark.harbor,
    pytest.mark.terminalbench,
]

EXPECTED_ORACLE_REWARD = 0.6051282051282051


def describe_real_terminal_bench_task_from_the_development_container():
    def it_records_the_tasks_known_oracle_reward_through_the_host_broker(
        host_broker_runtime, tmp_path
    ):
        """Runs bash-log-processor-fix with its shipped oracle and real verifier."""
        if os.environ.get("DSB_RUN_TERMINALBENCH") != "true":
            pytest.skip("set DSB_RUN_TERMINALBENCH=true to run the real task")
        runtime, client = host_broker_runtime
        image = os.environ.get("DSB_DEV_CONTAINER_IMAGE", "harbor-exploration-project")
        _require_image(image)
        repository = Path(__file__).parents[2]
        harbor_root = _harbor_checkout(repository)
        dataset = harbor_root / "OpenThoughts-TBLite"
        work_dir = tmp_path / "terminalbench-work"
        jobs_dir = work_dir / "jobs"
        work_dir.mkdir()
        config_path = work_dir / "harbor.json"
        config_path.write_text(
            json.dumps(
                {
                    "job_name": "broker-terminalbench-canary",
                    "jobs_dir": str(jobs_dir),
                    "n_attempts": 1,
                    "n_concurrent_trials": 1,
                    "environment": {
                        "import_path": "docker_sandbox_broker.harbor:LocalBrokerEnvironment"
                    },
                    "agents": [{"name": "oracle"}],
                    "datasets": [{"path": str(dataset), "task_names": ["bash-log-processor-fix"]}],
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
                "broker-terminalbench-test",
                str(harbor_root),
                str(config_path),
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        # The shipped solve.sh joins grouped paths with a literal ``\n`` and
        # therefore earns partial credit even as an oracle. Keep that observed
        # baseline explicit so it cannot hide a provider regression.
        assert _recorded_rewards(jobs_dir) == pytest.approx([EXPECTED_ORACLE_REWARD])
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
    if not (checkout / "OpenThoughts-TBLite/bash-log-processor-fix").is_dir():
        pytest.skip("set DSB_HARBOR_ROOT to a checkout containing OpenThoughts-TBLite")
    return checkout


def _recorded_rewards(jobs_dir):
    rewards = []
    for result_path in jobs_dir.rglob("result.json"):
        result = json.loads(result_path.read_text())
        verifier_result = result.get("verifier_result") or {}
        if reward := verifier_result.get("rewards"):
            rewards.extend(float(value) for value in reward.values())
    return rewards
