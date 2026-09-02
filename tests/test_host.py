import stat

import pytest

from docker_sandbox_broker.host import cleanup_runtime, prepare_runtime


def describe_host_runtime_for_dev_containers():
    def it_creates_private_credentials_and_a_container_environment(tmp_path):
        """Creates a private token and exports only the broker's consumer settings."""
        runtime = prepare_runtime(tmp_path / "runtime")

        lines = runtime.container_env.read_text().splitlines()

        assert lines == [
            f"DSB_AUTH_TOKEN={runtime.token}",
            f"DSB_SOCKET={runtime.socket}",
        ]
        assert stat.S_IMODE(runtime.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(runtime.token_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime.container_env.stat().st_mode) == 0o600

    def it_reuses_the_private_token_across_broker_restarts(tmp_path):
        """Keeps a stable token so separately launched containers can reconnect."""
        directory = tmp_path / "runtime"

        first = prepare_runtime(directory)
        second = prepare_runtime(directory)

        assert second.token == first.token

    def it_refuses_to_replace_an_unexpected_socket_path(tmp_path):
        """Refuses to unlink a regular file occupying the expected socket path."""
        directory = tmp_path / "runtime"
        directory.mkdir()
        (directory / "broker.sock").write_text("belongs to something else")

        with pytest.raises(RuntimeError, match="refusing to replace"):
            prepare_runtime(directory)

    def it_removes_only_ephemeral_runtime_files_when_the_server_stops(tmp_path):
        """Removes the socket and env export while retaining the private token."""
        runtime = prepare_runtime(tmp_path / "runtime")
        runtime.socket.touch()

        cleanup_runtime(runtime)

        assert runtime.token_file.exists()
        assert not runtime.container_env.exists()
        assert not runtime.socket.exists()
