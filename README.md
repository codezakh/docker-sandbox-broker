# Docker Sandbox Broker

A small, provider-shaped API that lets containerized clients request local Docker
sandboxes without receiving access to the host Docker socket.

The broker intentionally exposes only the lifecycle, process, and filesystem
operations used by Harbor and OpenInstruct. It owns only containers labeled with
its broker identity and never performs global Docker cleanup.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run complexipy src --max-complexity-allowed 10
```

Real-Docker tests are opt-in:

```bash
uv run pytest -m docker
```

The repository also owns four integration layers that exercise the provider and
its two consumers without CUDA, vLLM, or a model server:

```bash
# 1. Public broker API over a live Unix socket, including real Docker.
uv run pytest -m 'docker and e2e' tests/e2e/test_live_broker.py

# 2. Harbor's real CLI, oracle, and verifier against a tiny direct task.
uv run --group integration-harbor pytest -m 'harbor and e2e' \
  tests/e2e/test_harbor_oracle.py

# 3. OpenInstruct's real environment reset/tool/submission/reward path.
uv run --group integration-openinstruct pytest -m 'openinstruct and e2e' \
  tests/e2e/test_openinstruct_rollout.py

# 4. Harbor's real Compose orchestration through broker-provided DinD.
uv run --group integration-harbor pytest -m 'compose and e2e' \
  tests/e2e/test_harbor_compose.py
```

The OpenInstruct test uses the sibling `world-model-tmax/training/open-instruct`
checkout by default. Set `DSB_OPENINSTRUCT_ROOT` to test another checkout. The
optional dependency group deliberately contains only the environment/rollout
imports needed by this deterministic test; it does not install the fork's
training stack.

The default output is an executable behavior specification rendered by
pytest-describe and pytest-spec. The optional Harbor adapter contract can be run
with an environment that has Harbor installed:

```bash
python -m pytest tests/consumers/test_harbor_adapter.py
```

## Running

Set a bearer token and serve over a user-owned Unix socket:

```bash
export DSB_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run docker-sandbox-broker --uds "/run/user/$(id -u)/docker-sandbox-broker.sock"
```

Docker-enabled sandboxes use privileged Docker-in-Docker with host networking.
They are disabled unless `DSB_ALLOW_DOCKER_ENABLED=true` is set.

The host-network Compose compatibility overlay maps service names to localhost.
This works around the current host's Docker bridge DNS failure, but means Compose
jobs must run serially when their services bind the same ports.

## Consumer adapters

OpenInstruct selects `backend_type="local_broker"`. Harbor selects the custom
environment class `docker_sandbox_broker.harbor:LocalBrokerEnvironment`.
Both read `DSB_AUTH_TOKEN` and either `DSB_SOCKET` or `DSB_URL`.

The Harbor adapter supports direct Dockerfile, prebuilt-image, and Docker Compose
tasks. Compose reuses Harbor's DinD orchestration inside an approved privileged
controller; it does not expose the host Docker socket to Harbor.
