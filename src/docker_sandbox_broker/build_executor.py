"""Bounded build admission independent of Starlette's request-worker budget."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from time import monotonic

from docker_sandbox_broker.errors import BuildCapacityError, PayloadTooLargeError
from docker_sandbox_broker.logging import get_logger
from docker_sandbox_broker.models import BuildStatus


class BuildExecutor:
    """Keep running builds accounted for even after their HTTP waiter is cancelled.

    Admission and completion run on the application event loop. Only the build
    callable runs in this executor; queued requests do not consume threads.
    """

    def __init__(self, workers: int, queue_size: int, max_bytes: int):
        self._workers = workers
        self._queued_at: dict[object, float] = {}
        self._running_at: dict[object, float] = {}
        self._succeeded = self._failed = self._rejected = self._cancelled_queued = 0
        self._last_finished: float | None = None
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="broker-build")
        self._slots = asyncio.Semaphore(workers)
        self._capacity = workers + queue_size
        self._max_bytes = max_bytes
        self._pending = 0
        self._bytes = 0
        self._active = 0
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._log = get_logger().bind(component="build_executor")

    async def run(self, build, dockerfile: str, context: bytes) -> str:
        size = len(context)
        if size > self._max_bytes:
            raise PayloadTooLargeError("build context exceeds build memory budget")
        if self._closed or self._pending >= self._capacity or self._bytes + size > self._max_bytes:
            self._rejected += 1
            self._log.info(
                "build_rejected", active=self._active, queued=self._pending - self._active
            )
            raise BuildCapacityError("build capacity is full; retry later")
        self._pending += 1
        self._bytes += size
        self._idle.clear()
        queued_at = monotonic()
        ticket = object()
        self._queued_at[ticket] = queued_at
        self._log.info("build_admitted", active=self._active, queued=self._pending - self._active)
        try:
            await self._slots.acquire()
        except BaseException:
            self._cancelled_queued += 1
            self._unregister(size, ticket)
            self._log.info("build_queue_cancelled", queue_seconds=monotonic() - queued_at)
            raise
        started = monotonic()
        self._queued_at.pop(ticket)
        self._running_at[ticket] = started
        self._active += 1
        self._log.info(
            "build_started",
            queue_seconds=started - queued_at,
            active=self._active,
            queued=self._pending - self._active,
        )
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self._pool, copy_context().run, build, dockerfile, context
            )
        except BaseException:
            self._failed += 1
            self._last_finished = monotonic()
            self._release(size, ticket)
            raise
        future.add_done_callback(lambda result: self._finished(result, size, started, ticket))
        # Cancellation affects the waiter, never the running build or its capacity.
        return await asyncio.shield(future)

    def _release(self, size: int, ticket: object) -> None:
        self._active -= 1
        self._unregister(size, ticket)
        self._slots.release()

    def _unregister(self, size: int, ticket: object) -> None:
        self._queued_at.pop(ticket, None)
        self._running_at.pop(ticket, None)
        self._pending -= 1
        self._bytes -= size
        if self._pending == 0:
            self._idle.set()

    def _finished(self, result: asyncio.Future, size: int, started: float, ticket: object) -> None:
        self._release(size, ticket)
        error = result.exception()
        self._last_finished = monotonic()
        self._failed += int(error is not None)
        self._succeeded += int(error is None)
        self._log.info(
            "build_finished",
            execution_seconds=monotonic() - started,
            outcome="error" if error else "ok",
            error_type=type(error).__name__ if error else None,
            active=self._active,
            queued=self._pending - self._active,
        )

    def snapshot(self) -> BuildStatus:
        """Read on the application loop; no awaits, locks, payloads or Docker calls."""
        now = monotonic()
        return BuildStatus(
            accepting=not self._closed,
            workers=self._workers,
            queue_limit=self._capacity - self._workers,
            max_context_bytes=self._max_bytes,
            active=self._active,
            queued=self._pending - self._active,
            context_bytes=self._bytes,
            oldest_running_seconds=_oldest_age(self._running_at, now),
            oldest_queued_seconds=_oldest_age(self._queued_at, now),
            last_finished_seconds_ago=(
                None if self._last_finished is None else now - self._last_finished
            ),
            succeeded_total=self._succeeded,
            failed_total=self._failed,
            rejected_total=self._rejected,
            cancelled_queued_total=self._cancelled_queued,
        )

    async def close(self) -> None:
        self._closed = True
        await self._idle.wait()
        await asyncio.to_thread(self._pool.shutdown, wait=True)


def _oldest_age(timestamps: dict[object, float], now: float) -> float | None:
    return now - min(timestamps.values()) if timestamps else None
