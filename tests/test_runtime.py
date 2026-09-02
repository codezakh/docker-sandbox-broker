import io
import tarfile
from types import SimpleNamespace

import pytest

from docker_sandbox_broker.errors import RuntimeOperationError, SandboxOwnershipError
from docker_sandbox_broker.runtime import DockerRuntime


def describe_archive_validation():
    def _archive(name):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        return stream.getvalue()

    def it_rejects_parent_traversal():
        """Rejects an archive member that could traverse its requested root."""
        runtime = DockerRuntime("test", client=SimpleNamespace())

        with pytest.raises(RuntimeOperationError, match="unsafe archive path"):
            runtime.put_archive("runtime", "/", _archive("../escape"))

    def it_rejects_parent_traversal_in_a_build_context():
        """Rejects a Docker build context that could traverse broker storage."""
        runtime = DockerRuntime("test", client=SimpleNamespace())

        with pytest.raises(RuntimeOperationError, match="unsafe archive path"):
            runtime.build_image("build-id", "Dockerfile", _archive("../escape"))


def describe_owned_deletion():
    def it_refuses_to_delete_a_container_without_exact_labels():
        """Refuses deletion when exact broker ownership labels do not match."""
        container = SimpleNamespace(
            labels={"unrelated": "container"},
            remove=lambda **_kwargs: None,
        )
        client = SimpleNamespace(
            containers=SimpleNamespace(get=lambda _runtime_id: container),
        )
        runtime = DockerRuntime("our-broker", client=client)

        with pytest.raises(SandboxOwnershipError, match="ownership labels"):
            runtime.delete("runtime-id", "sandbox-id", "our-broker")
