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
        self.deleted = 3

    async def upsert_resource_snapshots(self, rows: Any) -> int:
        if self.fail_writes:
            raise RegistryError(
                "connection refused", details={"operation": "upsert_resource_snapshots"}
            )
        batch = list(rows)
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
