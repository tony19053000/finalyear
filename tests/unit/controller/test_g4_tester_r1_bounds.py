"""V0.4 G4 tester round 1: hostile ``AttemptStatus.fencing_token`` bound.

``fencing_token`` is ``uint64`` on the wire (up to 2**64-1) but stored in a
Postgres ``BIGINT`` (signed 64-bit, max 2**63-1, see
``reports.MAX_FENCING_TOKEN``). A malicious or buggy agent can send a value in
``(2**63-1, 2**64-1]`` that is perfectly valid on the wire but would fail (or
worse, silently wrap) at the database. ``parse_status`` must refuse it before
storage - checklist item 9 ("max accepted AND max+1 rejected") for this field
specifically was not covered by any existing test (grep confirmed only
0/negative fencing_token is exercised).
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import MAX_FENCING_TOKEN, parse_status

pytestmark = pytest.mark.unit


def _status(**kw: Any) -> agent_pb2.AttemptStatus:
    base: dict[str, Any] = {
        "job_id": str(new_job_id()),
        "attempt_id": str(new_attempt_id()),
        "fencing_token": 1,
        "state": common_pb2.ATTEMPT_STATE_RUNNING,
    }
    base.update(kw)
    return agent_pb2.AttemptStatus(**base)


def test_max_fencing_token_is_accepted() -> None:
    assert MAX_FENCING_TOKEN == 2**63 - 1
    report = parse_status(_status(fencing_token=MAX_FENCING_TOKEN))
    assert not isinstance(report, str)
    assert report.fencing_token == MAX_FENCING_TOKEN


def test_max_plus_one_fencing_token_is_rejected() -> None:
    """2**63 fits in a wire ``uint64`` but overflows Postgres BIGINT."""
    reason = parse_status(_status(fencing_token=MAX_FENCING_TOKEN + 1))
    assert isinstance(reason, str) and "fencing_token" in reason


def test_the_full_uint64_max_is_also_rejected() -> None:
    reason = parse_status(_status(fencing_token=2**64 - 1))
    assert isinstance(reason, str) and "fencing_token" in reason


def test_mutation_check_widening_the_bound_lets_an_unrepresentable_token_through() -> (
    None
):
    """Guard the implementer did not mutation-check (D62 list only names
    stale-token / D66-acked / OFFLINE-not-SUSPECT). Break the bound the way an
    implementer easily could - reuse the wire type's max instead of BIGINT's -
    and show the two tests above would have caught it."""
    import meshforge.controller.jobs.reports as reports_module

    original = reports_module.MAX_FENCING_TOKEN
    try:
        reports_module.MAX_FENCING_TOKEN = 2**64 - 1  # the wrong (wire) bound
        mutated = parse_status(_status(fencing_token=2**63))
        assert not isinstance(mutated, str), "mutation should have widened the bound"
    finally:
        reports_module.MAX_FENCING_TOKEN = original
    # restored: the real bound rejects it again
    restored = parse_status(_status(fencing_token=2**63))
    assert isinstance(restored, str) and "fencing_token" in restored
