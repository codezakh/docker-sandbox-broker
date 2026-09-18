"""Bounded build admission independent of Starlette's request-worker budget."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from time import monotonic

from docker_sandbox_broker.errors import BuildCapacityError, PayloadTooLargeError
from docker_sandbox_broker.logging import get_logger


class BuildExecutor:
    """Keep running builds accounted for even after their HTTP waiter is cancelled.

    Admission and completion run on the application event loop. Only the build
    callable runs in this executor; queued requests do not consume threads.
    """

    def __init__(self, workers: int, queue_size: int, max_bytes: int):
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
            self._log.info(
                "build_rejected", active=self._active, queued=self._pending - self._active
            )
            raise BuildCapacityError("build capacity is full; retry later")
        self._pending += 1
        self._bytes += size
        self._idle.clear()
        queued_at = monotonic()
        self._log.info("build_admitted", active=self._active, queued=self._pending - self._active)
        try:
            await self._slots.acquire()
        except BaseException:
            self._unregister(size)
            self._log.info("build_queue_cancelled", queue_seconds=monotonic() - queued_at)
            raise
        started = monotonic()
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
            self._release(size)
            raise
        future.add_done_callback(lambda result: self._finished(result, size, started))
        # Cancellation affects the waiter, never the running build or its capacity.
        return await asyncio.shield(future)

    def _release(self, size: int) -> None:
        self._active -= 1
        self._unregister(size)
        self._slots.release()

    def _unregister(self, size: int) -> None:
        self._pending -= 1
        self._bytes -= size
        if self._pending == 0:
            self._idle.set()

    def _finished(self, result: asyncio.Future, size: int, started: float) -> None:
        self._release(size)
        error = result.exception()
        self._log.info(
            "build_finished",
            execution_seconds=monotonic() - started,
            outcome="error" if error else "ok",
            error_type=type(error).__name__ if error else None,
            active=self._active,
            queued=self._pending - self._active,
        )

    async def close(self) -> None:
        self._closed = True
        await self._idle.wait()
        await asyncio.to_thread(self._pool.shutdown, wait=True)
