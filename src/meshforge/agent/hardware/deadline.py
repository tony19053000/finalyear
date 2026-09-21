"""Running a blocking probe with a deadline the caller can actually rely on (D10).

Most of this package is guarded by :class:`~meshforge.agent.hardware.probe.Budget`,
which works because the probe *returns* to check it: ``run_command`` really does kill
its subprocess. NVML is different. ``nvmlInit`` and the per-device queries are plain
synchronous FFI calls - no subprocess, no cancellation point - so a driver that wedges
instead of erroring blocks its thread for as long as it likes. Checking the budget
*before* each probe cannot bound that, and ``asyncio.to_thread`` does not either:

* ``asyncio.wait_for(asyncio.to_thread(...))`` returns control to the loop, but the
  worker thread keeps running. It belongs to the loop's default
  :class:`~concurrent.futures.ThreadPoolExecutor`, whose threads are **joined** by
  ``loop.shutdown_default_executor()`` and again by ``concurrent.futures``' atexit
  hook - so one wedged NVML call would hang process exit.

:func:`DeadlineThread.call` therefore runs the callable on its **own daemon thread**.
On expiry the caller gets :class:`BlockingCallTimeoutError` and the thread is *abandoned*:
never joined, never awaited, and daemon so neither ``asyncio.run`` nor interpreter
shutdown waits for it. (A wedged NVML call sits inside a ``ctypes`` call with the GIL
released, so the abandoned thread costs a thread and its stack, nothing else.)

**One slot.** A :class:`DeadlineThread` runs one call at a time: while an abandoned
call is still running, the next :meth:`call` fails fast with
:class:`BlockingCallStalledError` instead of starting a second thread. A permanently
wedged driver therefore leaks exactly one thread, not one per sample.

Not a general-purpose primitive: use it only for blocking *probes* whose result may
be dropped. Nothing here cancels the work, so the callable must have no side effect
that matters once its deadline has passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Final

from meshforge.common.logging import get_logger

__all__ = [
    "BlockingCallError",
    "BlockingCallStalledError",
    "BlockingCallTimeoutError",
    "DeadlineThread",
]

log = get_logger(__name__)

_MIN_TIMEOUT_S: Final = 0.01


class BlockingCallError(Exception):
    """Base of the two typed reasons a guarded blocking call produced no value."""


class BlockingCallTimeoutError(BlockingCallError):
    """The call did not finish within its deadline; its thread was abandoned."""


class BlockingCallStalledError(BlockingCallError):
    """A previously abandoned call still occupies the slot, so nothing was started."""


class DeadlineThread:
    """A one-slot runner for blocking calls, each bounded by its own deadline."""

    __slots__ = ("_busy", "_name", "abandoned", "calls", "stalled_calls", "timeouts")

    def __init__(self, name: str) -> None:
        self._name = name
        self._busy = threading.Event()
        """Set while a call is on the thread; cleared by that thread when it ends."""
        self.calls = 0
        self.timeouts = 0
        self.stalled_calls = 0
        self.abandoned = 0
        """Threads left running past their deadline (at most one is alive)."""

    @property
    def name(self) -> str:
        return self._name

    @property
    def stalled(self) -> bool:
        """True while an abandoned call is still running (see the module docstring)."""
        return self._busy.is_set()

    async def call[T](self, fn: Callable[[], T], *, timeout_s: float) -> T:
        """Run ``fn()`` on a daemon thread and await it for at most ``timeout_s``.

        Raises :class:`BlockingCallTimeoutError` on expiry, :class:`BlockingCallStalledError`
        when the previous call is still wedged, and anything ``fn`` raises.
        """
        if self._busy.is_set():
            self.stalled_calls += 1
            raise BlockingCallStalledError(
                f"{self._name}: a previous call is still running"
            )
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        box: list[tuple[bool, object]] = []
        self._busy.set()

        def runner() -> None:
            try:
                box.append((True, fn()))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the loop
                box.append((False, exc))
            finally:
                self._busy.clear()
                # The loop may already be closed if the agent exited while this
                # call was abandoned; nobody is waiting in that case.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(done.set)

        thread = threading.Thread(
            target=runner, name=f"mf-probe:{self._name}", daemon=True
        )
        self.calls += 1
        thread.start()
        try:
            await asyncio.wait_for(done.wait(), max(timeout_s, _MIN_TIMEOUT_S))
        except TimeoutError:
            self.timeouts += 1
            self.abandoned += 1
            log.warning(
                "agent.hardware.blocking_call_timeout",
                call=self._name,
                timeout_s=round(timeout_s, 3),
                abandoned_threads=self.abandoned,
            )
            raise BlockingCallTimeoutError(
                f"{self._name}: still running after {timeout_s:.3g} s"
            ) from None
        ok, value = box[-1]
        if not ok:
            raise value  # type: ignore[misc]  # BaseException from the worker
        return value  # type: ignore[return-value]  # T by construction
