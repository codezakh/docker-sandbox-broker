"""Safe host launcher for development-container consumers."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.logging import configure_logging

SOCKET_NAME = "broker.sock"
TOKEN_NAME = "token"
CONTAINER_ENV_NAME = "container.env"


@dataclass(frozen=True)
class HostRuntime:
    directory: Path
    socket: Path
    token_file: Path
    container_env: Path
    token: str


def default_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "docker-sandbox-broker"
    return Path("/tmp") / f"docker-sandbox-broker-{os.getuid()}"


def prepare_runtime(directory: Path, token: str | None = None) -> HostRuntime:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_owned_directory(directory)
    socket_path = directory / SOCKET_NAME
    _remove_stale_socket(socket_path)
    token_file = directory / TOKEN_NAME
    active_token = token or _load_or_create_token(token_file)
    if len(active_token) < 16 or "\n" in active_token:
        raise RuntimeError("broker token must be a single line containing at least 16 characters")
    container_env = directory / CONTAINER_ENV_NAME
    _atomic_private_write(
        container_env,
        f"DSB_AUTH_TOKEN={active_token}\nDSB_SOCKET={socket_path}\n",
    )
    return HostRuntime(directory, socket_path, token_file, container_env, active_token)


def cleanup_runtime(runtime: HostRuntime) -> None:
    runtime.container_env.unlink(missing_ok=True)
    runtime.socket.unlink(missing_ok=True)


def _secure_owned_directory(directory: Path) -> None:
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"broker runtime directory is not owned by this user: {directory}")
    directory.chmod(0o700)


def _load_or_create_token(path: Path) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_private_file(path)
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w") as token_file:
        token_file.write(f"{token}\n")
    return token


def _read_private_file(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"broker token file must be private and owned by this user: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as source:
        return source.read().strip()


def _atomic_private_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as target:
            target.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"refusing to replace non-owned broker socket path: {path}")
    if _socket_is_live(path):
        raise RuntimeError(f"a broker is already listening on {path}")
    path.unlink()


def _socket_is_live(path: Path) -> bool:
    transport = httpx.HTTPTransport(uds=str(path))
    try:
        with httpx.Client(
            transport=transport,
            base_url="http://localhost",
            timeout=0.5,
        ) as client:
            client.get("/health")
            return True
    except (httpx.HTTPError, OSError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the broker for local dev containers")
    parser.add_argument("--runtime-dir", type=Path, default=default_runtime_dir())
    parser.add_argument(
        "--allow-docker",
        action="store_true",
        help="allow approved privileged DinD controllers for Harbor Compose tasks",
    )
    args = parser.parse_args()

    runtime = prepare_runtime(args.runtime_dir, os.environ.get("DSB_AUTH_TOKEN"))
    previous_umask = None
    try:
        os.environ["DSB_AUTH_TOKEN"] = runtime.token
        os.environ.setdefault("DSB_BROKER_ID", f"host-{os.getuid()}")
        if args.allow_docker:
            os.environ["DSB_ALLOW_DOCKER_ENABLED"] = "true"
        configure_logging()
        settings = BrokerSettings.from_environment()
        previous_umask = os.umask(0o077)
        uvicorn.run(create_app(settings=settings), uds=str(runtime.socket))
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)
        cleanup_runtime(runtime)


if __name__ == "__main__":
    main()
