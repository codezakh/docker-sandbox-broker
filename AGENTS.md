# Development guide

This repository provides a local Docker sandbox broker and the consumer adapters
used by Harbor and OpenInstruct. Keep it independent from the sibling projects:
it has its own Git history, Python environment, dependencies, and tests.

## Tooling

- Use `uv` for dependency management and command execution. Add dependencies
  with `uv add`; do not maintain a parallel `pip` workflow.
- Support Python 3.12 and later.
- Use Pydantic models at API and configuration boundaries, FastAPI for HTTP
  endpoints, structlog for service logging, and `python-ulid` for object IDs.
- Keep the required dependency set free of consumer training stacks such as
  Torch, CUDA, and vLLM. Harbor and OpenInstruct integrations remain optional.

Run the standard checks before considering a code change complete:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run complexipy src --max-complexity-allowed 10
```

Use `uv run ruff format .` to format Python code. Do not silence lint or
complexity failures when a straightforward code improvement resolves them.

## Tests

Write tests as readable behavior specifications using `pytest-describe` and
`pytest-spec`:

```python
def describe_sandbox_lifecycle():
    def it_removes_the_sandbox_when_closed(client):
        """Deletes the broker-owned container and releases its identity."""
        ...
```

- Group behavior under `describe_*` functions and name examples `it_*`.
- Give each example a short docstring that states the observable behavior;
  pytest-spec uses it in the test report.
- Assert through public interfaces where practical. Avoid tests coupled to an
  implementation detail when the behavior can be observed externally.
- Add regression tests for bug fixes and both success and failure examples for
  new validation or API behavior.
- Do not weaken assertions or broadly skip tests to make a failure disappear.

The default suite excludes tests that use the local Docker daemon. Run those
explicitly when the change affects container behavior:

```bash
uv run pytest -m docker
```

The end-to-end consumer suites use optional dependency groups:

```bash
uv run --group integration-harbor pytest -m 'harbor and e2e'
uv run --group integration-openinstruct pytest -m 'openinstruct and e2e'
```

Use the existing markers (`docker`, `e2e`, `harbor`, `openinstruct`, `compose`,
`devcontainer`, and `terminalbench`) so expensive or environment-dependent tests
remain opt-in. The standard integration fixtures are deterministic and do not
require CUDA, vLLM, or a model server.

## Architecture and safety

- Preserve the provider boundary: Harbor and OpenInstruct own task, rollout,
  verifier, and reward orchestration; this package owns sandbox operations.
- Never expose the host Docker socket to a sandbox or consumer container.
- The broker may manage only resources carrying its own identity labels. Never
  introduce global Docker cleanup or delete unrelated images, containers,
  volumes, or networks.
- Privileged Docker-in-Docker is an explicit capability for approved Harbor
  Compose controllers. Do not enable it by default or generalize it to arbitrary
  sandbox requests.
- Keep consumer imports isolated to their adapter modules so importing the core
  package does not require Harbor or OpenInstruct.
- Maintain both Unix-socket and HTTP transports unless a change explicitly
  narrows the supported deployment model.

## Documentation

- `README.md` covers development, running the service, and test commands.
- `INTEGRATIONS.md` describes how Harbor and OpenInstruct use the broker.
- `SPEC.md` records the project scope, design, and safety model.

Update the relevant document when changing a public configuration value,
consumer integration, operational requirement, or architectural boundary.
