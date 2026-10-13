"""The output-dir size cap, enforced while the job runs (V0.7 G5, 0.7.9, D143).

A job writes its outputs (and anything else) into its attempt folder - for
Docker the ``/work`` bind mount, a host folder no container size limit covers.
:class:`WorkdirCap` measures that folder every ``[agent.execution]
output_check_interval_s`` and calls back once it has grown more than
``max_written_bytes`` beyond what staging put there; the executor then stops the
run and fails the attempt ``output_error`` with reason ``output_limit``.

**Measuring** (:func:`disk_usage`): allocated blocks (``st_blocks * 512``; a
sparse file costs what it really occupies), each inode once (hard links are not
counted twice), symlinks never followed, every directory entry included. The
job runs as the agent's own uid (D63-2), so a folder it made unreadable
(``chmod 000``) is made readable again (``u+rx``) and measured; a folder that
still cannot be listed is **unmeasurable** and counts as over the cap (fail
closed - a job cannot hide its writes). Files that vanish mid-walk are skipped.

**Free disk** (V0.7 G5 fix r2): the same poll also reads the free space of the
folder's filesystem (``shutil.disk_usage``); below ``free_floor_bytes`` (the
agent's staging floor) it calls ``on_disk_low(free)`` once and returns - the
executor stops the run and fails it ``disk_full``. This covers what the folder
walk cannot see: bytes in a file the job deleted but keeps open, and a disk
another process is filling. A failed free-space read is logged and skipped, but
never silently for long (V0.7 G6 item 0, D161 N1): failed reads are counted and
a successful read resets the count; ``max_free_read_failures`` failures **in a
row** call ``on_disk_unknown(failures)`` once and return - the executor fails
the attempt ``disk_full`` with reason ``disk_unknown`` (fail closed: a job must
not keep writing to a disk nobody can measure). A read that **hangs** (a stale
network mount; V0.7 G6 fix round 1, D162 T-6) is a failed read too: each poll
waits at most ``free_read_timeout_s`` (``[agent.execution]
free_disk_read_timeout_s``) for it. A hung system call cannot be cancelled, so
the read runs on its own daemon thread (it never blocks the agent's shared
worker pool or its exit), a watcher never starts a second read while its first
is still stuck, and at most :data:`MAX_STUCK_READS` such threads exist in the
whole agent - past that a read fails at once instead of starting. An
``ENOSPC`` write still fails the attempt ``disk_full`` too (D116).

**Limits, said honestly:** both checks are a poll, so a writer can overshoot by
what it writes in one interval (bounded by the disk's speed). The interval
stretches to 5x the last walk's duration so a huge folder never turns the check
into a busy loop - which stretches the free-disk check with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.logging import get_logger

__all__ = [
    "MAX_STUCK_READS",
    "UNMEASURABLE",
    "WorkdirCap",
    "disk_usage",
    "free_bytes",
    "stop_watch",
]

log = get_logger(__name__)

UNMEASURABLE: Final = -1
""":func:`disk_usage` result for a folder that cannot be measured."""
_SLOWDOWN: Final = 5.0
MAX_STUCK_READS: Final = 4
"""Free-space reads that may be running (so: hung) at once in this process."""
_reads_lock: Final = threading.Lock()
_reads_running = 0


def _allocated(info: os.stat_result) -> int:
    blocks = getattr(info, "st_blocks", None)
    return int(blocks) * 512 if isinstance(blocks, int) else int(info.st_size)


def _list(path: str) -> list[os.DirEntry[str]] | None:
    """The entries of ``path``; after a ``PermissionError`` once more with
    ``u+rx`` restored (the job runs as us). None = cannot be listed."""
    for attempt in range(2):
        try:
            with os.scandir(path) as it:
                return list(it)
        except FileNotFoundError:
            return []
        except PermissionError:
            if attempt:
                return None
            try:
                mode = stat.S_IMODE(os.lstat(path).st_mode)
                os.chmod(path, mode | stat.S_IRUSR | stat.S_IXUSR)
            except OSError:
                return None
        except OSError:
            return None
    return None  # pragma: no cover - the loop always returns


def disk_usage(root: Path) -> int:
    """Bytes allocated under ``root`` (module docstring), or
    :data:`UNMEASURABLE`."""
    seen: set[tuple[int, int]] = set()
    total = 0
    stack = [str(root)]
    while stack:
        entries = _list(stack.pop())
        if entries is None:
            return UNMEASURABLE
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                return UNMEASURABLE
            key = (info.st_dev, info.st_ino)
            if info.st_nlink > 1 or stat.S_ISDIR(info.st_mode):
                if key in seen:
                    continue
                seen.add(key)
            total += _allocated(info)
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
    return total


def free_bytes(root: Path) -> int:
    """Free bytes on the filesystem holding ``root``."""
    return shutil.disk_usage(root).free


def _start_free_read(read: Callable[[Path], int], root: Path) -> asyncio.Future[int]:
    """Run ``read(root)`` on a daemon thread; the future resolves on this
    loop. Raises ``OSError(EBUSY)`` while :data:`MAX_STUCK_READS` reads are
    still running - the threads of hung reads are bounded, never piled up."""
    global _reads_running
    with _reads_lock:
        if _reads_running >= MAX_STUCK_READS:
            raise OSError(errno.EBUSY, "too many free-space reads are stuck")
        _reads_running += 1
    loop = asyncio.get_running_loop()
    future: asyncio.Future[int] = loop.create_future()
    # Nobody may be waiting any more (the attempt ended): never "unretrieved".
    future.add_done_callback(lambda done: done.cancelled() or done.exception())

    def deliver(value: int | None, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value if value is not None else 0)

    def work() -> None:
        global _reads_running
        value: int | None = None
        error: BaseException | None = None
        try:
            value = int(read(root))
        except BaseException as exc:  # noqa: BLE001 - delivered to the waiter
            error = exc
        finally:
            with _reads_lock:
                _reads_running -= 1
        with contextlib.suppress(RuntimeError):  # the loop is closed
            loop.call_soon_threadsafe(deliver, value, error)

    try:
        threading.Thread(target=work, name="mf-free-disk", daemon=True).start()
    except BaseException:
        with _reads_lock:
            _reads_running -= 1
        raise
    return future


@dataclass(slots=True)
class WorkdirCap:
    """Watch one attempt folder (module docstring). One owner task."""

    root: Path
    max_bytes: int
    interval_s: float
    clock: Callable[[], float] = time.monotonic
    measure: Callable[[Path], int] = disk_usage
    free_floor_bytes: int = 0
    """Free space the disk must keep while the job runs; 0 = not checked."""
    disk_free: Callable[[Path], int] = free_bytes
    max_free_read_failures: int = 5
    """Consecutive failed free-space reads that stop the job (``disk_unknown``)."""
    free_read_failures: int = 0
    """Failed free-space reads in a row (reset by a successful read)."""
    free_read_timeout_s: float = 0.5
    """Longest one poll waits for the free-space read; a read still running
    then is a failed read (D162 T-6). The agent passes ``[agent.execution]
    free_disk_read_timeout_s``."""
    _free_read: asyncio.Future[int] | None = None
    """The read in progress - kept across polls while it hangs."""
    baseline: int | None = None
    written: int = 0
    """Bytes added since the baseline at the last measurement
    (:data:`UNMEASURABLE` once a measurement failed)."""
    free: int | None = None
    """Free bytes on the folder's disk at the last check (None = not read)."""

    async def run(
        self,
        on_exceeded: Callable[[int], None],
        on_disk_low: Callable[[int], None] | None = None,
        on_disk_unknown: Callable[[int], None] | None = None,
    ) -> None:
        """Measure until the cap is passed (then call ``on_exceeded(written)``
        once and return), until the disk's free space is below the floor (then
        call ``on_disk_low(free)`` once and return), until the free space could
        not be read ``max_free_read_failures`` times in a row (then call
        ``on_disk_unknown(failures)`` once and return) or until cancelled."""
        if self.baseline is None:
            started = self.clock()
            base = await self._measure()
            self.baseline = max(0, base)
            took = self.clock() - started
        else:
            took = 0.0
        while True:
            await asyncio.sleep(max(self.interval_s, _SLOWDOWN * took))
            low = await self._disk_low() if on_disk_low is not None else None
            if on_disk_low is not None and low is not None:
                on_disk_low(low)
                return
            if on_disk_unknown is not None and self.free_read_failures >= max(
                1, self.max_free_read_failures
            ):
                on_disk_unknown(self.free_read_failures)
                return
            started = self.clock()
            used = await self._measure()
            took = self.clock() - started
            if used == UNMEASURABLE:
                self.written = UNMEASURABLE
                on_exceeded(UNMEASURABLE)
                return
            self.written = max(0, used - self.baseline)
            if self.written > self.max_bytes:
                on_exceeded(self.written)
                return

    async def measure_once(self) -> int:
        """One measurement (the baseline, before the job starts)."""
        return await self._measure()

    async def _disk_low(self) -> int | None:
        """The free bytes when the folder's disk has less than the floor free,
        else None. A failed read is logged and counted in
        :attr:`free_read_failures` (the caller acts on a streak); a successful
        read resets the count."""
        if self.free_floor_bytes <= 0:
            return None
        try:
            if self._free_read is None:
                self._free_read = _start_free_read(self.disk_free, self.root)
            # shield: a timeout must not cancel the future the stuck thread
            # still resolves - the next poll waits on the same read.
            self.free = await asyncio.wait_for(
                asyncio.shield(self._free_read), self.free_read_timeout_s
            )
        except TimeoutError:
            self.free_read_failures += 1
            log.warning(
                "agent.execution.free_disk_read_failed",
                failures_in_a_row=self.free_read_failures,
                limit=self.max_free_read_failures,
                cause="timeout",
                timeout_s=self.free_read_timeout_s,
            )
            return None
        except Exception:
            self._free_read = None
            self.free_read_failures += 1
            log.warning(
                "agent.execution.free_disk_read_failed",
                failures_in_a_row=self.free_read_failures,
                limit=self.max_free_read_failures,
                exc_info=True,
            )
            return None
        self._free_read = None
        self.free_read_failures = 0
        return self.free if self.free < self.free_floor_bytes else None

    async def _measure(self) -> int:
        """One measurement off the event loop; an unexpected failure is
        logged and counts as unmeasurable (fail closed, never a silent stop)."""
        try:
            return await asyncio.to_thread(self.measure, self.root)
        except Exception:
            log.exception("agent.execution.workdir_measure_failed")
            return UNMEASURABLE


async def stop_watch(task: asyncio.Task[None] | None) -> None:
    """Cancel and await a watch task (never raises)."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
