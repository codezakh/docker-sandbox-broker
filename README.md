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

## Consumer adapters

OpenInstruct selects `backend_type="local_broker"`. Harbor selects the custom
environment class `docker_sandbox_broker.harbor:LocalBrokerEnvironment`.
Both read `DSB_AUTH_TOKEN` and either `DSB_SOCKET` or `DSB_URL`.

The Harbor adapter currently supports direct Dockerfile and prebuilt-image
tasks. Compose task support remains the next adapter milestone; the broker's
privileged Docker-enabled primitive is already covered by a real-Docker test.
