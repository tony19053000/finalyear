"""``split.progress`` events: when to read a split job's progress and publish
it (V0.10 G7, D270; the numbers themselves come from :mod:`.progress`).

One supervised task in the controller (:meth:`ProgressTracker.run`). It never
counts anything itself - it decides *when* to re-read the rows:

* **touches** - the dispatcher touches a job after handling one of its chunk
  messages (grant, result, renewal) and touches every job after a visibility
  timeout released leases; the committed job / attempt state changes (lane
  started, LOST, combine placed, job ended) touch their job too. A touch only
  marks the job dirty (synchronous, no I/O, safe from a commit hook);
* **resync** - every ``progress_resync_s`` the running split jobs are listed
  from the job table and marked dirty: a job nobody touched (e.g. right after a
  controller restart - the "rebuild": the first pass reads every running split
  job and publishes its state) is still read; an unchanged job publishes
  nothing (its :meth:`~.progress.SplitProgress.comparable` is compared).

**Rate bound.** A job is read at most ``progress_event_hz`` times a second;
touches in between coalesce into its next read, which always happens (the last
state is never lost). So a job publishes at most ``progress_event_hz`` events a
second, whatever its chunk rate - 10 000 tiny chunks a minute still mean at
most 2 events a second.

**Bounded memory.** The dirty set holds at most ``4 x progress_max_jobs`` ids
(more touches are dropped, counted and caught by the next resync); the last
published ``comparable()`` of at most ``progress_max_jobs`` jobs (LRU); a bounded
cache of which job ids are split jobs at all (Mode 1 jobs' state changes are
touches too - one batched lookup, then ignored).

A read that fails (database down) is logged and retried after a back-off; it
never stops the controller (supervised like the other loops).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy.exc import SQLAlchemyError

from meshforge.common.logging import get_logger
from meshforge.controller.settings import SplitSettings
from meshforge.controller.split.progress import SplitProgress

__all__ = ["ProgressSource", "ProgressStats", "ProgressTracker"]

log = get_logger(__name__)

MAX_READS_PER_STEP: Final = 64
"""Jobs read in one pass (the rest stay dirty for the next pass)."""
MAX_KIND_CACHE: Final = 10_000
"""Job ids remembered as split / not split."""
_MAX_BACKOFF_S: Final = 30.0
_LOOKUP_BATCH: Final = 500


class ProgressSource(Protocol):
    """Where the tracker reads (:class:`~.progress.ProgressReads` in the
    controller; a fake in unit tests)."""

    async def progress(
        self, job_id: str, *, with_map: bool = False
    ) -> SplitProgress | None: ...

    async def running_split_jobs(self, *, limit: int) -> Sequence[str]: ...

    async def split_job_ids(self, job_ids: Collection[str]) -> set[str]: ...


@dataclass(slots=True)
class ProgressStats:
    reads: int = 0
    published: int = 0
    unchanged: int = 0
    """Reads whose progress equalled the last published one (no event)."""
    touches: int = 0
    dropped_touches: int = 0
    errors: int = 0
    resyncs: int = 0


@dataclass(slots=True)
class _Seen:
    comparable: tuple[Any, ...]
    terminal: bool


class ProgressTracker:
    """See module docstring."""

    def __init__(
        self,
        source: ProgressSource,
        *,
        publish: Callable[[SplitProgress], None],
        settings: SplitSettings,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._source = source
        self._publish = publish
        self._interval = 1.0 / settings.progress_event_hz
        self._resync_s = settings.progress_resync_s
        self._max_jobs = settings.progress_max_jobs
        self._clock = clock or time.monotonic
        self._dirty: dict[str, None] = {}
        """Insertion-ordered set of job ids to read."""
        self._last_read: OrderedDict[str, float] = OrderedDict()
        """job -> monotonic time of its last read (the rate bound)."""
        self._seen: OrderedDict[str, _Seen] = OrderedDict()
        self._is_split: OrderedDict[str, bool] = OrderedDict()
        self._wake = asyncio.Event()
        self._next_resync = 0.0
        self._stopping = False
        self.stats = ProgressStats()

    # ------------------------------------------------------------ touches

    def touch(self, job_id: str | None) -> None:
        """Mark ``job_id`` (None = every job read before and not ended) for a
        read. Never blocks, never raises."""
        self.stats.touches += 1
        if job_id is None:
            for known, seen in self._seen.items():
                if not seen.terminal:
                    self._mark(known)
        else:
            if self._is_split.get(job_id) is False:
                return
            self._mark(job_id)
        self._wake.set()

    def _mark(self, job_id: str) -> None:
        if job_id in self._dirty:
            return
        if len(self._dirty) >= 4 * self._max_jobs:
            self.stats.dropped_touches += 1
            return
        self._dirty[job_id] = None

    def resync_now(self) -> None:
        """Run the resync on the next pass (tests; a restart does it anyway)."""
        self._next_resync = 0.0
        self._wake.set()

    # ------------------------------------------------------------ loop

    @property
    def stopping(self) -> bool:
        return self._stopping

    def request_stop(self) -> None:
        """Return from :meth:`run` after the current pass (never a read
        cancelled mid-query)."""
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        """Until :meth:`request_stop` (or cancelled): passes of :meth:`step`,
        sleeping until the next job is due, a touch arrives or the resync is
        due. A failing pass is logged and backed off - progress is advisory and
        never stops the controller."""
        failures = 0
        while not self._stopping:
            try:
                wait = await self.step()
                failures = 0
            except Exception as exc:  # logged, backed off, retried
                failures += 1
                self.stats.errors += 1
                wait = min(self._interval * 2**failures, _MAX_BACKOFF_S)
                fields = {"cause": type(exc).__name__, "retry_in_s": round(wait, 3)}
                if isinstance(exc, SQLAlchemyError | OSError):
                    log.warning("controller.split.progress_read_failed", **fields)
                else:
                    log.exception("controller.split.progress_crashed", **fields)
                self._wake.clear()
                with contextlib.suppress(TimeoutError):  # touches wait in the set
                    await asyncio.wait_for(self._stopped_wait(), timeout=wait)
                continue
            self._wake.clear()
            if self.stopping:  # asked while the pass ran
                return
            if self._dirty_due_now():
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(wait, 0.0))

    async def _stopped_wait(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()

    def _dirty_due_now(self) -> bool:
        now = self._clock()
        return any(self._due_at(j) <= now for j in self._dirty) or (
            now >= self._next_resync
        )

    def _due_at(self, job_id: str) -> float:
        last = self._last_read.get(job_id)
        return 0.0 if last is None else last + self._interval

    async def step(self) -> float:
        """One pass: resync if due, then read (and maybe publish) the dirty
        jobs that are due. Returns seconds until something is due."""
        now = self._clock()
        if now >= self._next_resync:
            await self._resync()
            self._next_resync = now + self._resync_s
        await self._learn_kinds()
        due = [j for j in self._dirty if self._due_at(j) <= now][:MAX_READS_PER_STEP]
        for job_id in due:
            self._dirty.pop(job_id, None)
            try:
                await self._read_one(job_id)
            except BaseException:
                self._mark(job_id)  # not lost: read again after the back-off
                raise
            finally:
                # measured from the END of the read (= its publish): the next
                # event of this job is at least one interval after this one
                self._last_read[job_id] = self._clock()
                self._last_read.move_to_end(job_id)
                while len(self._last_read) > self._max_jobs:
                    self._last_read.popitem(last=False)
        later = [self._due_at(j) for j in self._dirty]
        nxt = min([*later, self._next_resync])
        return max(0.0, nxt - self._clock())

    async def _resync(self) -> None:
        self.stats.resyncs += 1
        ids = await self._source.running_split_jobs(limit=self._max_jobs)
        for job_id in ids:
            self._remember_kind(job_id, True)
            self._mark(job_id)

    async def _learn_kinds(self) -> None:
        unknown = [j for j in self._dirty if j not in self._is_split]
        for start in range(0, len(unknown), _LOOKUP_BATCH):
            batch = unknown[start : start + _LOOKUP_BATCH]
            split = await self._source.split_job_ids(batch)
            for job_id in batch:
                is_split = job_id in split
                self._remember_kind(job_id, is_split)
                if not is_split:
                    self._dirty.pop(job_id, None)

    def _remember_kind(self, job_id: str, is_split: bool) -> None:
        self._is_split[job_id] = is_split
        self._is_split.move_to_end(job_id)
        while len(self._is_split) > MAX_KIND_CACHE:
            self._is_split.popitem(last=False)

    async def _read_one(self, job_id: str) -> None:
        progress = await self._source.progress(job_id)
        self.stats.reads += 1
        if progress is None:
            return
        comparable = progress.comparable()
        seen = self._seen.get(job_id)
        if seen is not None and seen.comparable == comparable:
            self.stats.unchanged += 1
            self._seen.move_to_end(job_id)
            return
        self._seen[job_id] = _Seen(comparable, progress.terminal)
        self._seen.move_to_end(job_id)
        while len(self._seen) > self._max_jobs:
            self._seen.popitem(last=False)
        self.stats.published += 1
        try:
            self._publish(progress)
        except Exception as exc:  # noqa: BLE001 - a sink must never stop the loop
            log.warning(
                "controller.split.progress_publish_failed",
                job_id=job_id,
                cause=type(exc).__name__,
            )
