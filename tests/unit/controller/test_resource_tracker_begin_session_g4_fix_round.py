"""V0.3 G4 fix round (R1): edge cases in ``begin_session`` arming beyond the
happy paths already covered by ``test_resource_tracker_reconnect_replay.py``
(replay rejected, restart accepted, same-process reconnect accepted, zombie
epoch rejected, empty-ring reconnect).

These target the interaction between the two independently-motivated pieces of
state a node carries: the "may be a fresh process" arming
(``_reset_armed``/``begin_session``) and the epoch fence
(``_epoch``), plus the ``max_tracked_nodes`` LRU eviction that can remove either
one out from under an in-flight reconnect.
"""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.resources import ResourceTracker

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.UTC)
NODE = "node_01TESTTESTTESTTESTTESTTEST"
OTHER = "node_02TESTTESTTESTTESTTESTTEST"


def update(*, sequence: int, cpu: float = 10.0) -> agent_pb2.ResourceUpdate:
    message = agent_pb2.ResourceUpdate(cpu_percent=cpu, sequence=sequence)
    message.observed_at.FromDatetime(T0)
    return message


def test_begin_session_called_twice_with_no_report_between_stays_armed() -> None:
    """Two session-open events in a row (e.g. a fast reconnect-reconnect) must
    not desynchronise the flag: it is still simply "armed" afterwards."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=1), 100.0) is True
    # The reset consumed by that first report: an exact duplicate is rejected.
    assert tracker.record(NODE, 1, update(sequence=1), 100.5) is False
    assert tracker.duplicates == 1


def test_a_report_with_no_begin_session_at_all_is_still_accepted() -> None:
    """A node the tracker has never called begin_session for (e.g. resources
    tracking was wired up after sessions already existed) must not crash or
    reject the first report - there is simply no cursor yet."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    assert tracker.record(NODE, 1, update(sequence=5), 100.0) is True
    assert tracker.record(NODE, 1, update(sequence=5), 100.5) is False, (
        "cursor must still be enforced normally even without a begin_session call"
    )
    assert tracker.recorded == 1
    assert tracker.duplicates == 1


def test_reconcile_arriving_after_the_first_report_does_not_retroactively_reset() -> (
    None
):
    """Out-of-order delivery: the session's first report lands before its own
    (late) Reconcile. The reset must already have been consumed by the report,
    so the late Reconcile must not go back and clear an established cursor."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 2, update(sequence=1), 100.0) is True
    # Reconcile arrives late, announcing a replay starting at 1 (this SAME report).
    tracker.begin_session(NODE, 1)
    # The replayed report itself must still be rejected as a duplicate.
    assert tracker.record(NODE, 2, update(sequence=1), 100.5) is False
    assert tracker.duplicates == 1
    assert tracker.recorded == 1


def test_two_nodes_arming_are_fully_independent() -> None:
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    tracker.begin_session(OTHER, 0)
    assert tracker.record(NODE, 1, update(sequence=10), 0.0) is True
    assert tracker.record(OTHER, 1, update(sequence=10), 0.0) is True

    # Reconnect NODE only; OTHER's armed-then-consumed state must be untouched.
    tracker.begin_session(NODE, 0)
    tracker.begin_session(NODE, 10)  # NODE replays; cursor kept
    assert tracker.record(NODE, 2, update(sequence=10), 1.0) is False  # replay rejected
    # OTHER was never touched: sequence 10 is still its cursor, so a duplicate
    # of OTHER must still be rejected too (proving OTHER's state wasn't reset).
    assert tracker.record(OTHER, 1, update(sequence=10), 1.0) is False


def test_replay_from_sequence_far_ahead_of_anything_ever_seen_does_not_crash() -> None:
    """``replay_from_sequence`` is only a > 0 / == 0 signal to disarm the reset,
    never itself installed as the cursor - a Reconcile that (wrongly, or from a
    device clock issue) claims a huge replay point must not silently accept
    everything below it as "already seen"."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=5), 100.0) is True
    tracker.begin_session(NODE, 1_000_000)  # disarm; cursor should remain 5
    assert tracker.record(NODE, 1, update(sequence=6), 100.5) is True
    assert tracker.record(NODE, 1, update(sequence=6), 100.6) is False


def test_sequence_zero_is_a_valid_first_sequence_and_still_deduped() -> None:
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=0), 100.0) is True
    assert tracker.record(NODE, 1, update(sequence=0), 100.1) is False


def test_uint64_boundary_sequence_is_handled() -> None:
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    huge = (2**64) - 1
    assert tracker.record(NODE, 1, update(sequence=huge), 100.0) is True
    assert tracker.record(NODE, 1, update(sequence=huge), 100.1) is False


def test_a_zombie_epoch_arriving_while_armed_cannot_move_the_cursor() -> None:
    """A stale/superseded stream delivering exactly while a legitimate reconnect
    is being armed must still be fenced out by epoch, not accidentally accepted
    because the node currently has an armed reset."""
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 5, update(sequence=1), 100.0) is True

    # A new session opens (arms the reset) ...
    tracker.begin_session(NODE, 0)
    # ... but a zombie from the OLD (epoch 5) stream delivers first, under a
    # lower epoch than nothing has actually superseded it with yet - wait, 5 is
    # still the highest seen, so an epoch of 4 is unambiguously a zombie.
    assert tracker.record(NODE, 4, update(sequence=999), 100.5) is False
    assert tracker.stale_epochs == 1
    # The arming must have survived the rejected zombie report: the genuine new
    # session's first real report must still get the fresh-process treatment.
    assert tracker.record(NODE, 6, update(sequence=1), 101.0) is True
    assert tracker.recorded == 2


def test_eviction_while_a_reconnect_is_armed_drops_the_kept_cursor() -> None:
    """DEFECT CANDIDATE (see tester report): max_tracked_nodes eviction pops
    cursor/epoch/reset-armed for whichever node is least recently updated,
    with no awareness that a *different* node's in-flight reconnect currently
    depends on that state surviving until its Reconcile disarms it.

    Sequence: NODE has history (LRU). A new session for NODE opens
    (``begin_session(NODE, 0)`` arms a reset) but its Reconcile has not arrived
    yet. Before it does, enough *other* nodes report for the first time to push
    NODE out of the tracker under ``max_tracked_nodes``. When NODE's Reconcile
    and replayed report finally arrive, the eviction has already deleted NODE's
    cursor/epoch - so the replay dedupe that Reconcile was supposed to enable
    cannot reject anything: the previously-recorded sample is accepted again
    into a brand-new ring/bucket.
    """
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0, max_tracked_nodes=1)
    tracker.begin_session(NODE, 0)
    assert tracker.record(NODE, 1, update(sequence=100), 0.0) is True
    assert tracker.evicted_nodes == 0

    # NODE's session drops; a new one opens and arms a reset, but its Reconcile
    # has not arrived yet (still "in flight").
    tracker.begin_session(NODE, 0)

    # A different node's first-ever report pushes NODE out (max_tracked_nodes=1).
    assert tracker.record(OTHER, 1, update(sequence=1), 1.0) is True
    assert tracker.evicted_nodes == 1, "NODE should have been evicted by the cap"

    # NODE's Reconcile now arrives, announcing a replay from 100 (i.e. "keep the
    # cursor, reject a duplicate of 100").
    tracker.begin_session(NODE, 100)
    accepted_again = tracker.record(NODE, 2, update(sequence=100), 2.0)

    assert accepted_again is False, (
        "DEFECT: eviction silently discarded NODE's cursor while a reconnect "
        "was armed, so the replayed sequence=100 sample (already recorded once "
        "before eviction) was accepted a second time into a fresh ring - the "
        "exact double-count shape R1 was meant to close, reopened via the "
        "eviction path."
    )
