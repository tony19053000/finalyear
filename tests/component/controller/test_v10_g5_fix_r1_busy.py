"""V0.10 G5 fix round 1 (reviewer B1, Lead ruling option b): a combine the
node refuses ``resource_busy`` - it is still at its attempt limit because the
job's own lane is draining (ADR-0019 addendum I12) - is not counted against
``retry.max_attempts``, does not steer the combine away from that node, and
is placed again after ``[controller.split] combine_busy_backoff_s``; past
``combine_busy_max`` free refusals they count again (the bound).

Real controller (PostgreSQL, gRPC sessions, HTTPS blob API), fake agents
(``mf_fake_lanes``) that refuse a combine while one of their lanes is live,
like the real agent's ``max_concurrent_attempts`` check."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

sys.path.insert(0, str(Path(__file__).parent))
import test_v10_g5_combine as base

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
HB_S = (
    0.5  # lease 1.5 s: survives the ~0.75 s gen-2 GC pauses of a one-process V0.10 run
)
DRAIN_S = 1.5
BACKOFF_S = 0.2


def _settings(pg_settings: DatabaseSettings, **split: Any) -> Any:
    return g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        admission_timeout_s=1.2,  # default 0.5 s follows the lease; GC pauses reach 0.75 s
        split={
            "visibility_timeout_s": 5.0,
            "wait_retry_s": 0.2,
            "combine_busy_backoff_s": BACKOFF_S,
            **split,
        },
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(_settings(pg_settings)) as rt:
        yield rt


@pytest_asyncio.fixture
async def strict_runtime(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[ControllerRuntime]:
    """Only one free busy refusal per job."""
    async with running_controller(_settings(pg_settings, combine_busy_max=1)) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


@contextlib.asynccontextmanager
async def _nodes(
    runtime: ControllerRuntime, per_node: Sequence[dict[str, Any]]
) -> AsyncIterator[list[FakeLane]]:
    """Fake agents with ONE cpu slot each (max_cpu_jobs = 1)."""
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for extra in per_node:
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=["exec", "exec-subprocess", "exec-split"],
                    ),
                    max_cpu_jobs=1,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(fake, runtime, speed_s=0.002, **extra)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


BUSY = {"busy_while_lanes_live": True, "drain_delay_s": DRAIN_S}


async def test_busy_refusal_while_the_own_lane_drains_is_not_counted(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """1 node, 1 slot, retry.max_attempts = 1: the combine arrives while the
    job's lane is still draining and is refused resource_busy; it is placed
    again after the fixed backoff and the job SUCCEEDS."""
    async with _nodes(runtime, [BUSY]) as (node,):
        job_id = await base._submit(
            runtime,
            http,
            tmp_path,
            submitter=node.agent.node_id,
            retry={"max_attempts": 1, "backoff_s": 0},
        )
        job = await base._wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        combines = [
            r for r in await base._attempts(runtime, job_id) if r.role == "combine"
        ]
        assert node.busy_refusals, "the race was not provoked (no busy refusal)"
        busy = [c for c in combines if c.failure_type == "resource_busy"]
        assert len(busy) == len(node.busy_refusals) >= 1
        assert [c.state for c in busy] == ["failed"] * len(busy)
        # every refusal re-placed on the same (only) node; the last one ran
        assert {c.node_id for c in combines} == {node.agent.node_id}
        assert combines[-1].state == "done"
        assert len(combines) == len(busy) + 1
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["output_digest"] == combines[-1].output_digest
        assert await base._blob(http, detail["output_digest"]) == (
            base._expected_output()
        )


async def test_busy_refusal_does_not_steer_the_combine_off_the_submitter(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A busy refusal is no combine failure: the re-placed combine goes back
    to the submitter's node (a real failure would move it to the other)."""
    async with _nodes(runtime, [BUSY, {}]) as lanes:
        submitter = lanes[0].agent.node_id
        job_id = await base._submit(
            runtime,
            http,
            tmp_path,
            submitter=submitter,
            retry={"max_attempts": 1, "backoff_s": 0},
        )
        job = await base._wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        combines = [
            r for r in await base._attempts(runtime, job_id) if r.role == "combine"
        ]
        if not lanes[0].busy_refusals:
            pytest.skip("the submitter ran no lane of this job: no race to provoke")
        assert {c.node_id for c in combines} == {submitter}
        assert combines[-1].state == "done"
        assert not lanes[1].combines


async def test_busy_refusals_past_the_free_bound_count_again(
    strict_runtime: ControllerRuntime, tmp_path: Path
) -> None:
    """``combine_busy_max = 1``: the second refusal counts against
    ``retry.max_attempts = 1`` - an endless refusal still ends the job."""
    runtime = strict_runtime
    slow = {"busy_while_lanes_live": True, "drain_delay_s": 30.0}
    async with (
        api_client(runtime, token=token_of(runtime)) as http,
        _nodes(runtime, [slow]) as (node,),
    ):
        job_id = await base._submit(
            runtime,
            http,
            tmp_path,
            submitter=node.agent.node_id,
            retry={"max_attempts": 1, "backoff_s": 0},
        )
        job = await base._wait_job(runtime, job_id, within=60.0)
        assert job.state is JobState.FAILED
        assert job.failure_type is FailureType.RESOURCE_BUSY
        combines = [
            r for r in await base._attempts(runtime, job_id) if r.role == "combine"
        ]
        assert [(c.state, c.failure_type) for c in combines] == [
            ("failed", "resource_busy"),
            ("failed", "resource_busy"),
        ]
