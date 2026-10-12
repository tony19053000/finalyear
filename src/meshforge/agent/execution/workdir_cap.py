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
another process is filling. A failed free-space read is logged and skipped (an
``ENOSPC`` write still fails the attempt ``disk_full``, D116).

**Limits, said honestly:** both checks are a poll, so a writer can overshoot by
what it writes in one interval (bounded by the disk's speed). The interval
stretches to 5x the last walk's duration so a huge folder never turns the check
into a busy loop - which stretches the free-disk check with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.logging import get_logger

__all__ = ["UNMEASURABLE", "WorkdirCap", "disk_usage", "free_bytes", "stop_watch"]

log = get_logger(__name__)

UNMEASURABLE: Final = -1
""":func:`disk_usage` result for a folder that cannot be measured."""
_SLOWDOWN: Final = 5.0


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
    ) -> None:
        """Measure until the cap is passed (then call ``on_exceeded(written)``
        once and return), until the disk's free space is below the floor (then
        call ``on_disk_low(free)`` once and return) or until cancelled."""
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
        else None. A failed read is logged and counts as fine (ENOSPC stays
        typed ``disk_full``)."""
        if self.free_floor_bytes <= 0:
            return None
        try:
            self.free = await asyncio.to_thread(self.disk_free, self.root)
        except Exception:
            log.warning("agent.execution.free_disk_read_failed", exc_info=True)
            return None
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
