"""V0.3 G4: the replay dedupe across a real reconnect (tester CRITICAL, now fixed).

The tester round found that the original ``(session_epoch, sequence)`` dedupe key
could not dedupe a replay, because the two halves of the key come from different
lifetimes: ``agent/session/client.py`` bumps ``identity.next_epoch`` on every
connection *attempt* ("Per attempt", ADR-0002 Updates), while ``sequence`` is a
single per-*process* counter that is never reset, and the controller records with
``live.session_epoch`` - the epoch of the session the report *arrived* on, not the
one it was taken under. A report sent as ``(epoch 5, seq 100)`` whose Ack was lost
came back after the reconnect as ``(epoch 6, seq 100)``, compared greater, and was
counted twice in both the live ring and the history buckets.

The fix keys on ``sequence`` alone and resets that cursor from
``Reconcile.replay_from_sequence``, which is already on the wire:

* session opens      -> ``begin_session(node, 0)``  : arm a reset (maybe a new process)
* ``Reconcile`` > 0  -> ``begin_session(node, R)``  : this process is replaying, keep it
* ``Reconcile`` == 0 -> nothing buffered, the armed reset is harmless

These tests assert that behaviour, and that ``session_epoch`` still fences a
superseded stream out.
"""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.resources import ResourceTracker

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"


def update(*, sequence: int, cpu: float) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(cpu_percent=cpu, sequence=sequence)
    message.observed_at.FromDatetime(T0)
    return message


def test_a_replay_after_a_reconnect_with_a_bumped_epoch_is_deduped() -> None:
    """The exact defect the tester reproduced, now the other way round.

    Report #5 is recorded under epoch 1; the stream drops before the agent sees
    its Ack, so it stays in the replay ring. The reconnect bumps the epoch to 2
    for the *same process* and replays sequence 5 unchanged.
    """
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)  # the first session of this process
    assert tracker.record(NODE, 1, update(sequence=5, cpu=10.0), 100.0) is True

    # Reconnect: a new session opens, then its Reconcile announces the replay.
    tracker.begin_session(NODE, 0)
    tracker.begin_session(NODE, 5)  # Reconcile{replay_from_sequence=5}
    assert tracker.record(NODE, 2, update(sequence=5, cpu=10.0), 105.0) is False

    assert tracker.recorded == 1
    assert tracker.duplicates == 1
    ring = tracker.ring(NODE)
    assert ring is not None
    assert len(ring) == 1, "the replayed report must not occupy a second ring slot"

    rows = tracker.closed_rows(T0 + dt.timedelta(minutes=2))
    assert len(rows) == 1
    assert rows[0].samples == 1, "one observation must be folded into the bucket once"


def test_a_report_the_controller_never_saw_is_still_accepted_from_the_replay() -> None:
    """Un-acked does not mean recorded: the ring also replays what never arrived."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=5, cpu=10.0), 100.0) is True

    tracker.begin_session(NODE, 0)
    tracker.begin_session(NODE, 5)
    assert tracker.record(NODE, 2, update(sequence=5, cpu=10.0), 105.0) is False
    # Sequence 6 was written to the wire but never reached the controller.
    assert tracker.record(NODE, 2, update(sequence=6, cpu=20.0), 105.1) is True
    assert tracker.recorded == 2


def test_a_restarted_process_resets_the_cursor_because_it_sends_no_reconcile() -> None:
    """A fresh process starts at sequence 1 again and sends no Reconcile at all
    (``client.py``: ``reconnect = self.stats.sessions_accepted > 0``), so the
    session's own ``begin_session(node, 0)`` is what clears the cursor."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=7, cpu=10.0), 100.0) is True

    tracker.begin_session(NODE, 0)  # new process, higher epoch, no Reconcile
    assert tracker.record(NODE, 2, update(sequence=1, cpu=11.0), 200.0) is True
    assert tracker.duplicates == 0
    assert tracker.recorded == 2


def test_a_reconnect_with_an_empty_ring_resets_harmlessly() -> None:
    """``Reconcile{replay_from_sequence=0}`` (G3's reviewer fix): nothing is
    buffered, so resetting the cursor costs nothing - the next sequence is
    higher anyway."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=5, cpu=10.0), 100.0) is True
    tracker.begin_session(NODE, 0)
    tracker.begin_session(NODE, 0)  # Reconcile with an empty ring
    assert tracker.record(NODE, 2, update(sequence=6, cpu=12.0), 101.0) is True
    assert tracker.duplicates == 0


def test_the_armed_reset_only_applies_to_the_first_report_of_the_session() -> None:
    """Once the session's first report has landed, the session's own duplicates
    are rejected again (the reset is consumed, not sticky)."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 4, update(sequence=1, cpu=10.0), 100.0) is True
    assert tracker.record(NODE, 4, update(sequence=1, cpu=10.0), 100.5) is False
    assert tracker.duplicates == 1


def test_an_epoch_older_than_the_highest_seen_is_fenced_out() -> None:
    """A superseded stream must not move the cursor, whatever its sequence is."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 9, update(sequence=1, cpu=10.0), 100.0) is True
    assert tracker.record(NODE, 8, update(sequence=999, cpu=99.0), 100.1) is False
    assert tracker.stale_epochs == 1
    ring = tracker.ring(NODE)
    assert ring is not None
    assert [s.session_epoch for s in ring.snapshot()] == [9]
