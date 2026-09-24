"""V0.3 G6 fix round, D26 (LATE-1): a history bucket is closed only after a grace
period, and a sample later than that is counted and logged - never silently
dropped, never merged into a stored row.

Pure logic with an injected wall clock; no database, no sleep.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, cast

import pytest
import structlog
from pydantic import ValidationError

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import Registry
from meshforge.controller.resources import (
    DEFAULT_BUCKET_GRACE_S,
    Downsampler,
    LiveSample,
    ResourceTracker,
    live_sample,
)
from meshforge.controller.settings import ControllerSettings, ResourceHistorySettings
from meshforge.controller.snapshots import SnapshotFlusher

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"
OTHER = "node_02TESTTESTTESTTESTTESTTEST"


class Wall:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def update(sequence: int, at: dt.datetime) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(
        sequence=sequence, cpu_percent=10.0, ram_used_bytes=1, ram_total_bytes=2
    )
    message.observed_at.FromDatetime(at)
    return message


def sample(at: dt.datetime, sequence: int = 1) -> LiveSample:
    return live_sample(update(sequence, at), session_epoch=3, received_at=0.0)


def secs(n: float) -> dt.timedelta:
    return dt.timedelta(seconds=n)


def late_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "controller.resources.sample_late"]


# ------------------------------------------------------------------- the grace


def test_the_default_grace_is_the_documented_setting_default() -> None:
    assert DEFAULT_BUCKET_GRACE_S == 20.0
    assert ResourceHistorySettings().bucket_grace_s == DEFAULT_BUCKET_GRACE_S
    assert Downsampler().bucket_grace_s == DEFAULT_BUCKET_GRACE_S
    assert ResourceTracker().downsampler.bucket_grace_s == DEFAULT_BUCKET_GRACE_S


def test_the_setting_reaches_the_tracker_and_rejects_nonsense() -> None:
    cfg = ControllerSettings.load(controller={"resources": {"bucket_grace_s": 3.5}})
    assert cfg.controller.resources.bucket_grace_s == 3.5
    with pytest.raises(ValidationError, match="bucket_grace_s"):
        ResourceHistorySettings(bucket_grace_s=-1.0)
    with pytest.raises(ValueError, match="bucket_grace_s"):
        Downsampler(bucket_grace_s=-0.1)


def test_a_bucket_stays_open_through_its_grace_and_closes_at_the_edge() -> None:
    down = Downsampler(bucket_s=60.0, bucket_grace_s=20.0)
    down.add(NODE, sample(T0))
    # Ended (T0+60) but in grace: not closed, and a late-by-19 s sample still counts.
    assert down.closed_buckets(T0 + secs(79.999)) == []
    down.add(NODE, sample(T0 + secs(59), sequence=2))
    rows = down.closed_buckets(T0 + secs(80))  # end + grace: closed exactly here
    assert [(r.bucket_start, r.samples) for r in rows] == [(T0, 2)]
    assert down.dropped_late == 0


def test_the_shutdown_flush_does_not_wait_for_the_grace() -> None:
    down = Downsampler(bucket_s=60.0, bucket_grace_s=20.0)
    down.add(NODE, sample(T0))
    down.add(NODE, sample(T0 + secs(61), sequence=2))  # the next, still-open bucket
    rows = down.closed_buckets(T0 + secs(65), grace=False)
    # Ended buckets only: the open one would look complete while it is partial.
    assert [r.bucket_start for r in rows] == [T0]
    assert down.open_buckets == 1


# ---------------------------------------------------------------- late samples


def test_a_sample_later_than_the_grace_is_counted_and_logged_not_merged() -> None:
    wall = Wall(T0 + secs(95))
    down = Downsampler(bucket_s=60.0, bucket_grace_s=20.0, wall_clock=wall)
    down.add(NODE, sample(T0))
    assert len(down.closed_buckets(T0 + secs(90))) == 1
    with structlog.testing.capture_logs() as logs:
        down.add(NODE, sample(T0 + secs(30), sequence=7))
    assert down.dropped_late == 1
    [event] = late_events(logs)
    assert event["log_level"] == "warning"
    assert event["node_id"] == NODE
    assert event["bucket_start"] == T0.isoformat()
    # Arrived at T0+95; its bucket ended at T0+60.
    assert event["lateness_s"] == pytest.approx(35.0)
    assert event["grace_s"] == 20.0
    assert (event["sequence"], event["session_epoch"]) == (7, 3)
    # Never merged: no second (partial) row for the stored bucket, ever.
    assert down.open_buckets == 0
    assert down.closed_buckets(T0 + secs(3600)) == []


def test_a_replay_of_many_late_samples_warns_once_per_bucket_and_counts_all() -> None:
    down = Downsampler(bucket_s=60.0, bucket_grace_s=0.0, wall_clock=Wall(T0))
    down.add(NODE, sample(T0))
    down.add(NODE, sample(T0 + secs(60)))
    down.add(OTHER, sample(T0))
    down.closed_buckets(T0 + secs(200))
    with structlog.testing.capture_logs() as logs:
        for i in range(5):
            down.add(NODE, sample(T0 + secs(i), sequence=10 + i))
        for i in range(3):
            down.add(NODE, sample(T0 + secs(60 + i), sequence=20 + i))
        down.add(OTHER, sample(T0 + secs(1), sequence=30))
    assert down.dropped_late == 9
    events = late_events(logs)
    assert len(events) == 9  # every late sample is logged ...
    warned = [
        (e["node_id"], e["bucket_start"]) for e in events if e["log_level"] == "warning"
    ]
    # ... but only the first of each (node, bucket) at warning level.
    assert warned == [
        (NODE, T0.isoformat()),
        (NODE, (T0 + secs(60)).isoformat()),
        (OTHER, T0.isoformat()),
    ]
    assert events[-1]["dropped_late"] == 9


def test_the_tracker_keeps_a_late_sample_in_the_live_ring() -> None:
    wall = Wall(T0 + secs(200))
    tracker = ResourceTracker(bucket_s=60.0, bucket_grace_s=20.0, wall_clock=wall)
    assert tracker.record(NODE, 3, update(1, T0), 1.0)
    assert len(tracker.closed_rows()) == 1
    assert tracker.record(NODE, 3, update(2, T0 + secs(10)), 2.0)  # late, not a dup
    ring = tracker.ring(NODE)
    assert ring is not None
    assert [s.sequence for s in ring.snapshot()] == [1, 2]
    assert tracker.downsampler.dropped_late == 1
    assert tracker.duplicates == 0


# ------------------------------------------------------- the open-bucket cap


def test_buckets_in_their_grace_do_not_count_against_max_open_buckets() -> None:
    down = Downsampler(bucket_s=1.0, max_open_buckets=2, bucket_grace_s=5.0)
    for i in range(7):  # 2 open + 5 in grace: all expected, none a clock jump
        down.add(NODE, sample(T0 + secs(i), sequence=i + 1))
    assert down.dropped_buckets == 0
    assert down.open_buckets == 7
    down.add(NODE, sample(T0 + secs(7), sequence=8))  # one more is a jump: evict
    assert down.dropped_buckets == 1


# ------------------------------------------------------------------ flusher


class RecordingRegistry:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def upsert_resource_snapshots(self, rows: Any) -> int:
        batch = list(rows)
        self.rows.extend(batch)
        return len(batch)


@pytest.mark.asyncio
async def test_the_flusher_waits_for_the_grace_but_its_final_flush_does_not() -> None:
    wall = Wall(T0 + secs(70))  # bucket T0 ended, still in its 20 s grace
    tracker = ResourceTracker(bucket_s=60.0, bucket_grace_s=20.0, wall_clock=wall)
    registry = RecordingRegistry()
    flusher = SnapshotFlusher(
        tracker,
        cast(Registry, registry),
        ResourceHistorySettings(bucket_s=60.0, bucket_grace_s=20.0),
        clock=asyncio.get_running_loop().time,
        wall_clock=wall,
    )
    tracker.record(NODE, 3, update(1, T0), 1.0)
    assert await flusher.flush_once() == 0
    assert await flusher.final_flush() == 1
    assert [r.bucket_start for r in registry.rows] == [T0]
