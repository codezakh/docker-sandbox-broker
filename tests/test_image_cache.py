from dataclasses import dataclass

from docker.errors import DockerException, ImageNotFound

from docker_sandbox_broker.image_cache import ImageCache, ImageInventory
from docker_sandbox_broker.models import CreateSandboxRequest
from docker_sandbox_broker.runtime import DockerRuntime


@dataclass
class FakeImage:
    id: str


class FakeImages:
    def __init__(self, images=None):
        self.by_reference = images or {}
        self.pull_failures = []
        self.pull_calls = []
        self.removed = []

    def get(self, reference):
        try:
            return self.by_reference[reference]
        except KeyError as error:
            raise ImageNotFound(reference) from error

    def pull(self, reference):
        self.pull_calls.append(reference)
        if self.pull_failures:
            raise self.pull_failures.pop(0)
        image = FakeImage(f"sha256:pulled-{len(self.pull_calls)}")
        self.by_reference[reference] = image
        return image

    def remove(self, reference):
        if reference not in self.by_reference:
            raise ImageNotFound(reference)
        self.removed.append(reference)
        self.by_reference.pop(reference)


class FakeContainer:
    def __init__(self, manager, container_id, image_id, labels):
        self._manager = manager
        self.id = container_id
        self.labels = labels
        self.attrs = {"Image": image_id, "State": {"Status": "running"}}
        self.status = "running"

    def start(self):
        pass

    def reload(self):
        pass

    def remove(self, **_kwargs):
        self._manager.by_id.pop(self.id, None)


class FakeContainers:
    def __init__(self, images):
        self._images = images
        self.by_id = {}

    def create(self, reference, **kwargs):
        image = self._images.get(reference)
        container_id = f"container-{len(self.by_id) + 1}"
        container = FakeContainer(self, container_id, image.id, kwargs["labels"])
        self.by_id[container_id] = container
        return container

    def get(self, container_id):
        return self.by_id[container_id]

    def list(self, *, all):
        assert all
        return list(self.by_id.values())


class FakeDockerClient:
    def __init__(self, images=None):
        self.images = FakeImages(images)
        self.containers = FakeContainers(self.images)


class FreeSpace:
    def __init__(self, *values):
        self._values = list(values)

    def __call__(self):
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def describe_broker_owned_image_inventory():
    def it_persists_only_images_explicitly_recorded_by_the_broker(tmp_path):
        """Persists broker image ownership in an atomic node-local JSON inventory."""
        state_path = tmp_path / "images.json"
        client = FakeDockerClient()
        cache = ImageCache(client, state_path, 10, 20, 0, free_bytes=FreeSpace(100))

        cache.record("registry/task:one", "sha256:one", "pulled")
        restored = ImageCache(client, state_path, 10, 20, 0, free_bytes=FreeSpace(100))

        inventory = ImageInventory.model_validate_json(state_path.read_bytes())
        assert [(item.reference, item.origin) for item in inventory.images] == [
            ("registry/task:one", "pulled")
        ]
        assert restored.collect_if_needed() == []
        assert state_path.stat().st_mode & 0o077 == 0

    def it_collects_unused_owned_images_oldest_first_and_preserves_active_images(tmp_path):
        """Reclaims LRU broker images while preserving active and unrecorded images."""
        images = {
            "owned:old": FakeImage("sha256:old"),
            "owned:active": FakeImage("sha256:active"),
            "owned:new": FakeImage("sha256:new"),
            "unrecorded:keep": FakeImage("sha256:unrecorded"),
        }
        client = FakeDockerClient(images)
        client.containers.create("owned:active", labels={})
        free_space = FreeSpace(0, 50, 100)
        cache = ImageCache(client, tmp_path / "images.json", 10, 100, 0, free_bytes=free_space)
        cache.record("owned:old", "sha256:old", "pulled")
        cache.record("owned:active", "sha256:active", "pulled")
        cache.record("owned:new", "sha256:new", "built")

        removed = cache.collect_if_needed()

        assert removed == ["owned:old", "owned:new"]
        assert client.images.removed == ["owned:old", "owned:new"]
        assert set(client.images.by_reference) == {"owned:active", "unrecorded:keep"}

    def it_forgets_a_record_when_its_tag_now_points_to_another_image(tmp_path):
        """Preserves a mutable tag when it no longer identifies the image the broker pulled."""
        client = FakeDockerClient({"mutable:latest": FakeImage("sha256:original")})
        state_path = tmp_path / "images.json"
        cache = ImageCache(client, state_path, 10, 20, 0, free_bytes=FreeSpace(0))
        cache.record("mutable:latest", "sha256:original", "pulled")
        client.images.by_reference["mutable:latest"] = FakeImage("sha256:replacement")

        removed = cache.collect_if_needed()

        assert removed == []
        assert client.images.removed == []
        assert ImageInventory.model_validate_json(state_path.read_bytes()).images == []

    def it_protects_an_image_reserved_for_container_creation(tmp_path):
        """Keeps a resolved image until its container becomes visible to Docker."""
        client = FakeDockerClient({"owned:reserved": FakeImage("sha256:reserved")})
        cache = ImageCache(
            client,
            tmp_path / "images.json",
            10,
            20,
            0,
            free_bytes=FreeSpace(0),
        )
        cache.record("owned:reserved", "sha256:reserved", "pulled")

        with cache.reserve("sha256:reserved"):
            while_reserved = cache.collect_if_needed()
        after_release = cache.collect_if_needed()

        assert while_reserved == []
        assert after_release == ["owned:reserved"]

    def it_gives_newly_acquired_images_a_creation_grace_period(tmp_path):
        """Preserves a newly acquired image long enough for a separate create request."""
        client = FakeDockerClient({"owned:new": FakeImage("sha256:new")})
        cache = ImageCache(
            client,
            tmp_path / "images.json",
            10,
            20,
            300,
            free_bytes=FreeSpace(0),
        )
        cache.record("owned:new", "sha256:new", "built")

        removed = cache.collect_if_needed()

        assert removed == []
        assert client.images.removed == []


def describe_runtime_image_acquisition():
    def it_records_an_image_only_when_the_broker_pulls_it(tmp_path):
        """Claims a missing image after pulling it without claiming a preexisting image."""
        client = FakeDockerClient({"preexisting:latest": FakeImage("sha256:preexisting")})
        state_path = tmp_path / "images.json"
        cache = ImageCache(client, state_path, 10, 20, 0, free_bytes=FreeSpace(100))
        runtime = DockerRuntime("test", client=client, image_cache=cache)

        existing = runtime.create("existing", CreateSandboxRequest(image="preexisting:latest"))
        pulled = runtime.create("pulled", CreateSandboxRequest(image="missing:latest"))
        runtime.delete(existing.id, "existing", "test")
        runtime.delete(pulled.id, "pulled", "test")

        inventory = ImageInventory.model_validate_json(state_path.read_bytes())
        assert [item.reference for item in inventory.images] == ["missing:latest"]
        assert client.images.pull_calls == ["missing:latest"]

    def it_collects_once_and_retries_a_pull_that_runs_out_of_space(tmp_path):
        """Retries one failed pull after emergency collection of an unused owned image."""
        client = FakeDockerClient({"owned:old": FakeImage("sha256:old")})
        client.images.pull_failures.append(DockerException("no space left on device"))
        cache = ImageCache(
            client,
            tmp_path / "images.json",
            10,
            20,
            0,
            free_bytes=FreeSpace(100),
        )
        cache.record("owned:old", "sha256:old", "pulled")
        runtime = DockerRuntime("test", client=client, image_cache=cache)

        sandbox = runtime.create("new", CreateSandboxRequest(image="missing:latest"))

        assert sandbox.state == "running"
        assert client.images.pull_calls == ["missing:latest", "missing:latest"]
        assert client.images.removed == ["owned:old"]
