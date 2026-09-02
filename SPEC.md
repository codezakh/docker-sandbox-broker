# Docker Sandbox Broker: Initial Specification

## Purpose

Provide a small local sandbox-provider service that allows clients running inside
development containers to create and operate task containers on the host without
giving those clients direct access to the host Docker socket.

The initial consumers are:

- Harbor, including the Terminal-Bench affordances used by this project.
- The `training/open-instruct` fork in `world-model-tmax`.

The broker emulates the high-level functions those consumers use from providers
such as Modal and Daytona. It does not attempt to reproduce either provider's
wire protocol, full SDK, account model, or unrelated product surface.

## Design principles

1. **Reuse consumer orchestration.** Harbor and OpenInstruct continue to own task
   loading, agent loops, verifier timing, Compose orchestration, and reward logic.
2. **Keep the provider primitive small.** The broker supplies sandbox lifecycle,
   process execution, filesystem transfer, status, and cleanup.
3. **Deny ambient Docker authority.** Development containers never receive the
   host Docker socket.
4. **Delete only what we own.** Destructive operations require an exact broker
   sandbox identity and matching Docker ownership labels. Never prune globally or
   delete resources by an unscoped name or broad label.
5. **Make cleanup idempotent and crash-tolerant.** Explicit close is the primary
   path; leases and broker-owned labels will provide recovery from dead clients.
6. **Prefer understandable code.** Keep cognitive complexity at or below the
   configured Complexipy threshold and favor small, typed components.
7. **Keep repositories independent.** This is a sibling Git repository under the
   project umbrella, with its own uv environment and history.

## Provider-level capability contract

The broker and its Python client provide only the functions observed to be needed
by Harbor and OpenInstruct:

- Create a sandbox from a prebuilt OCI image.
- Execute a command with optional timeout, environment, user, and working directory.
- Upload and download a file.
- Upload a tar archive rooted at a sandbox path.
- Upload and download directories through safe archive transfer.
- Inspect sandbox state.
- Wait for termination when a consumer requires it.
- Delete a sandbox idempotently.
- List sandboxes owned by the authenticated broker/client scope for recovery.
- Create a Docker-enabled sandbox for Harbor's existing DinD Compose strategy.

The client should feel provider-shaped, for example:

```python
sandbox = client.create(...)
sandbox.process.exec(...)
sandbox.fs.upload_file(...)
sandbox.fs.download_file(...)
sandbox.delete()
```

## Execution modes

### Direct mode

The broker starts one task container directly on the host daemon. This mode serves:

- OpenInstruct TMax training tasks backed by prebuilt images.
- OpenThoughts-TBLite tasks.
- Harbor tasks that require one container.

Direct task containers are unprivileged and receive no Docker socket, host devices,
host PID namespace, or arbitrary host bind mounts.

### Docker-enabled mode

For Harbor tasks with Docker Compose, the broker starts an approved privileged
Docker-in-Docker controller image. Harbor reuses its existing DinD orchestration:

1. Wait for the inner Docker daemon.
2. Upload task environments and generated Compose overlays.
3. Build and start the Compose project inside the sandbox.
4. Address the `main` and sidecar services through Harbor's existing operations.
5. Run the verifier and retrieve artifacts.
6. Bring down Compose and delete the outer sandbox.

On the current host, Docker-enabled sandboxes require host networking. Testing
showed ordinary Docker bridge containers could not resolve external DNS, while a
privileged DinD controller using host networking successfully provided nested HTTP
and registry access.

Docker-enabled mode is opt-in, restricted to approved images, and must never be
enabled merely because a client supplied a privileged Docker option.

## Safety and policy invariants

- Authenticate every mutating or sandbox-reading API operation.
- Bind the production service to a user-owned Unix socket by default.
- Apply a maximum CPU, memory, output, upload, download, and command-timeout policy.
- Reject absolute or parent-traversing archive member paths and special device entries.
- Do not accept arbitrary Docker API options from clients.
- Do not permit arbitrary privileged images, host mounts, devices, capabilities,
  host PID, or the host Docker socket.
- Label every owned container with broker ID and sandbox ULID.
- Before deletion, re-read and validate all ownership labels on the exact container.
- Never invoke Docker system prune, broad image cleanup, or unscoped container cleanup.
- Use run IDs and, in a later milestone, leases for scoped crash recovery.
- Treat command retries carefully: do not repeat a command unless it is known not to
  have started.

## Identity

Use `python-ulid` for externally visible sandbox and run-related object identities.
Docker runtime IDs remain internal implementation details.

## API shape

Initial versioned endpoints:

```text
GET    /health
POST   /v1/images/build?dockerfile=Dockerfile
POST   /v1/sandboxes
GET    /v1/sandboxes
GET    /v1/sandboxes/{sandbox_id}
POST   /v1/sandboxes/{sandbox_id}/exec
PUT    /v1/sandboxes/{sandbox_id}/files?path=/absolute/path
GET    /v1/sandboxes/{sandbox_id}/files?path=/absolute/path
PUT    /v1/sandboxes/{sandbox_id}/archives?root=/absolute/path
DELETE /v1/sandboxes/{sandbox_id}
```

Use raw bodies for files and tar archives rather than base64 JSON payloads.
Return stable, typed error codes and retryability metadata.

## Consumer integrations

### OpenInstruct

Implement a thin `SandboxBackend` that maps its six methods to the local provider
client. OpenInstruct retains dataset handling, sandbox pooling, tool execution,
submission handling, verifier upload, reward parsing, and retries.

### Harbor

Implement a custom `BaseEnvironment` provider with:

- A direct strategy for prebuilt-image and Dockerfile tasks.
- A DinD strategy that reuses Harbor's existing Compose operations.

Harbor retains task compilation, agent/verifier orchestration, generated overlays,
service addressing, Compose behavior, artifact handling, and trial lifecycle.

## Testing ladder

1. **Unit tests:** fake runtime; authentication, validation, identities, safety,
   lifecycle, typed errors, and ownership checks.
2. **Real direct Docker contract:** use a tiny image to test create, exec, timeout,
   file/archive transfer, inspect, delete, and absence of leaked containers.
3. **Docker-enabled contract:** launch approved DinD, start a nested tiny container,
   verify networking and Compose availability, then remove the outer sandbox.
4. **Synthetic consumer tasks:** one OpenInstruct-shaped seed/test/reward fixture and
   one Harbor-shaped Dockerfile/instruction/tests/solution fixture.
5. **Real Harbor task:** run an inexpensive TB-Lite task with Harbor's oracle agent,
   compare with native Docker, and require equivalent reward/artifacts/cleanup.
6. **Real deterministic OpenInstruct task:** set up the labmate fork's isolated
   OpenInstruct environment and drive known bash calls without a model or GPU.
7. **Compose canary:** run a modest multi-service Terminal-Bench oracle task.
8. **Generated rollout:** only after the preceding tests pass, add vLLM, a model,
   Ray, GPUs, and progressively scale to the documented training smoke shape.

Real-Docker tests are opt-in and may create only explicitly named, broker-labeled
resources. Test cleanup must target exact IDs and run even after assertion failures.

Tests use behavior-oriented structure throughout:

- `describe_*` blocks name the subject or context.
- Nested `describe_when_*` blocks name meaningful state transitions.
- Individual behaviors have `it_*` names and sentence docstrings.
- pytest-spec renders the docstring as the specification line.
- Helper failures describe the violated behavior, not merely the helper implementation.
- Both test environments declare `pytest-describe` and `pytest-spec`; `--tb=short`
  keeps failure diagnostics compact beneath the failed behavior.

## Tooling and project conventions

- Use `uv` for dependency and environment management.
- Use Pydantic for API and configuration models.
- Use FastAPI for the broker HTTP API.
- Use pytest for testing, with pytest-describe for nested behavior-oriented tests
  and pytest-spec for readable output.
- Use structlog for structured logs.
- Use Ruff for formatting and linting.
- Use Complexipy to enforce low cognitive complexity.
- Use `python-ulid` for generated object identities.
- Support Python 3.12 or newer.

## Initial non-goals

- Full Modal or Daytona wire/API compatibility.
- A general-purpose Docker API proxy.
- Arbitrary host bind mounts or privileged container creation.
- GPU allocation inside task containers.
- Windows containers.
- Distributed scheduling or multi-host placement.
- Reimplementing Harbor's task, Compose, agent, verifier, or reward orchestration.
- Reimplementing OpenInstruct's rollout or environment-pool orchestration.
