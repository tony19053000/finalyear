"""V0.10 G5 tester r2: try to break the B1 rule (a combine refused
``resource_busy`` is not counted, not an exclusion, re-placed after a fixed
backoff, bounded by ``combine_busy_max``) with cancel, node loss, flapping
between two nodes and the exact bound.

Real controller (PostgreSQL, gRPC, HTTPS blobs), fake agents (``mf_fake_lanes``).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mf_jobs_harness import api_client, running_controller, token_of

sys.path.insert(0, str(Path(__file__).parent))
import test_v10_g5_combine as base
import test_v10_g5_fix_r1_busy as busy

from meshforge.common.errors import FailureType
from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

RETRY1 = {"max_attempts": 1, "backoff_s": 0}


@contextlib.asynccontextmanager
async def _fabric(
    pg: DatabaseSettings, **split: Any
) -> AsyncIterator[tuple[ControllerRuntime, Any]]:
    async with (
        running_controller(busy._settings(pg, **split)) as rt,
        api_client(rt, token=token_of(rt)) as http,
    ):
        yield rt, http


async def _combines(rt: ControllerRuntime, job_id: str) -> list[Any]:
    return [r for r in await base._attempts(rt, job_id) if r.role == "combine"]


async def _until(cond: Any, within: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + within
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------ busy + cancel


async def test_cancel_during_the_busy_backoff_ends_cancelled_without_a_new_combine(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """The refusal leaves the job COLLECTING with no live attempt, waiting for
    its backoff. A cancel then must end it CANCELLED at once and no combine
    may be placed afterwards (not even once the backoff would have elapsed)."""
    async with (
        _fabric(pg_settings, combine_busy_backoff_s=3.0) as (rt, http),
        busy._nodes(rt, [{"busy_while_lanes_live": True, "drain_delay_s": 0.5}]) as (
            node,
        ),
    ):
        job_id = await base._submit(
            rt, http, tmp_path, submitter=node.agent.node_id, retry=RETRY1
        )
        await _until(lambda: bool(node.busy_refusals))
        got = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert got.status_code in (200, 202), got.text
        job = await base._wait_job(rt, job_id, within=20.0)
        assert job.state is JobState.CANCELLED, (job.failure_type,)
        seen = len(await _combines(rt, job_id))
        await asyncio.sleep(4.0)  # past the backoff and the lane's drain
        rows = await _combines(rt, job_id)
        assert len(rows) == seen == len(node.busy_refusals)
        assert not node.combines
        assert all(r.state != "running" for r in rows)
        assert (await http.get(f"/v1/jobs/{job_id}")).json()["output_digest"] is None


# ------------------------------------------------------------ busy + node lost


async def test_busy_then_the_refused_node_is_lost_then_the_other_node_combines(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """Refused by the submitter, which then dies (its lane is still draining):
    the job must not wedge; the other node runs the combine."""
    async with (
        _fabric(pg_settings, combine_busy_backoff_s=1.0) as (rt, http),
        busy._nodes(
            rt, [{"busy_while_lanes_live": True, "drain_delay_s": 20.0}, {}]
        ) as (victim, other),
    ):
        job_id = await base._submit(
            rt, http, tmp_path, submitter=victim.agent.node_id, retry=RETRY1
        )
        await _until(lambda: bool(victim.busy_refusals))
        await victim.kill()
        job = await base._wait_job(rt, job_id, within=90.0)
        assert job.state is JobState.SUCCEEDED, (
            job.failure_type,
            job.failure_detail,
        )
        rows = await _combines(rt, job_id)
        assert rows[-1].state == "done"
        assert rows[-1].node_id == other.agent.node_id
        assert not victim.combines


async def test_busy_does_not_hide_a_later_lost_combine_from_the_budget(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """busy x N (free) then a REAL combine loss: only the loss is counted.
    max_attempts=2 -> the loss is retried on the other node and succeeds."""
    async with _fabric(pg_settings, combine_busy_backoff_s=0.3) as (rt, http):
        extra = {
            "busy_while_lanes_live": True,
            "drain_delay_s": 1.0,
            "combine_hold": True,
        }
        async with busy._nodes(rt, [extra, {}]) as (victim, other):
            job_id = await base._submit(
                rt,
                http,
                tmp_path,
                submitter=victim.agent.node_id,
                retry={"max_attempts": 2, "backoff_s": 0},
            )
            await _until(lambda: bool(victim.combines and victim.combine_parts), 60.0)
            await victim.kill()
            job = await base._wait_job(rt, job_id, within=90.0)
            assert job.state is JobState.SUCCEEDED, (
                job.failure_type,
                job.failure_detail,
            )
            rows = await _combines(rt, job_id)
            kinds = [(r.failure_type, r.state) for r in rows]
            assert kinds[-1] == (None, "done")
            assert kinds.count(("node_lost", "lost")) == 1
            assert all(
                k
                in (("resource_busy", "failed"), ("node_lost", "lost"), (None, "done"))
                for k in kinds
            ), kinds
            assert rows[-1].node_id == other.agent.node_id


async def test_busy_refusals_do_not_extend_the_budget_for_real_losses(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """The mirror: max_attempts=1, a refusal (free) then a loss (counted) ->
    the job FAILS; the free refusal must not give a second life."""
    async with _fabric(pg_settings, combine_busy_backoff_s=0.3) as (rt, http):
        extra = {
            "busy_while_lanes_live": True,
            "drain_delay_s": 1.0,
            "combine_hold": True,
        }
        async with busy._nodes(rt, [extra, {}]) as (victim, _other):
            job_id = await base._submit(
                rt, http, tmp_path, submitter=victim.agent.node_id, retry=RETRY1
            )
            await _until(lambda: bool(victim.combines and victim.combine_parts), 60.0)
            await victim.kill()
            job = await base._wait_job(rt, job_id, within=90.0)
            assert job.state is JobState.FAILED, job.state
            rows = await _combines(rt, job_id)
            assert [r.state for r in rows].count("lost") == 1
            assert all(r.state != "done" for r in rows)


# ------------------------------------------------------------ flapping


async def test_two_busy_nodes_flapping_still_complete_within_one_attempt(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """Both nodes keep a lane live for a while, so either may refuse. With
    max_attempts=1 the job still SUCCEEDS and every non-final combine attempt
    is a ``resource_busy`` refusal."""
    both = {"busy_while_lanes_live": True, "drain_delay_s": 2.0}
    async with (
        _fabric(pg_settings, combine_busy_backoff_s=0.2) as (rt, http),
        busy._nodes(rt, [both, both]) as (a, b),
    ):
        job_id = await base._submit(rt, http, tmp_path, retry=RETRY1)
        job = await base._wait_job(rt, job_id)
        assert job.state is JobState.SUCCEEDED, (
            job.failure_type,
            job.failure_detail,
        )
        rows = await _combines(rt, job_id)
        assert rows[-1].state == "done"
        assert all(r.failure_type == "resource_busy" for r in rows[:-1])
        assert len(rows) - 1 == len(a.busy_refusals) + len(b.busy_refusals)
        tokens = [r.fencing_token for r in await base._attempts(rt, job_id)]
        assert tokens == sorted(set(tokens))  # distinct, increasing


# ------------------------------------------------------------ bound


async def _endless(
    pg: DatabaseSettings, tmp_path: Path, *, free: int, attempts: int
) -> tuple[Any, list[Any]]:
    async with _fabric(pg, combine_busy_max=free) as (rt, http):
        slow = {"busy_while_lanes_live": True, "drain_delay_s": 60.0}
        async with busy._nodes(rt, [slow]) as (node,):
            job_id = await base._submit(
                rt,
                http,
                tmp_path,
                submitter=node.agent.node_id,
                retry={"max_attempts": attempts, "backoff_s": 0},
            )
            job = await base._wait_job(rt, job_id, within=90.0)
            return job, await _combines(rt, job_id)


@pytest.mark.parametrize(
    ("free", "attempts", "expected"),
    [(0, 1, 1), (0, 3, 3), (2, 1, 3), (2, 3, 5), (1, 2, 3)],
)
async def test_combine_busy_max_boundary_is_exact(
    pg_settings: DatabaseSettings,
    tmp_path: Path,
    free: int,
    attempts: int,
    expected: int,
) -> None:
    """Endless refusal: the first ``free`` refusals are free, the rest count,
    so the job ends FAILED(resource_busy) after exactly free + attempts."""
    job, rows = await _endless(pg_settings, tmp_path, free=free, attempts=attempts)
    assert job.state is JobState.FAILED
    assert job.failure_type is FailureType.RESOURCE_BUSY
    assert len(rows) == expected == free + attempts
