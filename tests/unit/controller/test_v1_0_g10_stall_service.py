"""V1.0 G10 BUG-4 (D451) tester pass, controller half: the stalled-lease marks
of ``SplitService`` (spec: BUG-4 d), with a fake store (pure logic)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from meshforge.controller.split.messages import LaneRef, LeaseRenewIn
from meshforge.controller.split.service import SplitService

pytestmark = pytest.mark.unit

JOB, LANE_A = "job_1", "att_a"


class FakeStore:
    def __init__(self, others: int = 1, renew: tuple[int, int] = (1, 0)) -> None:
        self.others = others
        self.renew_result = renew
        self.other_calls = 0
        self.stall_peaks: Any = None

    async def other_lanes(self, job_id: str, attempt_id: str, node_id: str) -> int:
        self.other_calls += 1
        return self.others

    async def renew(self, **_kw: Any) -> tuple[int, int]:
        return self.renew_result


def _service(store: FakeStore) -> SplitService:
    manager = SimpleNamespace(
        dispatch=SimpleNamespace(register=lambda *_a: None),
        defer_ack=lambda *_a: None,
    )
    return SplitService(
        store=store,  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        grants=None,
        settings=SimpleNamespace(visibility_timeout_s=60.0, wait_retry_s=1.0),  # type: ignore[arg-type]
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        put=lambda _w: True,
    )


def _live(node: str = "node_a") -> Any:
    return SimpleNamespace(node_id=node)


def _renew(
    leases: dict[tuple[int, int], float | None], attempt: str = LANE_A, job: str = JOB
) -> LeaseRenewIn:
    return LeaseRenewIn(
        LaneRef(job, attempt, 1),
        tuple(leases),
        {k: v for k, v in leases.items() if v is not None},
    )


@pytest.mark.asyncio
async def test_a_stalled_lease_is_marked_once_and_logged_once() -> None:
    store = FakeStore(others=1)
    svc = _service(store)
    with structlog.testing.capture_logs() as logs:
        await svc._note_stalls(_live(), _renew({(7, 1): 40.0}))
        await svc._note_stalls(_live(), _renew({(7, 1): 55.0}))  # still stalled
    warned = [e for e in logs if e["event"] == "controller.split.chunk_stalled"]
    assert len(warned) == 1 and warned[0]["action"] == "copy_first"
    assert svc.stats.stalls == 1 and svc.stats.stalls_alone == 0
    assert store.other_calls == 1  # no DB read on the second renewal
    assert svc._stalls[7].stalled_s == 55.0  # the mark follows the lane's report


@pytest.mark.asyncio
async def test_alone_when_no_other_node_has_a_lane() -> None:
    svc = _service(FakeStore(others=0))
    with structlog.testing.capture_logs() as logs:
        await svc._note_stalls(_live(), _renew({(7, 1): 40.0}))
    (warn,) = [e for e in logs if e["event"] == "controller.split.chunk_stalled"]
    assert warn["action"] == "alone"
    assert svc.stats.stalls_alone == 1
    assert 7 in svc._stalls  # still marked: a lane may join later


@pytest.mark.asyncio
async def test_resume_clears_only_its_own_lease_mark() -> None:
    svc = _service(FakeStore())
    await svc._note_stalls(_live(), _renew({(7, 2): 40.0}))
    with structlog.testing.capture_logs() as logs:
        await svc._note_stalls(_live(), _renew({(7, 1): None}))  # an older seq
        assert 7 in svc._stalls and not logs
        await svc._note_stalls(_live(), _renew({(7, 2): None}))
    assert 7 not in svc._stalls
    assert [e["event"] for e in logs] == ["controller.split.chunk_resumed"]


@pytest.mark.asyncio
async def test_peak_stall_is_kept_for_the_chunk_record_even_after_resume() -> None:
    store = FakeStore()
    svc = _service(store)
    await svc._note_stalls(_live(), _renew({(7, 1): 40.0}))
    await svc._note_stalls(_live(), _renew({(7, 1): 90.0}))
    await svc._note_stalls(_live(), _renew({(7, 1): 20.0}))
    await svc._note_stalls(_live(), _renew({(7, 1): None}))
    assert store.stall_peaks[(7, 1)] == 90.0  # the store writes this at commit


@pytest.mark.asyncio
async def test_stalled_for_offers_only_other_nodes_in_the_same_job() -> None:
    svc = _service(FakeStore())
    await svc._note_stalls(_live("node_a"), _renew({(7, 1): 40.0}))
    await svc._note_stalls(_live("node_b"), _renew({(8, 1): 10.0}, "att_b"))
    assert svc._stalled_for(JOB, "node_a") == {8: (1, 10.0)}
    assert svc._stalled_for(JOB, "node_b") == {7: (1, 40.0)}
    assert svc._stalled_for(JOB, "node_c") == {7: (1, 40.0), 8: (1, 10.0)}
    assert svc._stalled_for("job_other", "node_c") is None


@pytest.mark.asyncio
async def test_a_result_or_commit_ends_the_mark() -> None:
    svc = _service(FakeStore())
    await svc._note_stalls(_live(), _renew({(7, 1): 40.0, (8, 1): 40.0}))
    svc._clear_stall(7, 9)  # another lease's result: mark stays
    assert 7 in svc._stalls
    svc._clear_stall(7, 1)
    assert 7 not in svc._stalls
    svc._clear_stall(8, None)  # commit: any seq
    assert not svc._stalls
    svc._clear_stall(99, None)  # unknown: no error


@pytest.mark.asyncio
async def test_forget_lane_drops_its_marks_only_and_is_idempotent() -> None:
    svc = _service(FakeStore())
    await svc._note_stalls(_live("node_a"), _renew({(7, 1): 40.0}))
    await svc._note_stalls(_live("node_b"), _renew({(8, 1): 40.0}, "att_b"))
    svc.forget_lane(LANE_A)
    svc.forget_lane(LANE_A)
    assert list(svc._stalls) == [8]


@pytest.mark.asyncio
async def test_marks_and_peaks_are_bounded() -> None:
    from meshforge.controller.split import service as mod

    svc = _service(FakeStore())
    n = mod._MAX_STALLS + 50
    for i in range(n):
        await svc._note_stalls(_live(), _renew({(i, 1): 5.0}))
    assert len(svc._stalls) == mod._MAX_STALLS
    assert len(svc._stall_peaks) == mod._MAX_STALLS
    assert (n - 1) in svc._stalls and 0 not in svc._stalls  # oldest dropped


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT D451-R1: _renew marks stalls for leases the store REFUSED "
    "(not held by this lane); a node can plant marks on a peer's lease",
)
async def test_a_refused_lease_is_not_marked_stalled() -> None:
    svc = _service(FakeStore(renew=(0, 1)))
    await svc._renew(_live(), _renew({(7, 1): 40.0}))
    assert 7 not in svc._stalls
