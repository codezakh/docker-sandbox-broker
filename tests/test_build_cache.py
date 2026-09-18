import io
import tarfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import DockerException, ImageNotFound

from docker_sandbox_broker.errors import RuntimeOperationError, SandboxOwnershipError
from docker_sandbox_broker.image_cache import ImageCache, ImageInventory
from docker_sandbox_broker.runtime import DockerRuntime


def context(script=b"echo first > /marker"):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in {
            "Dockerfile": b"FROM alpine:3.20\nCOPY post_install.sh /tmp/setup\nRUN sh /tmp/setup\n",
            "Dockerfile.other": b"FROM alpine:3.20\n",
            "post_install.sh": script,
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


@pytest.fixture
def fixture(tmp_path):
    images = {}

    def get(reference):
        if reference not in images:
            raise ImageNotFound(reference)
        return images[reference]

    def build(**kwargs):
        image = SimpleNamespace(id=f"sha256:{len(images)}", labels=kwargs["labels"])
        images[kwargs["tag"]] = image
        return image, []

    client = SimpleNamespace(
        images=SimpleNamespace(get=get, build=Mock(side_effect=build), remove=images.pop),
        containers=SimpleNamespace(list=lambda **kwargs: []),
    )
    cache = ImageCache(client, tmp_path / "images.json", 0, 0, 0, free_bytes=lambda: 100)
    runtime = DockerRuntime("test", client=client, image_cache=cache)
    return SimpleNamespace(
        images=images, client=client, cache=cache, runtime=runtime, path=tmp_path / "images.json"
    )


def describe_content_addressed_builds():
    def it_reuses_identical_inputs_across_request_ids(fixture):
        """Returns one image and refreshes its last use without calling Docker again."""
        tag = fixture.runtime.build_image("first", "Dockerfile", context())
        before = ImageInventory.model_validate_json(fixture.path.read_bytes()).images[0]
        assert fixture.runtime.build_image("second", "Dockerfile", context()) == tag
        after = ImageInventory.model_validate_json(fixture.path.read_bytes()).images[0]
        assert fixture.client.images.build.call_count == 1
        assert after.last_used_at >= before.last_used_at

    def it_invalidates_when_the_post_install_script_changes(fixture):
        """Includes the script bytes in the build identity."""
        first = fixture.runtime.build_image("a", "Dockerfile", context())
        second = fixture.runtime.build_image("b", "Dockerfile", context(b"echo changed"))
        assert first != second
        assert fixture.client.images.build.call_count == 2

    def it_distinguishes_selected_dockerfiles(fixture):
        """Includes the Dockerfile selector even when archive bytes are identical."""
        first = fixture.runtime.build_image("a", "Dockerfile", context())
        second = fixture.runtime.build_image("b", "Dockerfile.other", context())
        assert first != second

    def it_reuses_images_after_a_runtime_restart(fixture):
        """Recovers the image using Docker labels and persisted inventory."""
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        restarted = DockerRuntime("test", client=fixture.client, state_path=fixture.path)
        assert restarted.build_image("b", "Dockerfile", context()) == tag
        assert fixture.client.images.build.call_count == 1

    def it_recovers_owned_images_without_inventory(fixture, tmp_path):
        """Recognizes the broker and build labels when inventory is missing."""
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        restarted = DockerRuntime("test", client=fixture.client, state_path=tmp_path / "new.json")
        assert restarted.build_image("b", "Dockerfile", context()) == tag
        assert fixture.client.images.build.call_count == 1

    def it_rebuilds_a_collected_image(fixture):
        """Treats collection as a miss and rebuilds under the same content tag."""
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        assert fixture.cache.collect_if_needed(emergency=True) == [tag]
        assert fixture.runtime.build_image("b", "Dockerfile", context()) == tag
        assert fixture.client.images.build.call_count == 2

    @pytest.mark.parametrize("change", ["labels", "id"])
    def it_refuses_a_replaced_or_unowned_image(fixture, change):
        """Fails without building over an image whose identity no longer matches."""
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        setattr(fixture.images[tag], change, {} if change == "labels" else "sha256:foreign")
        with pytest.raises(SandboxOwnershipError):
            fixture.runtime.build_image("b", "Dockerfile", context())
        assert fixture.client.images.build.call_count == 1
        assert tag in fixture.images

    def it_releases_failed_builds_for_retry(fixture):
        """A failed build does not cache success or leave a locked gate."""
        build = fixture.client.images.build.side_effect
        fixture.client.images.build.side_effect = DockerException("build failed")
        with pytest.raises(RuntimeOperationError):
            fixture.runtime.build_image("a", "Dockerfile", context())
        fixture.client.images.build.side_effect = build
        fixture.runtime.build_image("b", "Dockerfile", context())
        assert fixture.client.images.build.call_count == 2

    def it_reports_inspection_errors_without_starting_a_build(fixture):
        """A Docker inspection failure is not mistaken for a cache miss."""
        fixture.client.images.get = Mock(side_effect=DockerException("daemon unavailable"))
        with pytest.raises(RuntimeOperationError, match="inspect build cache"):
            fixture.runtime.build_image("a", "Dockerfile", context())
        fixture.client.images.build.assert_not_called()

    def it_retries_a_build_after_disk_pressure(fixture):
        """Retries the build once after emergency GC without losing its cache identity."""
        build = fixture.client.images.build.side_effect
        calls = 0

        def fail_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DockerException("no space left on device")
            return build(**kwargs)

        fixture.client.images.build.side_effect = fail_once
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        assert fixture.runtime.build_image("b", "Dockerfile", context()) == tag
        assert fixture.client.images.build.call_count == 2

    def it_collapses_simultaneous_requests(fixture):
        """Sixteen simultaneous callers share exactly one Docker build."""
        start = Barrier(16)
        entered, release = Event(), Event()
        build = fixture.client.images.build.side_effect

        def slow_build(**kwargs):
            entered.set()
            assert release.wait(10)
            return build(**kwargs)

        def request(index):
            start.wait(timeout=10)
            return fixture.runtime.build_image(str(index), "Dockerfile", context())

        fixture.client.images.build.side_effect = slow_build
        with ThreadPoolExecutor(16) as pool:
            futures = [pool.submit(request, index) for index in range(16)]
            try:
                assert entered.wait(10)
            finally:
                release.set()
            tags = [future.result(timeout=10) for future in futures]
        assert len(set(tags)) == 1
        assert fixture.client.images.build.call_count == 1

    def it_builds_distinct_contexts_concurrently(fixture):
        """Different contexts reach Docker concurrently instead of sharing a global lock."""
        both_building = Barrier(2)
        build = fixture.client.images.build.side_effect

        def overlap(**kwargs):
            both_building.wait(timeout=10)
            return build(**kwargs)

        fixture.client.images.build.side_effect = overlap
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(
                    fixture.runtime.build_image, str(i), "Dockerfile", context(str(i).encode())
                )
                for i in range(2)
            ]
            assert len({future.result(timeout=10) for future in futures}) == 2

    def it_protects_build_handoffs_from_collection(fixture):
        """An in-flight build reference survives emergency collection until released."""
        tag = fixture.runtime.build_image("a", "Dockerfile", context())
        with fixture.cache.building(tag):
            assert fixture.cache.collect_if_needed(emergency=True) == []
        assert fixture.cache.collect_if_needed(emergency=True) == [tag]
