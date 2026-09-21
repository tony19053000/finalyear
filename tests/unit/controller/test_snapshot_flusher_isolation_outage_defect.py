"""V0.3 G4 cleanup round tester: a confirmed defect in the poison-row isolation.

``SnapshotFlusher._isolate`` (snapshots.py) is meant to hold to one invariant: a row
is only ever blamed (counted toward ``DROP_AFTER_ROW_FAILURES``) when an isolation
pass proves *other* rows can get through while this one cannot - i.e. the database
is demonstrably up. When **no** row gets through, the module docstring and NB-6 say
plainly: "the database is simply unreachable and nothing is held against the rows".

That guarantee only holds on an isolation pass that starts with ``_isolating`` still
``False``. Once a *different*, real poison-row episode has set the ``_isolating``
latch (a probe got through while another row failed), the early-return guard
``if not ok and not self._isolating: return`` no longer protects a **later**,
independent full outage: with the latch already ``True``, a pass where *every* row
fails (because the database is down again, not because any row is poison) falls
through to the "blame" branch and can drop a row on a failure that was never
individually confirmed as the row's own fault.

This lets a genuinely fine row - or a row that failed only once for real - be
dropped (history lost forever) purely because the database happened to go down a
second time while the flusher was mid-isolation from an earlier, unrelated poison
row. Reproduction below is a direct, minimal sequence against the real
``SnapshotFlusher``/``StubRegistry`` pair already used by this test module.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import pytest

from meshforge.controller.registry import Registry, RegistryError
from meshforge.controller.resources import ResourceTracker, SnapshotRow
from meshforge.controller.settings import ResourceHistorySettings
from meshforge.controller.snapshots import DROP_AFTER_ROW_FAILURES, SnapshotFlusher

pytestmark = pytest.mark.unit

# Mirrors tests/unit/controller/test_snapshot_flusher.py's fixtures exactly (no
# shared __init__.py in this test package, so relative import is not available).

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"


class StubRegistry:
    """Records what the flusher asked for; can fail on demand."""

    def __init__(self) -> None:
        self.batches: list[list[SnapshotRow]] = []
        self.fail_writes = False
        self.poison: tuple[str, dt.datetime] | None = None

    async def upsert_resource_snapshots(self, rows: Any) -> int:
        batch = list(rows)
        if self.fail_writes:
            raise RegistryError(
                "connection refused", details={"operation": "upsert_resource_snapshots"}
            )
        if self.poison is not None and any(
            (row.node_id, row.bucket_start) == self.poison for row in batch
        ):
            raise RegistryError(
                "insert or update violates foreign key constraint",
                details={"operation": "upsert_resource_snapshots"},
            )
        self.batches.append(batch)
        return len(batch)


def row_at(node_id: str, minute: int) -> SnapshotRow:
    return SnapshotRow(
        node_id=node_id,
        bucket_start=T0 + dt.timedelta(minutes=minute),
        samples=2,
        cpu_percent_mean=1.0,
        cpu_percent_max=2.0,
        ram_used_bytes_mean=1,
        ram_used_bytes_max=2,
        ram_total_bytes=3,
        gpu={},
        disks={},
    )


def make(registry: StubRegistry, wall: dt.datetime) -> SnapshotFlusher:
    tracker = ResourceTracker(bucket_s=60.0, wall_clock=lambda: wall)
    settings = ResourceHistorySettings(bucket_s=60.0, flush_interval_s=0.01)
    return SnapshotFlusher(
        tracker,
        cast(Registry, registry),
        settings,
        clock=lambda: 0.0,
        wall_clock=lambda: wall,
    )


@pytest.mark.asyncio
async def test_a_full_outage_during_isolation_does_not_drop_an_unconfirmed_row() -> (
    None
):
    """A row must not be dropped on a failure that coincides with every other row
    in the batch also failing - that is a database outage, not row poison, even
    when an unrelated earlier poison episode already set the ``_isolating`` latch.
    """
    registry = StubRegistry()
    flusher = make(registry, T0)

    # Step 1: seed one genuine, confirmed individual failure so the isolation
    # latch (``_isolating``) is armed exactly the way a real poison row would
    # arm it - one probe got through, one did not.
    good = [row_at(NODE, minute) for minute in range(3)]
    suspect = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (suspect.node_id, suspect.bucket_start)
    flusher._pending = [*good, suspect]

    assert await flusher.flush_once() == 0  # batch fails as a whole
    assert (
        await flusher.flush_once() == 0
    )  # isolation: good rows land, suspect blamed once
    assert flusher._isolating is True
    assert flusher._row_failures[(suspect.node_id, suspect.bucket_start)] == 1
    assert flusher.dropped_poison == 0
    assert flusher.pending == 1  # only the suspect row remains

    # Step 2: before the suspect row gets a *second* real, independent chance to
    # prove itself poison, the database goes down entirely (a plain outage, not
    # this row's fault - no other row is even present to distinguish the two).
    registry.poison = None
    registry.fail_writes = True
    await flusher.flush_once()

    # Expected: an outage where literally every probed row fails must never be
    # attributed to any specific row, exactly as the very first full-outage case
    # (before any latch was armed) already guarantees. The row must still be
    # pending, not dropped.
    assert flusher.dropped_poison == 0, (
        "the suspect row was dropped on a failure that coincided with a total "
        "outage (no row succeeded) - it was never individually confirmed poison "
        "a second time, only unlucky enough to be probed during an outage while "
        "the isolating latch from an earlier, unrelated episode was still set"
    )
    assert flusher.pending == 1

    # Step 3: the database genuinely recovers. The row must still be here to
    # write - if it was wrongly dropped, this assertion is what proves the loss.
    registry.fail_writes = False
    assert await flusher.flush_once() == 1
    assert flusher.pending == 0


@pytest.mark.asyncio
async def test_dropping_a_row_requires_two_failures_each_proven_against_a_live_database() -> (
    None
):
    """Sanity check on the *intended* path: two failures that both coincide with
    other rows succeeding is the only sequence the docstring authorises a drop
    for. This must keep passing - it is the isolation feature actually working.
    """
    assert DROP_AFTER_ROW_FAILURES == 2
    registry = StubRegistry()
    flusher = make(registry, T0)
    good = [row_at(NODE, minute) for minute in range(3)]
    bad = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (bad.node_id, bad.bucket_start)
    flusher._pending = [*good, bad]

    assert await flusher.flush_once() == 0
    assert await flusher.flush_once() == 0  # isolation: bad blamed once, others land
    assert flusher.dropped_poison == 0

    # A third tick where the database is demonstrably up (a fresh good row lands
    # alongside the still-poison one) is the only legitimate way to the second
    # strike.
    flusher._pending = [*flusher._pending, row_at(NODE, 20)]
    assert await flusher.flush_once() == 0
    assert flusher.dropped_poison == 1
    assert flusher.pending == 0
