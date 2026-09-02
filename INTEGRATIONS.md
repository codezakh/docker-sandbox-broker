# Consumer integrations

Harbor and OpenInstruct keep responsibility for task orchestration. The Docker
Sandbox Broker replaces only the sandbox provider: it creates containers and
provides process and filesystem operations over an authenticated HTTP API.

The broker package must be installed in the same Python environment as the
consumer. Both integrations connect using `DSB_AUTH_TOKEN` and one of:

- `DSB_SOCKET`, for a Unix socket shared with a local process or development
  container.
- `DSB_URL`, for a broker exposed over HTTP.

See [Running](README.md#running) for how to start the broker and make its socket
and credentials available to a consumer.

For local development, add a checkout as an editable dependency from the
consumer project:

```bash
uv add --editable /path/to/docker-sandbox-broker
```

Once the broker repository is public, it can be installed directly from Git:

```bash
uv add "docker-sandbox-broker @ git+https://github.com/<owner>/docker-sandbox-broker.git"
```

A tag or commit can be appended to the URL when the consuming project should
use a specific broker revision:

```bash
uv add "docker-sandbox-broker @ git+https://github.com/<owner>/docker-sandbox-broker.git@v0.1.0"
```

## Harbor

The broker package provides Harbor's custom environment implementation at
`docker_sandbox_broker.harbor:LocalBrokerEnvironment`. Harbor can load it through
its standard `environment.import_path` configuration:

```json
{
  "environment": {
    "import_path": "docker_sandbox_broker.harbor:LocalBrokerEnvironment"
  }
}
```

No Harbor source changes are required. The environment supports tasks backed by
a Dockerfile, a prebuilt image, or Docker Compose. Compose tasks use Harbor's
existing Docker-in-Docker orchestration and require the broker to be started with
Docker-enabled sandboxes allowed:

```bash
uv run docker-sandbox-broker-host --allow-docker
```

## OpenInstruct

OpenInstruct selects sandbox providers by name in its `create_backend` factory;
it does not currently have an external backend import setting like Harbor. Its
integration therefore consists of a `LocalBrokerBackend` adapter in this package
and a small registration in OpenInstruct's backend factory.

The adapter is provided at
`docker_sandbox_broker.openinstruct.LocalBrokerBackend`. It implements
OpenInstruct's `SandboxBackend` interface by translating sandbox lifecycle,
command execution, and file transfer calls to `BrokerClient`. It accepts the
same `image`, `timeout`, and `mem_limit` values that the OpenInstruct sandbox
environments already pass to their backends.

OpenInstruct's `create_backend` function needs a `local_broker` case that loads
the adapter lazily:

```python
if backend_type == "local_broker":
    broker_module = import_module("docker_sandbox_broker.openinstruct")
    return broker_module.LocalBrokerBackend(**kwargs)
```

This requires `from importlib import import_module` with OpenInstruct's other
imports. Lazy loading allows the adapter to use OpenInstruct's `SandboxBackend`,
`ExecutionResult`, and `SandboxOOMError` types without creating a module import
cycle. Once registered, an OpenInstruct sandbox environment selects it with:

```python
environment = SWERLSandboxEnv(
    backend="local_broker",
    image="example/image:tag",
    timeout=120,
    mem_limit="4g",
    # Existing task-data configuration...
)
```

The broker does not replace OpenInstruct's dataset loading, environment pooling,
rollout, or reward logic. Those continue to run in OpenInstruct and call the
adapter through its existing backend interface.

## Dependencies

Harbor and OpenInstruct remain optional consumers of the broker package. The
broker does not install either project; instead, it is installed alongside the
consumer that will load its adapter. Its runtime dependencies are Docker,
FastAPI, HTTPX, Pydantic, python-ulid, structlog, and Uvicorn. The broker itself
does not depend on Torch, CUDA, or vLLM.
