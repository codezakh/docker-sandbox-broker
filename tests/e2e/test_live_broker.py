import pytest

from docker_sandbox_broker.models import CreateSandboxRequest

pytestmark = [pytest.mark.docker, pytest.mark.e2e]


def describe_live_broker_transport():
    def it_runs_a_complete_sandbox_lifecycle_through_a_unix_socket(live_broker):
        """Runs lifecycle, process, and filesystem operations through a live broker."""
        client, _socket_path, _token = live_broker
        sandbox = client.create(CreateSandboxRequest(image="alpine:3.20", run_id="e2e-direct"))
        try:
            executed = sandbox.process.exec("printf live-ok")
            sandbox.fs.upload_file(b"file-ok", "/tmp/message.txt")
            downloaded = sandbox.fs.download_file("/tmp/message.txt")
        finally:
            sandbox.delete()

        assert executed.stdout == "live-ok"
        assert downloaded == b"file-ok"
        assert client.list() == []
