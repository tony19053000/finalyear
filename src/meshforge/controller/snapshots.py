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
through ``_on_task_done``, and losing a minute of history is not worth that. That
is why :meth:`flush_once` and :meth:`sweep_once` catch ``Exception``, not only
:class:`~meshforge.common.errors.MeshForgeError` - a malformed row raising a
``TypeError`` inside the driver would otherwise escape (NB-5).

**One poison row must not block the batch** (NB-6). The retry is a single
statement, so a row the database always refuses (e.g. its ``node`` row is gone)
used to fail every later flush until the pending cap evicted it - hours of lost
history. After two consecutive batch failures the flusher makes an *isolation
pass*: up to :data:`MAX_ISOLATED_ROWS` rows written one statement at a time. A
row is blamed only when the database is proved reachable **in that same pass**:
either another row got through, or - when nothing did, which is what a lone
poison row looks like - an explicit :meth:`~meshforge.controller.registry.Registry.ping`
answered. If neither holds, the database is unreachable and no row is blamed. A
blamed row is dropped (with a counter) the second time it fails that way.

Rows stay owned by ``_pending`` **across** the write and are cleared only after it
succeeds, so a cancellation mid-upsert re-writes the batch (the upsert is
idempotent) instead of losing it (NB-10).

On shutdown :meth:`final_flush` writes every bucket that has *ended* exactly once,
inside the runtime's shared drain deadline, without waiting for the late-sample
grace period (D26): it runs after the session manager shut down, so no sample can
still arrive for them. Open (not yet ended) buckets are deliberately *not*
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

__all__ = [
    "DEFAULT_MAX_PENDING_ROWS",
    "DROP_AFTER_ROW_FAILURES",
    "ISOLATE_AFTER_FAILURES",
    "MAX_ISOLATED_ROWS",
    "SnapshotFlusher",
]

log = get_logger(__name__)

DEFAULT_MAX_PENDING_ROWS: Final = 5_000
"""Rows kept for retry while the database is unreachable (~35 nodes for a day)."""

ISOLATE_AFTER_FAILURES: Final = 2
"""Consecutive batch failures before the flusher starts writing rows one by one."""

MAX_ISOLATED_ROWS: Final = 100
"""Rows probed individually per tick; bounds the cost when the database is down."""

DROP_AFTER_ROW_FAILURES: Final = 2
"""Individual failures (while *other* rows succeed) before a row is dropped."""


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
        self.dropped_poison = 0
        """Rows the database refused on their own twice while others went through."""
        self._consecutive_failures = 0
        self._isolating = False
        """A suspected poison row is pending: isolate on the next failure, at once."""
        self._row_failures: dict[tuple[str, dt.datetime], int] = {}

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

    async def flush_once(self, *, grace: bool = True) -> int:
        """Write every closed bucket (plus anything pending). Returns rows written.

        ``grace=False`` (shutdown only) also closes buckets still in their
        late-sample grace period (D26). Never raises except
        :class:`asyncio.CancelledError` (see the module docstring). The rows are
        held in ``_pending`` for the whole write and released only on success, so
        a cancelled upsert is retried, not lost.
        """
        rows = [
            *self._pending,
            *self._tracker.closed_rows(self._wall_clock(), grace=grace),
        ]
        if not rows:
            return 0
        self._remember(rows)  # NB-10: owned by _pending across the await
        rows = list(self._pending)
        try:
            written = await self._registry.upsert_resource_snapshots(rows)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            self._consecutive_failures += 1
            log.warning(
                "controller.snapshots.flush_failed",
                rows=len(rows),
                pending=len(self._pending),
                failures=self.failures,
                consecutive=self._consecutive_failures,
                error=str(exc),
                failure_type=(
                    exc.failure_type.value
                    if isinstance(exc, MeshForgeError)
                    else type(exc).__name__
                ),
                exc_info=not isinstance(exc, MeshForgeError),
            )
            if self._isolating or self._consecutive_failures >= ISOLATE_AFTER_FAILURES:
                await self._isolate(rows)
            return 0
        self._pending = []
        self._consecutive_failures = 0
        self._isolating = False
        for row in rows:
            self._row_failures.pop((row.node_id, row.bucket_start), None)
        self.written += written
        log.debug("controller.snapshots.flushed", rows=written, total=self.written)
        return written

    async def _isolate(self, rows: list[SnapshotRow]) -> None:
        """Write rows one by one to find the one(s) the batch chokes on (NB-6).

        Only called after repeated batch failures. A row is blamed only if this
        pass establishes that the database is reachable *now*: either some other
        row got through, or - when none did - :meth:`Registry.ping` answered. A
        pass in which nothing gets through and the ping fails is an outage: no
        row is blamed, no count moves, everything stays pending.
        """
        probed = rows[:MAX_ISOLATED_ROWS]
        ok: list[SnapshotRow] = []
        bad: list[SnapshotRow] = []
        for row in probed:
            try:
                await self._registry.upsert_resource_snapshots([row])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the point is to find *any* bad row
                bad.append(row)
            else:
                ok.append(row)
        if not ok and not await self._database_answers():
            # Nothing got through and the database does not answer at all: it is
            # unreachable, not poisoned. Blame no row, whatever earlier passes saw.
            log.debug(
                "controller.snapshots.isolation_all_failed",
                probed=len(probed),
                pending=len(self._pending),
            )
            return
        # Either another row landed, or the database answered a ``SELECT 1`` while
        # this one still failed: the row is the problem, even when it is the only
        # row left (a lone poison row must not block every flush forever - NB-6).
        self.written += len(ok)
        self._consecutive_failures = 0
        keep: list[SnapshotRow] = []
        for row in bad:
            key = (row.node_id, row.bucket_start)
            attempts = self._row_failures.get(key, 0) + 1
            if attempts >= DROP_AFTER_ROW_FAILURES:
                self._row_failures.pop(key, None)
                self.dropped_poison += 1
                log.error(
                    "controller.snapshots.row_dropped",
                    node_id=row.node_id,
                    bucket_start=row.bucket_start.isoformat(),
                    attempts=attempts,
                    dropped_poison=self.dropped_poison,
                )
            else:
                self._row_failures[key] = attempts
                keep.append(row)
        self._pending = keep + rows[MAX_ISOLATED_ROWS:]
        self._prune_row_failures()
        self._isolating = bool(keep)
        log.warning(
            "controller.snapshots.isolated",
            written=len(ok),
            failed=len(bad),
            dropped=len(bad) - len(keep),
            pending=len(self._pending),
        )

    async def sweep_once(self) -> int:
        """Delete snapshot rows older than ``retention_days``. Returns rows deleted."""
        cutoff = self._wall_clock() - dt.timedelta(days=self._settings.retention_days)
        try:
            deleted = await self._registry.delete_snapshots_before(cutoff)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            log.warning(
                "controller.snapshots.sweep_failed",
                error=str(exc),
                failure_type=(
                    exc.failure_type.value
                    if isinstance(exc, MeshForgeError)
                    else type(exc).__name__
                ),
                exc_info=not isinstance(exc, MeshForgeError),
            )
            return 0
        self.deleted += deleted
        return deleted

    async def final_flush(self) -> int:
        """Shutdown hook: write every ended bucket, once (no grace). Never raises."""
        with contextlib.suppress(asyncio.CancelledError):
            return await self.flush_once(grace=False)
        return 0

    async def _database_answers(self) -> bool:
        """Is the database reachable *right now*? One ``SELECT 1``, never raises.

        The isolation pass needs this only when no row got through, i.e. at most
        once per tick. Any failure - including a registry that cannot be asked -
        is reported as "not answering", so the fail-safe direction is always
        "blame nobody".
        """
        try:
            await self._registry.ping()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any answer but success is "down"
            log.debug("controller.snapshots.ping_failed", error=str(exc))
            return False
        return True

    def _prune_row_failures(self) -> None:
        """Forget failure counts for rows that are no longer pending.

        Without this a row that failed once individually and was then evicted by
        the pending cap (or written by a later batch) would leave an orphaned key
        behind forever. Pruning against ``_pending`` also bounds the dict by
        ``max_pending_rows``.
        """
        if not self._row_failures:
            return
        live = {(row.node_id, row.bucket_start) for row in self._pending}
        self._row_failures = {
            key: count for key, count in self._row_failures.items() if key in live
        }

    def _remember(self, rows: list[SnapshotRow]) -> None:
        """Keep ``rows`` for the next tick, newest kept when the cap is passed."""
        self._pending = rows
        overflow = len(self._pending) - self._max_pending
        if overflow > 0:
            del self._pending[:overflow]
            self.dropped_rows += overflow
            self._prune_row_failures()
            log.warning(
                "controller.snapshots.pending_overflow",
                dropped=overflow,
                dropped_total=self.dropped_rows,
                max_pending_rows=self._max_pending,
            )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
