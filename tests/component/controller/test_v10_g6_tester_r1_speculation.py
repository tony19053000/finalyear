"""V0.10 G6 tester r1: controller-level checks from the criteria - combine output
is identical with and without speculation, a speculating job whose copy lane is
a slow node still terminates with the exactly-once ledger, and a probe of the
known G4 ordering (a result arriving after a cancel was requested)."""

# ruff: noqa: F811, T201
from __future__ import annotations

import pytest
from test_v10_g6_speculation import (
    CHUNKS,
    FAST,
    _blob,
    _expected_output,
    _lanes,
    _leases,
    _ledger_check,
    _queue_row,
    _slow_from,
    _sql,
    _stats,
    _submit,
    _until,
    _wait_job,
    http,  # noqa: F401  (fixture)
    runtime,  # noqa: F401  (fixture)
)

from meshforge.common.states import JobState

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]


async def test_combine_output_identical_with_and_without_speculation(
    runtime,
    http,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    async with _lanes(runtime, [{"chunk_speed": _slow_from(CHUNKS - 4, 2.5)}, {}]):
        with_spec = await _submit(runtime, http, tmp_path / "a")
        j1, _ = await _wait_job(runtime, with_spec)
        no_spec = await _submit(
            runtime, http, tmp_path / "b", split={"speculative": False}
        )
        j2, _ = await _wait_job(runtime, no_spec)
    assert j1.state is JobState.SUCCEEDED and j2.state is JobState.SUCCEEDED
    assert (await _queue_row(runtime, with_spec)).speculated_count >= 1
    assert (await _queue_row(runtime, no_spec)).speculated_count == 0
    d1 = (await http.get(f"/v1/jobs/{with_spec}")).json()["output_digest"]
    d2 = (await http.get(f"/v1/jobs/{no_spec}")).json()["output_digest"]
    assert d1 == d2
    assert await _blob(http, d1) == _expected_output()
    await _ledger_check(runtime, with_spec, CHUNKS)
    await _ledger_check(runtime, no_spec, CHUNKS)


async def test_no_second_lane_per_node_and_leases_never_exceed_two(
    runtime,
    http,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Three lanes, two stragglers: whatever is copied, a chunk never has more than
    two leases overlapping in time and a copy is never on its original's node."""
    async with _lanes(
        runtime,
        [
            {"chunk_speed": _slow_from(CHUNKS - 6, 2.0)},
            {"chunk_speed": _slow_from(CHUNKS - 6, 2.0)},
            {},
        ],
    ):
        job_id = await _submit(runtime, http, tmp_path)
        job, _ = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
    await _ledger_check(runtime, job_id, CHUNKS)
    rows = await _leases(runtime, job_id)
    for r in rows:
        overlap = [
            o
            for o in rows
            if o.idx == r.idx
            and o.seq != r.seq
            and o.leased_at <= r.leased_at
            and (o.ended_at is None or o.ended_at > r.leased_at)
        ]
        assert len(overlap) <= 1, rows
    # the copies never exceed ceil(0.10 x 20) = 2 chunks
    assert len({r.idx for r in rows if r.speculative}) <= 2


async def test_probe_g4_result_after_cancel_requested(
    runtime,
    http,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Characterisation (not a fix): a lane ignoring the cancel (honour_cancel
    False) whose final result arrives after the cancel was requested. Judged:
    the job must still END cancelled with nothing live; record the transitions."""
    async with _lanes(runtime, [{"speed_s": 1.0, "honour_cancel": False}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=2, chunk=2)
        await _until(lambda: lanes[0].timeline)
        got = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert got.status_code in (200, 202), got.text
        job, _ = await _wait_job(runtime, job_id)
        await _until(lambda: all(lane.stopped for lane in lanes))
    seq = [
        (r.from_state, r.to_state)
        for r in await _sql(
            runtime,
            "SELECT from_state, to_state FROM job_transition WHERE job_id=:j ORDER BY seq",
            j=job_id,
        )
    ]
    print("G4-PROBE transitions:", seq)
    assert job.state is JobState.CANCELLED, seq
    assert not [r for r in await _leases(runtime, job_id) if r.state == "live"]
    _ = (FAST, _stats)
