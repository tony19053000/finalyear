"""V0.10 G5 tester r1 (component, fake agents over the real controller):
retry budget and pinning rules of the combine (D283 pin 7)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import test_v10_g5_combine as base

from meshforge.common.errors import FailureType
from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

runtime = base.runtime
http = base.http


async def test_retryable_combine_failure_with_budget_one_fails_the_job(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    flaky = {
        "combine_fail": (FailureType.TRANSFER_ERROR, "connection reset", True),
    }
    async with base._lanes(runtime, [0.002, 0.002], [flaky, flaky]):
        job_id = await base._submit(
            runtime, http, tmp_path, retry={"max_attempts": 1, "backoff_s": 0}
        )
        job = await base._wait_job(runtime, job_id)
        assert job.state is JobState.FAILED
        combines = [
            r for r in await base._attempts(runtime, job_id) if r.role == "combine"
        ]
        assert len(combines) == 1  # the budget is honoured
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["output_digest"] is None


async def test_lost_combine_with_on_lost_false_is_not_rerun(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    import asyncio

    async with base._lanes(
        runtime, [0.002, 0.002], [{"combine_hold": True}, {}]
    ) as lanes:
        victim = lanes[0]
        job_id = await base._submit(
            runtime,
            http,
            tmp_path,
            submitter=victim.agent.node_id,
            retry={"max_attempts": 3, "backoff_s": 0, "on_lost": False},
        )
        for _ in range(400):
            if victim.combines and victim.combine_parts:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the combine never reached the submitter's node")
        await victim.kill()
        job = await base._wait_job(runtime, job_id)
        assert job.state is JobState.FAILED, job.state
        await asyncio.sleep(0.5)
        combines = [
            r for r in await base._attempts(runtime, job_id) if r.role == "combine"
        ]
        assert len(combines) == 1 and combines[0].state == "lost"


async def test_chunk_results_stay_pinned_while_the_combine_is_retried(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Every part blob stays readable after the first combine node was lost
    and until the job ends; the retry reads the very same parts."""
    import asyncio

    async with base._lanes(
        runtime, [0.002, 0.002], [{"combine_hold": True}, {}]
    ) as lanes:
        victim = lanes[0]
        job_id = await base._submit(
            runtime,
            http,
            tmp_path,
            submitter=victim.agent.node_id,
            retry={"max_attempts": 3, "backoff_s": 0},
        )
        for _ in range(400):
            if victim.combines and victim.combine_parts:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the combine never reached the submitter's node")
        first_parts = list(victim.combine_parts[victim.combines[0]])
        await victim.kill()
        job = await base._wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        survivor = lanes[1]
        (second_id,) = survivor.combines
        assert list(survivor.combine_parts[second_id]) == first_parts
