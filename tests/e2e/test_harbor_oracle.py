import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor.cli.main", reason="Harbor consumer environment is optional")

pytestmark = [pytest.mark.docker, pytest.mark.e2e, pytest.mark.harbor]


def describe_harbor_oracle_through_the_live_broker():
    def it_completes_a_repository_owned_task_with_full_reward(live_broker, tmp_path):
        """Runs Harbor's real oracle and verifier lifecycle through the broker."""
        client, socket_path, token = live_broker
        dataset = Path(__file__).parents[1] / "fixtures/harbor-dataset"
        jobs_dir = tmp_path / "jobs"
        config_path = tmp_path / "harbor.json"
        config_path.write_text(
            json.dumps(
                {
                    "job_name": "broker-harbor-e2e",
                    "jobs_dir": str(jobs_dir),
                    "n_attempts": 1,
                    "n_concurrent_trials": 1,
                    "environment": {
                        "import_path": ("docker_sandbox_broker.harbor:LocalBrokerEnvironment")
                    },
                    "agents": [{"name": "oracle"}],
                    "datasets": [{"path": str(dataset), "task_names": ["tiny-oracle"]}],
                }
            )
        )
        environment = {
            **os.environ,
            "DSB_AUTH_TOKEN": token,
            "DSB_SOCKET": str(socket_path),
        }

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "harbor.cli.main",
                "run",
                "--config",
                str(config_path),
                "--yes",
                "--quiet",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert _recorded_rewards(jobs_dir) == [1.0]
        assert client.list() == []


def _recorded_rewards(jobs_dir):
    rewards = []
    for result_path in jobs_dir.rglob("result.json"):
        result = json.loads(result_path.read_text())
        verifier_result = result.get("verifier_result") or {}
        if reward := verifier_result.get("rewards"):
            rewards.extend(float(value) for value in reward.values())
    return rewards
