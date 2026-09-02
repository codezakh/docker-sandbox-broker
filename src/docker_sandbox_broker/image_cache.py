"""Broker-owned Docker image inventory and pressure-triggered collection."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Literal

from docker.errors import DockerException, ImageNotFound
from pydantic import BaseModel, Field

from docker_sandbox_broker.logging import get_logger


class CachedImage(BaseModel):
    reference: str
    image_id: str
    origin: Literal["built", "pulled"]
    last_used_at: datetime


class ImageInventory(BaseModel):
    images: list[CachedImage] = Field(default_factory=list)


class ImageCache:
    """Track only images acquired by the broker and collect unused entries."""

    def __init__(
        self,
        client,
        state_path: Path,
        min_free_bytes: int,
        target_free_bytes: int,
        min_age_seconds: int = 300,
        free_bytes: Callable[[], int] | None = None,
    ):
        self._client = client
        self._state_path = state_path
        self._min_free_bytes = min_free_bytes
        self._target_free_bytes = target_free_bytes
        self._min_age = timedelta(seconds=min_age_seconds)
        self._free_bytes = free_bytes or self._docker_free_bytes
        self._lock = RLock()
        self._reservations: dict[str, int] = {}
        self._log = get_logger().bind(component="image_cache")
        self._records = self._load()

    def record(self, reference: str, image_id: str, origin: Literal["built", "pulled"]) -> None:
        with self._lock:
            self._records[reference] = CachedImage(
                reference=reference,
                image_id=image_id,
                origin=origin,
                last_used_at=datetime.now(UTC),
            )
            self._save()

    @contextmanager
    def reserve(self, image_id: str):
        """Protect an image while a container is being created from it."""
        with self._lock:
            self._reservations[image_id] = self._reservations.get(image_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._reservations[image_id] - 1
                if remaining:
                    self._reservations[image_id] = remaining
                else:
                    self._reservations.pop(image_id)

    def touch(self, reference: str, image_id: str) -> None:
        with self._lock:
            record = self._records.get(reference)
            if record is None:
                return
            if record.image_id != image_id:
                self._records.pop(reference)
            else:
                record.last_used_at = datetime.now(UTC)
            self._save()

    def collect_if_needed(self, *, emergency: bool = False) -> list[str]:
        with self._lock:
            collection = self._prepare_collection(emergency)
            if collection is None:
                return []
            free_bytes, active_images = collection
            removed, free_bytes = self._collect_records(free_bytes, active_images, emergency)
            if removed:
                self._log.info("images_collected", images=removed, free_bytes=free_bytes)
            return removed

    def _prepare_collection(self, emergency: bool) -> tuple[int, set[str]] | None:
        if not self._records:
            return None
        free_bytes = self._available_bytes()
        if free_bytes is None or (not emergency and free_bytes >= self._min_free_bytes):
            return None
        active_images = self._active_image_ids()
        if active_images is None:
            return None
        return free_bytes, active_images

    def _collect_records(
        self,
        free_bytes: int,
        active_images: set[str],
        emergency: bool,
    ) -> tuple[list[str], int]:
        removed: list[str] = []
        must_remove_one = emergency
        for record in sorted(self._records.values(), key=lambda item: item.last_used_at):
            if free_bytes >= self._target_free_bytes and not must_remove_one:
                break
            if not self._remove_if_eligible(record, active_images):
                continue
            removed.append(record.reference)
            must_remove_one = False
            updated_free_bytes = self._available_bytes()
            if updated_free_bytes is not None:
                free_bytes = updated_free_bytes
        return removed, free_bytes

    def _remove_if_eligible(self, record: CachedImage, active_images: set[str]) -> bool:
        if datetime.now(UTC) - record.last_used_at < self._min_age:
            return False
        try:
            image = self._client.images.get(record.reference)
        except ImageNotFound:
            self._forget(record.reference)
            return False
        except DockerException as error:
            self._log.warning("image_inspect_failed", image=record.reference, error=str(error))
            return False
        if image.id != record.image_id:
            self._forget(record.reference)
            return False
        if image.id in active_images:
            return False
        try:
            self._client.images.remove(record.reference)
        except ImageNotFound:
            pass
        except DockerException as error:
            self._log.warning("image_delete_failed", image=record.reference, error=str(error))
            return False
        self._forget(record.reference)
        return True

    def _active_image_ids(self) -> set[str] | None:
        try:
            container_images = {
                image_id
                for container in self._client.containers.list(all=True)
                if (image_id := container.attrs.get("Image"))
            }
            return container_images | set(self._reservations)
        except DockerException as error:
            self._log.warning("container_inventory_failed", error=str(error))
            return None

    def _forget(self, reference: str) -> None:
        self._records.pop(reference, None)
        self._save()

    def _available_bytes(self) -> int | None:
        try:
            return self._free_bytes()
        except OSError as error:
            self._log.warning("disk_usage_failed", error=str(error))
            return None

    def _docker_free_bytes(self) -> int:
        docker_root = self._client.info()["DockerRootDir"]
        return shutil.disk_usage(docker_root).free

    def _load(self) -> dict[str, CachedImage]:
        try:
            inventory = ImageInventory.model_validate_json(self._state_path.read_bytes())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self._log.warning("image_inventory_load_failed", error=str(error))
            return {}
        return {record.reference: record for record in inventory.images}

    def _save(self) -> None:
        self._state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = ImageInventory(images=list(self._records.values())).model_dump_json(indent=2)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._state_path.name}.",
            dir=self._state_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write(payload)
            temporary.replace(self._state_path)
        finally:
            temporary.unlink(missing_ok=True)
