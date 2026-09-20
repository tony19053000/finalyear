"""V0.3 G4 tester round: dedupe edge cases beyond the implementer's own coverage.

``ResourceTracker.record`` is keyed on ``(session_epoch, sequence)`` (deviation 1
of the G4 handoff: "sequence alone breaks across an agent restart"). These tests
attack the ordering directly: a zombie session's late-arriving report must never
resurrect itself as "new" just because its sequence number is numerically large.
"""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.resources import ResourceTracker

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"


def update(*, sequence: int, cpu: float = 10.0) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(cpu_percent=cpu, sequence=sequence)
    message.observed_at.FromDatetime(T0)
    return message


def test_a_zombie_sessions_huge_sequence_cannot_beat_a_newer_epochs_small_one() -> None:
    """A controller-fencing race: an old (stale) session with epoch 1 is still
    delivering buffered reports with high sequence numbers (e.g. its replay ring
    had a lot queued) at the same time epoch 2's session has already reported a
    fresh, low-sequence sample. Tuple ordering must prefer the epoch, not the
    sequence - the brief's exact worry ("sequence alone breaks across an agent
    restart") - so the *old* session's report, however large its sequence, must
    never overwrite what a newer process already reported.
    """
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    # Epoch 2 (the new, legitimate process) reports first, sequence 1.
    assert tracker.record(NODE, 2, update(sequence=1), 100.0) is True
    # Epoch 1 (the zombie stream, somehow still delivering after a fencing bug
    # elsewhere) tries to deliver a report with a much bigger sequence number.
    assert tracker.record(NODE, 1, update(sequence=999_999), 101.0) is False
    assert tracker.duplicates == 1
    # The ring must still reflect only the epoch-2 sample.
    ring = tracker.ring(NODE)
    assert ring is not None
    assert [s.session_epoch for s in ring.snapshot()] == [2]


def test_two_updates_with_the_identical_epoch_and_sequence_are_deduped() -> None:
    """The exact-replay case: same epoch, same sequence, byte-identical message
    (a stream that replayed a report it was never acked for)."""
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    assert tracker.record(NODE, 3, update(sequence=5), 100.0) is True
    assert tracker.record(NODE, 3, update(sequence=5), 100.5) is False
    assert tracker.recorded == 1
    assert tracker.duplicates == 1


def test_epoch_zero_and_sequence_zero_is_a_legal_first_sample() -> None:
    """A brand new node's very first report - epoch and sequence both at their
    zero value - must be accepted, not treated as "not strictly newer than
    nothing" by an off-by-one in the initial cursor comparison."""
    tracker = ResourceTracker(live_ring_size=5, bucket_s=60.0)
    assert tracker.record(NODE, 0, update(sequence=0), 100.0) is True
    assert tracker.recorded == 1
    # A genuine duplicate of that exact first sample is still rejected.
    assert tracker.record(NODE, 0, update(sequence=0), 100.1) is False
