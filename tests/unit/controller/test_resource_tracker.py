"""V0.3 G4 (roadmap 0.3.11): live ring, bucket arithmetic, aggregation and bounds.

Pure in-memory state; no database, no gRPC. Wall clock and monotonic clock are both
injected, so a bucket boundary is exercised exactly, not approximately.
"""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.resources import (
    Downsampler,
    LiveRing,
    LiveSample,
    ResourceTracker,
    bucket_start,
    live_sample,
    total_samples,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"
OTHER = "node_02TESTTESTTESTTESTTESTTEST"


def update(
    *,
    sequence: int = 1,
    cpu: float = 10.0,
    ram_used: int = 1_000,
    at: dt.datetime | None = T0,
    gpu_util: float | None = None,
    free_bytes: int | None = None,
) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(
        sequence=sequence,
        cpu_percent=cpu,
        ram_used_bytes=ram_used,
        ram_total_bytes=8_000,
    )
    if at is not None:
        message.observed_at.FromDatetime(at)
    if gpu_util is not None:
        message.gpus.add(
            index=0, util_percent=gpu_util, vram_used_bytes=100, vram_total_bytes=800
        )
    if free_bytes is not None:
        message.disks.add(mount="/", free_bytes=free_bytes, total_bytes=1_000)
    return message


def sample_at(
    moment: dt.datetime,
    *,
    sequence: int = 1,
    cpu: float = 10.0,
    ram_used: int = 1_000,
    gpu_util: float | None = None,
    free_bytes: int | None = None,
) -> LiveSample:
    return live_sample(
        update(
            sequence=sequence,
            cpu=cpu,
            ram_used=ram_used,
            at=moment,
            gpu_util=gpu_util,
            free_bytes=free_bytes,
        ),
        session_epoch=1,
        received_at=0.0,
    )


# ---------------------------------------------------------------- live ring


def test_the_live_ring_keeps_the_newest_samples_in_order() -> None:
    ring = LiveRing(3)
    for index in range(5):
        ring.append(sample_at(T0 + dt.timedelta(seconds=index), sequence=index))
    assert len(ring) == 3
    assert [s.sequence for s in ring.snapshot()] == [2, 3, 4]
    assert ring.latest is not None and ring.latest.sequence == 4


def test_an_empty_ring_has_no_latest() -> None:
    assert LiveRing(2).latest is None
    with pytest.raises(ValueError, match="capacity"):
        LiveRing(0)


def test_live_sample_without_an_observation_time_uses_the_receive_time() -> None:
    fixed = T0 + dt.timedelta(hours=1)
    sample = live_sample(
        update(at=None), session_epoch=3, received_at=9.0, wall_clock=lambda: fixed
    )
    assert sample.observed_at == fixed
    assert sample.session_epoch == 3
    assert sample.received_at == 9.0


# ------------------------------------------------------------------ buckets


def test_a_sample_exactly_on_a_boundary_lands_in_the_new_bucket() -> None:
    assert bucket_start(T0, 60.0) == T0
    assert bucket_start(T0 + dt.timedelta(seconds=59.999), 60.0) == T0
    assert bucket_start(T0 + dt.timedelta(seconds=60), 60.0) == T0 + dt.timedelta(
        minutes=1
    )


def test_bucket_start_normalises_a_non_utc_timestamp() -> None:
    other_zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert bucket_start(T0.astimezone(other_zone), 60.0) == T0


def test_bucket_start_rejects_a_non_positive_length() -> None:
    with pytest.raises(ValueError, match="bucket_s"):
        bucket_start(T0, 0.0)


def test_mean_and_max_are_computed_per_bucket() -> None:
    down = Downsampler(bucket_s=60.0)
    for index, cpu in enumerate((10.0, 20.0, 60.0)):
        down.add(
            NODE, sample_at(T0 + dt.timedelta(seconds=index), cpu=cpu, ram_used=index)
        )
    rows = down.closed_buckets(T0 + dt.timedelta(minutes=2))
    assert len(rows) == 1
    row = rows[0]
    assert row.bucket_start == T0
    assert row.samples == 3
    assert row.cpu_percent_mean == pytest.approx(30.0)
    assert row.cpu_percent_max == 60.0
    assert row.ram_used_bytes_mean == 1
    assert row.ram_used_bytes_max == 2
    assert row.ram_total_bytes == 8_000


def test_an_open_bucket_is_never_returned() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0))
    assert down.closed_buckets(T0 + dt.timedelta(seconds=30)) == []
    assert down.open_buckets == 1
    assert len(down.closed_buckets(T0 + dt.timedelta(seconds=60))) == 1
    assert down.open_buckets == 0


def test_a_bucket_with_one_sample_is_written() -> None:
    down = Downsampler(bucket_s=1.0)
    down.add(NODE, sample_at(T0, cpu=7.0))
    rows = down.closed_buckets(T0 + dt.timedelta(seconds=5))
    assert total_samples(rows) == 1
    assert rows[0].cpu_percent_mean == rows[0].cpu_percent_max == 7.0


def test_gpu_and_disk_aggregates_are_per_index_and_per_mount() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0, gpu_util=10.0, free_bytes=900))
    down.add(
        NODE, sample_at(T0 + dt.timedelta(seconds=1), gpu_util=30.0, free_bytes=700)
    )
    row = down.closed_buckets(T0 + dt.timedelta(minutes=2))[0]
    assert row.gpu["0"]["util_mean"] == pytest.approx(20.0)
    assert row.gpu["0"]["util_max"] == 30.0
    assert row.gpu["0"]["vram_total"] == 800
    assert row.gpu["0"]["samples"] == 2
    assert row.disks["/"]["free_min"] == 700
    assert row.disks["/"]["free_mean"] == 800
    assert row.disks["/"]["total"] == 1_000


def test_an_out_of_order_sample_within_an_open_bucket_is_accepted() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0 + dt.timedelta(seconds=30), cpu=10.0))
    down.add(NODE, sample_at(T0 + dt.timedelta(seconds=5), cpu=20.0))
    row = down.closed_buckets(T0 + dt.timedelta(minutes=2))[0]
    assert row.samples == 2
    assert row.cpu_percent_mean == pytest.approx(15.0)


def test_a_sample_for_an_already_written_bucket_is_dropped_and_counted() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0))
    assert len(down.closed_buckets(T0 + dt.timedelta(minutes=2))) == 1
    down.add(NODE, sample_at(T0 + dt.timedelta(seconds=10)))
    assert down.dropped_late == 1
    assert down.open_buckets == 0


def test_too_many_open_buckets_drop_the_oldest_with_a_counter() -> None:
    down = Downsampler(bucket_s=60.0, max_open_buckets=2)
    for minutes in range(5):
        down.add(NODE, sample_at(T0 + dt.timedelta(minutes=minutes)))
    assert down.open_buckets == 2
    assert down.dropped_buckets == 3
    rows = down.closed_buckets(T0 + dt.timedelta(hours=1))
    assert [row.bucket_start for row in rows] == [
        T0 + dt.timedelta(minutes=3),
        T0 + dt.timedelta(minutes=4),
    ]


def test_drain_returns_open_buckets_too() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0))
    assert len(down.drain()) == 1
    assert down.open_buckets == 0


def test_buckets_of_two_nodes_are_independent() -> None:
    down = Downsampler(bucket_s=60.0)
    down.add(NODE, sample_at(T0, cpu=10.0))
    down.add(OTHER, sample_at(T0, cpu=90.0))
    rows = down.closed_buckets(T0 + dt.timedelta(minutes=2))
    assert {row.node_id for row in rows} == {NODE, OTHER}
    assert len(rows) == 2


# ------------------------------------------------------------------ tracker


def test_the_tracker_records_a_sample_into_the_ring_and_the_downsampler() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    assert tracker.record(NODE, 1, update(sequence=1), 100.0) is True
    ring = tracker.ring(NODE)
    assert ring is not None and len(ring) == 1
    assert tracker.latest(NODE) is not None
    assert tracker.nodes() == [NODE]
    assert tracker.recorded == 1


def test_a_replayed_update_is_not_double_counted() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.record(NODE, 1, update(sequence=1), 100.0)
    tracker.record(NODE, 1, update(sequence=2), 101.0)
    # the reconnect: the same reports are replayed on the new stream
    assert tracker.record(NODE, 1, update(sequence=1), 102.0) is False
    assert tracker.record(NODE, 1, update(sequence=2), 103.0) is False
    assert tracker.duplicates == 2
    ring = tracker.ring(NODE)
    assert ring is not None and len(ring) == 2


def test_a_restarted_agent_starts_its_sequence_again_and_is_accepted() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.record(NODE, 1, update(sequence=7), 100.0)
    # New process: its session opens (begin_session arms a cursor reset) and it
    # sends no Reconcile, so sequence 1 is a new sample, not a duplicate.
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 2, update(sequence=1), 101.0) is True
    assert tracker.duplicates == 0


def test_a_session_announcing_a_replay_keeps_its_cursor() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.record(NODE, 1, update(sequence=7), 100.0)
    tracker.begin_session(NODE, 0)  # the reconnect's session opens
    tracker.begin_session(NODE, 7)  # its Reconcile: replaying from sequence 7
    assert tracker.record(NODE, 2, update(sequence=7), 101.0) is False
    assert tracker.duplicates == 1


def test_a_sequence_gap_is_recorded_not_rejected() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.record(NODE, 1, update(sequence=1), 100.0)
    assert tracker.record(NODE, 1, update(sequence=9), 101.0) is True
    ring = tracker.ring(NODE)
    assert ring is not None
    assert [s.sequence for s in ring.snapshot()] == [1, 9]


def test_tracking_too_many_nodes_evicts_the_least_recently_updated() -> None:
    tracker = ResourceTracker(live_ring_size=2, bucket_s=60.0, max_tracked_nodes=2)
    tracker.record("node_a", 1, update(sequence=1), 1.0)
    tracker.record("node_b", 1, update(sequence=1), 2.0)
    tracker.record("node_a", 1, update(sequence=2), 3.0)  # a is now the newest
    tracker.record("node_c", 1, update(sequence=1), 4.0)
    assert tracker.evicted_nodes == 1
    assert set(tracker.nodes()) == {"node_a", "node_c"}
    assert tracker.ring("node_b") is None
    # Only the *ring* is evicted. The dedupe cursor lives in its own, much larger
    # LRU (max_cursor_nodes), so node_b replaying sequence 1 is still a duplicate
    # - ring pressure from other nodes must never reopen the double-count window.
    assert tracker.record("node_b", 1, update(sequence=1), 5.0) is False
    assert tracker.evicted_cursors == 0
    # A genuinely newer sample re-enrols node_b (and rebuilds its ring).
    assert tracker.record("node_b", 1, update(sequence=2), 6.0) is True


def test_cursor_eviction_is_counted_and_only_happens_under_its_own_cap() -> None:
    """The cursor LRU is what finally lets a duplicate through, so it is counted.

    ``max_cursor_nodes`` is clamped to at least ``max_tracked_nodes``; here both
    are 1, so every new node evicts the previous node's cursor.
    """
    tracker = ResourceTracker(
        live_ring_size=2, bucket_s=60.0, max_tracked_nodes=1, max_cursor_nodes=1
    )
    assert tracker.record("node_a", 1, update(sequence=5), 1.0) is True
    assert tracker.record("node_b", 1, update(sequence=5), 2.0) is True
    assert tracker.evicted_cursors == 1
    assert tracker.evicted_nodes == 1
    # node_a's cursor is gone, so its replay is (unavoidably) accepted again.
    assert tracker.record("node_a", 1, update(sequence=5), 3.0) is True
    assert tracker.evicted_cursors == 2


def test_max_cursor_nodes_is_never_below_max_tracked_nodes() -> None:
    tracker = ResourceTracker(bucket_s=60.0, max_tracked_nodes=8, max_cursor_nodes=2)
    for index in range(8):
        assert tracker.record(f"node_{index}", 1, update(sequence=1), float(index))
    assert tracker.evicted_nodes == 0
    assert tracker.evicted_cursors == 0


def test_closed_rows_use_the_injected_wall_clock() -> None:
    now = T0
    tracker = ResourceTracker(bucket_s=60.0, wall_clock=lambda: now)
    tracker.record(NODE, 1, update(sequence=1, at=T0), 1.0)
    assert tracker.closed_rows() == []
    now = T0 + dt.timedelta(minutes=2)
    rows = tracker.closed_rows()
    assert len(rows) == 1 and rows[0].node_id == NODE


def test_begin_session_raises_the_epoch_high_water_mark() -> None:
    """NB-9: without the epoch from Hello the fence is inert in the window between
    a session opening and its first report, so a zombie stream could get one
    report in. ``begin_session`` now raises the mark immediately."""
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.begin_session(NODE, 0, session_epoch=7)
    assert tracker.record(NODE, 6, update(sequence=1), 1.0) is False
    assert tracker.stale_epochs == 1
    assert tracker.record(NODE, 7, update(sequence=1), 2.0) is True


def test_begin_session_never_lowers_the_epoch_high_water_mark() -> None:
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    tracker.begin_session(NODE, 0, session_epoch=9)
    tracker.begin_session(NODE, 0, session_epoch=3)  # a late/zombie Hello
    assert tracker.record(NODE, 8, update(sequence=1), 1.0) is False
    assert tracker.stale_epochs == 1
