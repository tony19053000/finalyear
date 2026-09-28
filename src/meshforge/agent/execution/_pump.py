"""One task delivers a run's output to ``on_output``, in arrival order, with back-pressure.

Both runtimes have several producers (two pipe readers; a Docker attach thread) and
the contract promises the consumer is called from **one** task, never concurrently
(brief D51 / ``runtime.py``). Producers put ``(stream, bytes)`` into a bounded queue;
when ``on_output`` is slow the queue fills and the producers wait, which in turn
stops draining the workload's pipes - the workload's own writes block. Nothing is
buffered without bound.

If ``on_output`` raises, :attr:`OutputPump.failed` is set, the event fires (the
runtime stops the workload and reports ``internal_error``) and everything after is
discarded so producers never block on a dead consumer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Final

from meshforge.agent.execution.runtime import OutputCallback, Stream

__all__ = ["OutputPump"]

_QUEUE_CHUNKS: Final = 64
"""At most 64 chunks (<= 64 KiB each) in flight: ~4 MiB of back-pressure buffer."""
_THREAD_PUT_POLL_S: Final = 0.2


class OutputPump:
    """Owns the single consumer task; producers call :meth:`put` / :meth:`put_from_thread`."""

    def __init__(self, on_output: OutputCallback, *, name: str) -> None:
        self._on_output = on_output
        self._queue: asyncio.Queue[tuple[Stream, bytes] | None] = asyncio.Queue(
            _QUEUE_CHUNKS
        )
        self._name = name
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.failed: BaseException | None = None
        self.failed_event = asyncio.Event()
        self.delivered_bytes = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._consume(), name=f"output:{self._name}")

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if self.failed is not None:
                continue  # drain and discard: producers must never block on us
            try:
                await self._on_output(*item)
                self.delivered_bytes += len(item[1])
            except Exception as exc:  # noqa: BLE001 - reported as internal_error
                self.failed = exc
                self.failed_event.set()

    async def put(self, stream: Stream, data: bytes) -> None:
        if data:
            await self._queue.put((stream, data))

    def put_from_thread(
        self, stream: Stream, data: bytes, stop: threading.Event
    ) -> bool:
        """Blocking put from a worker thread; False when ``stop`` is set or the loop
        is gone (the caller then stops reading)."""
        loop = self._loop
        if loop is None or not data:
            return loop is not None
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._queue.put((stream, data)), loop
            )
        except RuntimeError:  # loop closed
            return False
        while True:
            try:
                fut.result(timeout=_THREAD_PUT_POLL_S)
            except concurrent.futures.TimeoutError:
                if stop.is_set():
                    fut.cancel()
                    return False
                continue
            except (concurrent.futures.CancelledError, RuntimeError):
                return False
            return True

    async def close(self, timeout_s: float) -> bool:
        """Deliver what is queued (bounded by ``timeout_s``), then stop the consumer.

        Returns False when the consumer did not finish in time (it is cancelled -
        a hung ``on_output`` must not hold the run open)."""
        task = self._task
        if task is None:
            return True
        try:
            async with asyncio.timeout(max(0.0, timeout_s)):
                await self._queue.put(None)
                await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            # gather: swallows the consumer's CancelledError, not our own
            await asyncio.gather(task, return_exceptions=True)
            return False
        return True

    async def abort(self) -> None:
        """Stop the consumer now (cancellation path); queued output is dropped."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
