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

# Opt-in real OpenThoughts-TBLite task from the development-container image.
DSB_RUN_TERMINALBENCH=true uv run pytest -m terminalbench \
  tests/e2e/test_terminalbench_container.py
```

The OpenInstruct test uses the sibling `world-model-tmax/training/open-instruct`
checkout by default. Set `DSB_OPENINSTRUCT_ROOT` to test another checkout. The
optional dependency group deliberately contains only the environment/rollout
imports needed by this deterministic test; it does not install the fork's
training stack.

The real canary currently uses `bash-log-processor-fix`. Its shipped oracle has
a filename-grouping bug and reliably earns partial reward rather than `1.0`;
the test records that known baseline while still requiring a clean Harbor run,
real verifier output, and complete broker cleanup.

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

For the Harbor development-container workflow, use the host launcher instead:

```bash
uv run docker-sandbox-broker-host --allow-docker
```

It creates a user-private runtime directory under `XDG_RUNTIME_DIR` (or a
UID-scoped `/tmp` fallback), keeps a stable mode-`0600` token there, and exposes
`broker.sock` plus a Docker-compatible `container.env` only while serving. A
container should mount that runtime directory read-only and pass
`--env-file .../container.env`; it must never mount the host Docker socket.

The Harbor sandbox launcher discovers this runtime automatically. It also
mounts a sibling `docker-sandbox-broker` checkout read-only and adds its `src`
directory to `PYTHONPATH`, keeping the two uv projects independent. Override
the defaults with `DSB_RUNTIME_DIR` or `DSB_SOURCE_DIR` when the checkouts live
elsewhere.

Docker-enabled sandboxes use privileged Docker-in-Docker with host networking.
They are disabled unless `DSB_ALLOW_DOCKER_ENABLED=true` is set.

Every sandbox is given a deadline when it is created, and a background sweep
deletes the ones that pass it. This is what reclaims a sandbox whose client is
gone: a trainer that exits without closing its environment pool, a killed run,
or a crashed process. Nothing else does, and such sandboxes otherwise hold
memory and disk until the daemon is cleaned by hand.

The default is 30 minutes, configured with `DSB_SANDBOX_TTL_SECONDS`, and the
sweep runs every `DSB_SANDBOX_SWEEP_INTERVAL_SECONDS` seconds (default 60). A
request may set its own `ttl_seconds`. Set `DSB_SANDBOX_TTL_SECONDS=0` to
disable expiry and return to relying on clients to delete what they create.

The deadline is absolute. It is fixed when the sandbox is created and is not
extended by activity, so a task that legitimately runs longer than the default
must ask for a larger `ttl_seconds` up front or it will be deleted mid-run.
`SandboxView.expires_at` reports the deadline.

The broker records images it pulls or builds in node-local state under
`/var/tmp/docker-sandbox-broker-$UID` and reclaims unused owned images when the
Docker filesystem runs low on space. Configure this with `DSB_STATE_DIR`,
`DSB_IMAGE_GC_MIN_FREE_MB` (default 20480), `DSB_IMAGE_GC_TARGET_FREE_MB`
(default 40960), and `DSB_IMAGE_GC_MIN_AGE_SECONDS` (default 300). Set both
free-space values to `0` to disable collection. Do not place `DSB_STATE_DIR` on
NFS.

The host-network Compose compatibility overlay maps service names to localhost.
This works around the current host's Docker bridge DNS failure, but means Compose
jobs must run serially when their services bind the same ports.

## Consumer adapters

OpenInstruct selects `backend_type="local_broker"`. Harbor selects the custom
environment class `docker_sandbox_broker.harbor:LocalBrokerEnvironment`.
Both read `DSB_AUTH_TOKEN` and either `DSB_SOCKET` or `DSB_URL`.

See [Consumer integrations](INTEGRATIONS.md) for the Harbor configuration and
the small backend registration OpenInstruct requires.

The Harbor adapter supports direct Dockerfile, prebuilt-image, and Docker Compose
tasks. Compose reuses Harbor's DinD orchestration inside an approved privileged
controller; it does not expose the host Docker socket to Harbor.
