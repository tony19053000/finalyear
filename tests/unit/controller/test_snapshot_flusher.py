"""V0.3 G4 (roadmap 0.3.11): the snapshot flusher's retry, retention and shutdown.

The registry here is a stub that either records the batch or raises the *real*
:class:`~meshforge.controller.registry.RegistryError` the async engine raises, so
the retry path is exercised without a container. The flusher against a real
PostgreSQL is the component test (``tests/component/controller``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, cast

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import Registry, RegistryError
from meshforge.controller.resources import ResourceTracker, SnapshotRow
from meshforge.controller.settings import ResourceHistorySettings
from meshforge.controller.snapshots import SnapshotFlusher

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"


class StubRegistry:
    """Records what the flusher asked for; can fail on demand."""

    def __init__(self) -> None:
        self.batches: list[list[SnapshotRow]] = []
        self.cutoffs: list[dt.datetime] = []
        self.fail_writes = False
        self.fail_deletes = False
        self.raise_unexpected = False
        self.poison: tuple[str, dt.datetime] | None = None
        self.deleted = 3
        self.pings = 0

    async def ping(self) -> None:
        """Up unless the whole database is down (``poison`` is a per-row refusal)."""
        self.pings += 1
        if self.fail_writes:
            raise RegistryError("connection refused", details={"operation": "ping"})

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
        if self.raise_unexpected:
            raise TypeError("a malformed row reached the driver")
        self.batches.append(batch)
        return len(batch)

    async def delete_snapshots_before(self, cutoff: dt.datetime) -> int:
        if self.fail_deletes:
            raise RegistryError(
                "gone", details={"operation": "delete_snapshots_before"}
            )
        self.cutoffs.append(cutoff)
        return self.deleted


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def settings(**overrides: Any) -> ResourceHistorySettings:
    base: dict[str, Any] = {"bucket_s": 60.0, "flush_interval_s": 0.01}
    base.update(overrides)
    return ResourceHistorySettings(**base)


def update(sequence: int, at: dt.datetime) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(
        sequence=sequence, cpu_percent=10.0, ram_used_bytes=1, ram_total_bytes=2
    )
    message.observed_at.FromDatetime(at)
    return message


def make(
    registry: StubRegistry,
    wall: dt.datetime,
    *,
    max_pending_rows: int = 5_000,
    **overrides: Any,
) -> tuple[SnapshotFlusher, ResourceTracker, FakeClock]:
    tracker = ResourceTracker(bucket_s=60.0, wall_clock=lambda: wall)
    clock = FakeClock()
    flusher = SnapshotFlusher(
        tracker,
        cast(Registry, registry),
        settings(**overrides),
        clock=clock,
        wall_clock=lambda: wall,
        max_pending_rows=max_pending_rows,
    )
    return flusher, tracker, clock


@pytest.mark.asyncio
async def test_a_flush_with_nothing_closed_writes_nothing() -> None:
    registry = StubRegistry()
    flusher, tracker, _ = make(registry, T0)
    tracker.record(NODE, 1, update(1, T0), 1.0)
    assert await flusher.flush_once() == 0
    assert registry.batches == []


@pytest.mark.asyncio
async def test_closed_buckets_are_written_once() -> None:
    registry = StubRegistry()
    later = T0 + dt.timedelta(minutes=2)
    flusher, tracker, _ = make(registry, later)
    tracker.record(NODE, 1, update(1, T0), 1.0)
    tracker.record(NODE, 1, update(2, T0 + dt.timedelta(seconds=1)), 2.0)

    assert await flusher.flush_once() == 1
    assert flusher.written == 1
    assert registry.batches[0][0].samples == 2
    # Nothing is left behind, so a second flush is a no-op.
    assert await flusher.flush_once() == 0
    assert len(registry.batches) == 1


@pytest.mark.asyncio
async def test_a_database_failure_keeps_the_rows_for_the_next_tick() -> None:
    registry = StubRegistry()
    registry.fail_writes = True
    later = T0 + dt.timedelta(minutes=2)
    flusher, tracker, _ = make(registry, later)
    tracker.record(NODE, 1, update(1, T0), 1.0)

    assert await flusher.flush_once() == 0
    assert flusher.failures == 1
    assert flusher.pending == 1

    registry.fail_writes = False
    assert await flusher.flush_once() == 1
    assert flusher.pending == 0
    assert registry.batches[0][0].node_id == NODE


@pytest.mark.asyncio
async def test_the_pending_list_is_bounded_and_drops_the_oldest() -> None:
    registry = StubRegistry()
    registry.fail_writes = True
    later = T0 + dt.timedelta(hours=2)
    flusher, tracker, _ = make(registry, later, max_pending_rows=2)
    for minute in range(5):
        tracker.record(
            NODE,
            1,
            update(minute + 1, T0 + dt.timedelta(minutes=minute)),
            float(minute),
        )

    assert await flusher.flush_once() == 0
    assert flusher.pending == 2
    assert flusher.dropped_rows == 3


@pytest.mark.asyncio
async def test_the_retention_sweep_uses_the_configured_cutoff() -> None:
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0, retention_days=7.0)
    assert await flusher.sweep_once() == 3
    assert registry.cutoffs == [T0 - dt.timedelta(days=7)]
    assert flusher.deleted == 3


@pytest.mark.asyncio
async def test_a_failed_sweep_is_counted_and_not_fatal() -> None:
    registry = StubRegistry()
    registry.fail_deletes = True
    flusher, _, _ = make(registry, T0)
    assert await flusher.sweep_once() == 0
    assert flusher.failures == 1


@pytest.mark.asyncio
async def test_the_loop_flushes_and_sweeps_on_its_schedule() -> None:
    registry = StubRegistry()
    later = T0 + dt.timedelta(minutes=2)
    flusher, tracker, clock = make(
        registry, later, flush_interval_s=0.01, retention_sweep_s=0.0001
    )
    tracker.record(NODE, 1, update(1, T0), 1.0)
    task = asyncio.create_task(flusher.run())
    for _ in range(200):
        if registry.batches and registry.cutoffs:
            break
        clock.advance(1.0)
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert registry.batches
    assert registry.cutoffs


@pytest.mark.asyncio
async def test_final_flush_writes_closed_buckets_and_never_raises() -> None:
    registry = StubRegistry()
    registry.fail_writes = True
    later = T0 + dt.timedelta(minutes=2)
    flusher, tracker, _ = make(registry, later)
    tracker.record(NODE, 1, update(1, T0), 1.0)
    assert await flusher.final_flush() == 0  # the failure is swallowed and logged
    registry.fail_writes = False
    assert await flusher.final_flush() == 1


# --------------------------------------------------------------- poison rows


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


@pytest.mark.asyncio
async def test_one_poison_row_does_not_block_the_batch_forever() -> None:
    """NB-6: the retry is a single statement, so a row the database always
    refuses used to fail every later flush until the pending cap evicted it."""
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0)
    good = [row_at(NODE, minute) for minute in range(3)]
    bad = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (bad.node_id, bad.bucket_start)
    flusher._pending = [*good, bad]

    # First tick: the batch fails as a whole, nothing is blamed yet.
    assert await flusher.flush_once() == 0
    assert flusher.pending == 4
    assert registry.batches == []

    # Second consecutive failure -> isolation pass: the good rows go in, the bad
    # one is blamed once and kept for one more try.
    assert await flusher.flush_once() == 0
    assert [len(batch) for batch in registry.batches] == [1, 1, 1]
    assert flusher.written == 3
    assert flusher.pending == 1
    assert flusher.dropped_poison == 0

    # It fails alone a second time while others succeed -> dropped and counted.
    assert await flusher.flush_once() == 0
    assert flusher.dropped_poison == 1
    assert flusher.pending == 0

    # And the flusher is healthy again.
    flusher._pending = [row_at(NODE, 20)]
    assert await flusher.flush_once() == 1


@pytest.mark.asyncio
async def test_an_unreachable_database_never_blames_a_row() -> None:
    """If nothing gets through during the isolation pass it is the database that
    is down, not a poison row: every row must survive."""
    registry = StubRegistry()
    registry.fail_writes = True
    flusher, _, _ = make(registry, T0)
    flusher._pending = [row_at(NODE, minute) for minute in range(4)]

    for _tick in range(5):
        assert await flusher.flush_once() == 0
    assert flusher.pending == 4
    assert flusher.dropped_poison == 0

    registry.fail_writes = False
    assert await flusher.flush_once() == 4


@pytest.mark.asyncio
async def test_an_unexpected_exception_does_not_escape_flush_once() -> None:
    """NB-5: ``flush_once`` caught only MeshForgeError, so a TypeError from a
    malformed row escaped to ``_on_task_done`` and stopped the controller."""
    registry = StubRegistry()
    registry.raise_unexpected = True
    flusher, _, _ = make(registry, T0)
    flusher._pending = [row_at(NODE, 0)]

    assert await flusher.flush_once() == 0
    assert flusher.failures == 1
    assert flusher.pending == 1


@pytest.mark.asyncio
async def test_a_cancelled_upsert_keeps_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NB-10: ``_pending`` used to be cleared before the await, so a cancellation
    mid-upsert lost up to one whole batch."""
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0)
    flusher._pending = [row_at(NODE, 0), row_at(NODE, 1)]

    async def cancelled(_rows: Any) -> int:
        raise asyncio.CancelledError

    monkeypatch.setattr(registry, "upsert_resource_snapshots", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await flusher.flush_once()
    assert flusher.pending == 2

    monkeypatch.undo()
    assert await flusher.flush_once() == 2


@pytest.mark.asyncio
async def test_an_unexpected_exception_does_not_escape_sweep_once() -> None:
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0)

    async def boom(_cutoff: dt.datetime) -> int:
        raise TypeError("driver said no")

    setattr(registry, "delete_snapshots_before", boom)  # noqa: B010
    assert await flusher.sweep_once() == 0
    assert flusher.failures == 1


@pytest.mark.asyncio
async def test_a_total_outage_never_blames_a_row_even_after_a_poison_episode() -> None:
    """ISO-1: the "nothing got through -> blame nobody" guarantee used to depend on
    the ``_isolating`` latch being unset, so a *later*, independent outage dropped a
    row that had only ever failed once for real. Liveness is now established in the
    current pass (``Registry.ping``), so no earlier episode can change the verdict."""
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0)
    good = [row_at(NODE, minute) for minute in range(3)]
    suspect = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (suspect.node_id, suspect.bucket_start)
    flusher._pending = [*good, suspect]

    assert await flusher.flush_once() == 0
    assert await flusher.flush_once() == 0  # isolation: latch armed, one strike
    assert flusher._isolating is True
    assert flusher.dropped_poison == 0

    # The database now goes down entirely, for several ticks.
    registry.poison = None
    registry.fail_writes = True
    for _tick in range(3):
        assert await flusher.flush_once() == 0
    assert flusher.dropped_poison == 0
    assert flusher.pending == 1
    assert registry.pings >= 1  # liveness really was asked, not assumed

    # It recovers: the row is still here to write, no history lost.
    registry.fail_writes = False
    assert await flusher.flush_once() == 1
    assert flusher.pending == 0


@pytest.mark.asyncio
async def test_a_lone_poison_row_is_still_dropped_when_the_ping_answers() -> None:
    """The other half of ISO-1: with the poison row the *only* row pending, no probe
    can ever succeed, so "blame nobody when nothing got through" would block every
    flush forever (the NB-6 bug). The ping supplies the missing proof."""
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0)
    bad = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (bad.node_id, bad.bucket_start)
    flusher._pending = [bad]

    assert await flusher.flush_once() == 0  # batch failure 1
    assert await flusher.flush_once() == 0  # isolation: ping up -> strike one
    assert flusher.dropped_poison == 0
    assert flusher._row_failures[(bad.node_id, bad.bucket_start)] == 1

    assert await flusher.flush_once() == 0  # strike two -> dropped
    assert flusher.dropped_poison == 1
    assert flusher.pending == 0
    assert flusher._row_failures == {}

    # The flusher is unblocked: healthy rows flow again on the next tick.
    registry.poison = None
    flusher._pending = [row_at(NODE, 20)]
    assert await flusher.flush_once() == 1


@pytest.mark.asyncio
async def test_row_failure_counts_do_not_outlive_their_rows() -> None:
    """A row blamed once and then evicted by the pending cap used to leave an
    orphaned key in ``_row_failures`` forever; counts are pruned to what is pending."""
    registry = StubRegistry()
    flusher, _, _ = make(registry, T0, max_pending_rows=2)
    good = row_at(NODE, 0)
    bad = row_at("node_02TESTTESTTESTTESTTESTTEST", 9)
    registry.poison = (bad.node_id, bad.bucket_start)
    flusher._pending = [good, bad]

    assert await flusher.flush_once() == 0
    assert await flusher.flush_once() == 0  # isolation: good lands, bad blamed once
    assert flusher._row_failures == {(bad.node_id, bad.bucket_start): 1}

    # Two fresh rows push the blamed row out of the (size-2) pending list.
    registry.fail_writes = True
    flusher._pending = [bad, row_at(NODE, 30), row_at(NODE, 31)]
    assert await flusher.flush_once() == 0
    assert flusher.dropped_rows == 1
    assert flusher._row_failures == {}
