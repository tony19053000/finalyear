"""V1.0 G10 (D451): ``LeaseRenew`` carries ``stalled_s`` per lease; the
controller keeps only finite positive values, clamped (a hostile or broken
agent cannot plant NaN, inf or negatives)."""

from __future__ import annotations

import pytest

from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.split.messages import (
    MAX_STALLED_S,
    LeaseRenewIn,
    parse_lease_renew,
)

pytestmark = pytest.mark.unit


def _renew(*values: float) -> agent_pb2.LeaseRenew:
    return agent_pb2.LeaseRenew(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=3,
        leases=[
            agent_pb2.ChunkLeaseRef(item_id=10 + i, lease_seq=1, stalled_s=v)
            for i, v in enumerate(values)
        ],
    )


def test_only_finite_positive_stalls_are_kept_and_clamped() -> None:
    got = parse_lease_renew(
        _renew(0.0, 42.5, -3.0, float("nan"), float("inf"), 10 * MAX_STALLED_S)
    )
    assert isinstance(got, LeaseRenewIn)
    assert len(got.leases) == 6  # every lease is still renewed
    assert got.stalled == {(11, 1): 42.5, (15, 1): MAX_STALLED_S}


def test_an_old_agent_reports_no_stall() -> None:
    got = parse_lease_renew(_renew(0.0, 0.0))
    assert isinstance(got, LeaseRenewIn) and got.stalled == {}
