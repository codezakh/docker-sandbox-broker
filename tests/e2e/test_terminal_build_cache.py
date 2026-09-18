import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.e2e, pytest.mark.devcontainer]
pytestmark.append(
    pytest.mark.skipif(
        not os.environ.get("DSB_TERMINAL_CONTAINER"),
        reason="requires a terminal-agents-rl container",
    )
)


def describe_terminal_agents_build_cache():
    def it_builds_from_separate_consumer_processes_and_executes_post_install_output(
        live_broker, tmp_path
    ):
        """Uses terminal-agents-rl inside its sandbox to build, reuse, change, and execute."""
        container = os.environ.get("DSB_TERMINAL_CONTAINER")
        host_workspace = os.environ.get("DSB_TERMINAL_HOST_WORKSPACE")
        container_workspace = os.environ.get("DSB_TERMINAL_CONTAINER_WORKSPACE")
        if not all((container, host_workspace, container_workspace)):
            pytest.skip("set DSB_TERMINAL_{CONTAINER,HOST_WORKSPACE,CONTAINER_WORKSPACE}")
        client, socket, token = live_broker

        def translated(path):
            if path.is_relative_to(Path(host_workspace)):
                return str(Path(container_workspace) / path.relative_to(Path(host_workspace)))
            return str(path)

        directory = tmp_path / "environment"
        directory.mkdir()
        (directory / "Dockerfile").write_text(
            "FROM alpine:3.20\nCOPY post_install.sh /tmp/setup\nRUN sh /tmp/setup\n"
        )
        script = directory / "post_install.sh"
        script.write_text("printf first > /marker\n")
        program = """
import json
import sys
from pathlib import Path
from terminal_agents_rl.task_images import build_context
from terminal_agents_rl.broker import sandbox_client
from docker_sandbox_broker.models import CreateSandboxRequest

assert not Path('/var/run/docker.sock').exists()
tag = build_context(Path(sys.argv[1]))
with sandbox_client() as client:
    box = client.create(CreateSandboxRequest(image=tag))
    try:
        result = box.process.exec('cat /marker')
        assert result.exit_code == 0
        print(json.dumps({'tag': tag, 'marker': result.stdout}))
    finally:
        box.delete()
"""

        def run():
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"DSB_SOCKET={translated(socket)}",
                    "-e",
                    "DSB_URL=",
                    "-e",
                    f"DSB_AUTH_TOKEN={token}",
                    container,
                    "/sandbox/project/venv/bin/python",
                    "-c",
                    program,
                    translated(directory),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        first, repeated = run(), run()
        assert first == repeated
        assert first["marker"] == "first"
        script.write_text("printf changed > /marker\n")
        changed = run()
        assert changed["tag"] != first["tag"]
        assert changed["marker"] == "changed"
        assert client.list() == []
        logs = [
            json.loads(line)
            for line in (tmp_path / "broker.log").read_text().splitlines()
            if line.startswith("{")
        ]
        events = [item.get("event") for item in logs]
        assert events.count("image_built") == 2
        assert events.count("image_cache_hit") == 1
        assert events.count("build_started") == 3
        assert events.count("build_finished") == 3
        builds = [item for item in logs if item.get("operation") == "docker_build"]
        assert len(builds) == 2
        assert all(item["duration_seconds"] >= 0 for item in builds)
        for build in builds:
            related = {
                item["event"] for item in logs if item.get("request_id") == build["request_id"]
            }
            assert {"build_started", "build_finished", "request_finished"} <= related
