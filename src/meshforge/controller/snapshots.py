"""The snapshot flusher task (roadmap 0.3.11): closed buckets -> Postgres, plus retention.

One task owned by :class:`~meshforge.controller.server.ControllerRuntime`, spawned
like the lease timer. Every ``flush_interval_s`` it drains the downsampler's
*closed* buckets and upserts them; every ``retention_sweep_s`` it deletes rows older
than ``retention_days``. Both intervals are measured on the event loop's **monotonic**
clock; only the bucket boundaries and the retention cutoff use the wall clock,
because those are the values stored in the table.

Failure policy: a database error is logged and the rows stay in a bounded pending
list to be retried on the next tick. The list has a hard cap - a controller whose
database is gone for an hour must not grow without limit - and the oldest rows are
dropped with a counter when it overflows. **The flusher never raises out of**
:meth:`SnapshotFlusher.run`: a crashed flusher would take the whole controller down
through ``_on_task_done``, and losing a minute of history is not worth that.

On shutdown :meth:`final_flush` writes whatever is already closed exactly once,
inside the runtime's shared drain deadline. Open buckets are deliberately *not*
written: a partial bucket would look like a complete one.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Callable
from typing import Final

from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.registry import Registry
from meshforge.controller.resources import ResourceTracker, SnapshotRow
from meshforge.controller.settings import ResourceHistorySettings

__all__ = ["DEFAULT_MAX_PENDING_ROWS", "SnapshotFlusher"]

log = get_logger(__name__)

DEFAULT_MAX_PENDING_ROWS: Final = 5_000
"""Rows kept for retry while the database is unreachable (~35 nodes for a day)."""


class SnapshotFlusher:
    """See the module docstring. Construct, then ``await run()`` in one task."""

    def __init__(
        self,
        tracker: ResourceTracker,
        registry: Registry,
        settings: ResourceHistorySettings,
        *,
        clock: Callable[[], float],
        wall_clock: Callable[[], dt.datetime] | None = None,
        max_pending_rows: int = DEFAULT_MAX_PENDING_ROWS,
    ) -> None:
        self._tracker = tracker
        self._registry = registry
        self._settings = settings
        self._clock = clock
        self._wall_clock = wall_clock or _utcnow
        self._max_pending = max(1, max_pending_rows)
        self._pending: list[SnapshotRow] = []
        self._next_sweep_at = clock() + settings.retention_sweep_s
        self.written = 0
        self.deleted = 0
        self.failures = 0
        self.dropped_rows = 0

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def run(self) -> None:
        """Flush and sweep forever; cancelled by the runtime on shutdown."""
        interval = self._settings.flush_interval_s
        log.info(
            "controller.snapshots.started",
            flush_interval_s=interval,
            bucket_s=self._settings.bucket_s,
            retention_days=self._settings.retention_days,
        )
        try:
            while True:
                await asyncio.sleep(interval)
                await self.flush_once()
                if self._clock() >= self._next_sweep_at:
                    self._next_sweep_at = self._clock() + (
                        self._settings.retention_sweep_s
                    )
                    await self.sweep_once()
        finally:
            log.info(
                "controller.snapshots.stopped",
                written=self.written,
                deleted=self.deleted,
                failures=self.failures,
                pending=len(self._pending),
            )

    async def flush_once(self) -> int:
        """Write every closed bucket (plus anything pending). Returns rows written."""
        rows = [*self._pending, *self._tracker.closed_rows(self._wall_clock())]
        self._pending = []
        if not rows:
            return 0
        try:
            written = await self._registry.upsert_resource_snapshots(rows)
        except MeshForgeError as exc:
            self.failures += 1
            self._remember(rows)
            log.warning(
                "controller.snapshots.flush_failed",
                rows=len(rows),
                pending=len(self._pending),
                failures=self.failures,
                error=str(exc),
                failure_type=exc.failure_type.value,
            )
            return 0
        self.written += written
        log.debug("controller.snapshots.flushed", rows=written, total=self.written)
        return written

    async def sweep_once(self) -> int:
        """Delete snapshot rows older than ``retention_days``. Returns rows deleted."""
        cutoff = self._wall_clock() - dt.timedelta(days=self._settings.retention_days)
        try:
            deleted = await self._registry.delete_snapshots_before(cutoff)
        except MeshForgeError as exc:
            self.failures += 1
            log.warning(
                "controller.snapshots.sweep_failed",
                error=str(exc),
                failure_type=exc.failure_type.value,
            )
            return 0
        self.deleted += deleted
        return deleted

    async def final_flush(self) -> int:
        """Shutdown hook: write what is already closed, once. Never raises."""
        with contextlib.suppress(asyncio.CancelledError):
            return await self.flush_once()
        return 0

    def _remember(self, rows: list[SnapshotRow]) -> None:
        """Keep ``rows`` for the next tick, newest kept when the cap is passed."""
        self._pending = rows
        overflow = len(self._pending) - self._max_pending
        if overflow > 0:
            del self._pending[:overflow]
            self.dropped_rows += overflow
            log.warning(
                "controller.snapshots.pending_overflow",
                dropped=overflow,
                dropped_total=self.dropped_rows,
                max_pending_rows=self._max_pending,
            )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
