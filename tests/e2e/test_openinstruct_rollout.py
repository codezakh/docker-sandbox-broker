import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.e2e, pytest.mark.openinstruct]


def describe_openinstruct_rollout_through_the_live_broker():
    @pytest.fixture
    def anyio_backend():
        return "asyncio"

    @pytest.fixture
    def openinstruct_types(monkeypatch):
        checkout = _openinstruct_checkout()
        monkeypatch.syspath_prepend(str(checkout))
        try:
            from open_instruct.environments.base import EnvCall
            from open_instruct.environments.swerl_sandbox import SWERLSandboxEnv
        except ImportError as error:
            pytest.skip(f"lightweight OpenInstruct integration dependencies missing: {error}")
        return EnvCall, SWERLSandboxEnv

    @pytest.mark.anyio
    async def it_resets_executes_submits_and_scores_a_deterministic_task(
        live_broker,
        openinstruct_types,
        monkeypatch,
    ):
        """Runs OpenInstruct reset, bash tool, submission, and reward through the broker."""
        _client, socket_path, token = live_broker
        EnvCall, SWERLSandboxEnv = openinstruct_types
        task_data = Path(__file__).parents[1] / "fixtures/openinstruct-task"
        environment = SWERLSandboxEnv(
            backend="local_broker",
            task_data_dir=str(task_data),
            image="bash:5.2",
            timeout=30,
            mem_limit="128m",
        )
        monkeypatch.setenv("DSB_AUTH_TOKEN", token)
        monkeypatch.setenv("DSB_SOCKET", str(socket_path))
        try:
            initial, tools = await environment.reset(
                task_id="tiny",
                image="bash:5.2",
                max_steps=2,
            )
            result = await environment.step(
                EnvCall(
                    id="deterministic-call",
                    name="bash",
                    args={
                        "command": (
                            "printf openinstruct-ok > /workspace/answer.txt; "
                            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
                        )
                    },
                )
            )
        finally:
            await environment.close()

        assert "Create `/workspace/answer.txt`" in initial.result
        assert tools[0]["function"]["name"] == "bash"
        assert result.done
        assert result.reward == 1.0


def _openinstruct_checkout():
    configured = os.environ.get("DSB_OPENINSTRUCT_ROOT")
    if configured:
        checkout = Path(configured)
    else:
        checkout = Path(__file__).parents[3] / "world-model-tmax/training/open-instruct"
    if not (checkout / "open_instruct").is_dir():
        pytest.skip("set DSB_OPENINSTRUCT_ROOT to an OpenInstruct checkout")
    return checkout
