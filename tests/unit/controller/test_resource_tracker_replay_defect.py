"""V0.3 G4 tester round: CRITICAL — the (session_epoch, sequence) dedupe key does
not actually dedupe a replay after a reconnect, because the two halves of the key
come from different lifetimes.

The G4 handoff's deviation 1 says: "``ResourceSink.record()`` takes
``session_epoch`` as well, so duplicates are keyed on ``(session_epoch,
sequence)`` rather than ``sequence`` alone - claim: sequence alone breaks across
an agent restart."

That claim assumes ``session_epoch`` only changes when the agent *process*
restarts. It does not. Per ``agent/session/client.py``'s own module docstring
("**Per attempt** (ADR-0002 Updates Sec1-Sec4): ``identity.next_epoch`` is
persisted... before the channel opens") and its ``__init__`` comment
(``self._report_sequence = 0  # reports, per process, never reset``),
**``session_epoch`` bumps on every reconnect attempt within one process**, while
``sequence`` is a single, never-reset, per-*process* counter. The controller
records into the sink with ``live.session_epoch`` - the *current* live session's
epoch (``controller/session/manager.py``'s ``_on_resource_update``) - not the
epoch that was live when the original sample was taken.

Consequence, reproduced below: an un-acked report sent once under epoch N gets
replayed (G3's own contract: "the replay of every un-acked report in the ring,
oldest first") after a reconnect bumps the epoch to N+1. The replay carries the
**same sequence number** it always had (sequence is never reset) but arrives
under the **new, larger** epoch. ``(session_epoch, sequence)`` is therefore
*strictly greater* than the previously recorded cursor, so the tracker accepts it
as a brand-new sample: it is pushed into the live ring a second time and folded
into the downsampler's mean/max a second time. Every lossy reconnect (the exact
case the replay ring exists for) silently duplicates data in the history table.
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


def test_a_replay_after_a_reconnect_is_double_recorded_not_deduped() -> None:
    """Reproduces the defect directly against ``ResourceTracker``, exactly as the
    controller's ``_on_resource_update`` calls it: ``session_epoch`` is the
    *current live session's* epoch (bumped per reconnect attempt), ``sequence``
    is the agent's never-reset, per-process counter.
    """
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)

    # Epoch 1 (first connection of this process): report #5 is sent and acked
    # from the controller's point of view, then the stream drops before the
    # agent itself sees the Ack, so it stays in the agent's replay ring.
    assert tracker.record(NODE, 1, update(sequence=5, cpu=10.0), 100.0) is True

    # Reconnect: client.py bumps the epoch to 2 for the *same process* (sequence
    # is untouched - "reports, per process, never reset") and replays every
    # un-acked report in the ring, oldest first - including sequence 5, with the
    # *identical* payload, now stamped with the new session's epoch by the
    # controller side (manager.py passes `live.session_epoch`, not the epoch the
    # sample was originally taken under).
    result = tracker.record(NODE, 2, update(sequence=5, cpu=10.0), 105.0)

    # DEFECT: this must be False (it is the same sample, replayed) but the tuple
    # (2, 5) > (1, 5), so the tracker accepts it as new.
    assert result is True, (
        "documenting the defect: a replay after a reconnect is NOT deduped "
        "because the epoch half of the key changes across a reconnect while the "
        "sequence half does not reset - see this test's module docstring"
    )
    assert tracker.recorded == 2, (
        "the same sample was recorded twice: once under epoch 1, once replayed "
        "under epoch 2 with the same sequence number"
    )
    ring = tracker.ring(NODE)
    assert ring is not None
    assert len(ring) == 2, "the exact same report occupies two ring slots"

    # And it corrupts the downsample: the closed bucket sees this one sample
    # twice, so its mean/max are computed over n=2 instead of n=1.
    rows = tracker.closed_rows(T0 + dt.timedelta(minutes=2))
    assert len(rows) == 1
    assert rows[0].samples == 2, (
        "a single real observation was folded into the bucket's mean/max twice"
    )


def test_a_replay_with_a_lower_but_still_bumped_epoch_sequence_pair_is_recognised_true() -> (
    None
):
    """Sanity check that this is really about the epoch changing under a
    replayed *identical* sequence, not a general ordering bug: two genuinely
    different, monotonically increasing (epoch, sequence) pairs from the same
    growing process are correctly accepted as distinct new samples (that part of
    the design is fine) - it is specifically same-sequence-different-epoch that
    breaks.
    """
    tracker = ResourceTracker(live_ring_size=10, bucket_s=60.0)
    assert tracker.record(NODE, 1, update(sequence=5, cpu=10.0), 100.0) is True
    assert tracker.record(NODE, 2, update(sequence=6, cpu=20.0), 105.0) is True
    assert tracker.recorded == 2
    assert tracker.duplicates == 0
