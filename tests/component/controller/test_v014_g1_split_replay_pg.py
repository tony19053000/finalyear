"""V0.14 G1 (D292): a **split** job's decision replays through the real
controller - PostgreSQL, the dispatcher task, gRPC sessions and fake agents
speaking the lane protocol (``mf_fake_lanes``, as V0.10 G4's lane tests).

Three lanes, 300 items: the dispatched ``plan_decision`` row is read back from
PostgreSQL and replayed - the same chosen candidate (mode and node set), the
same split plan, the stored candidate JSON byte for byte, the stored digest
and reason key; the row's lanes are the attempts the controller created.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import build_tree
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
HB_S = 0.2


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={"visibility_timeout_s": 5.0, "wait_retry_s": 0.2},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )
    async with running_controller(settings) as rt:
        yield rt


@contextlib.asynccontextmanager
async def _lanes(
    runtime: ControllerRuntime, speeds: Sequence[float]
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for speed in speeds:
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=["exec", "exec-subprocess", "exec-split"],
                    ),
                    max_cpu_jobs=2,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(fake, runtime, speed_s=speed)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _submit(runtime: ControllerRuntime, tmp_path: Path) -> str:
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    async with api_client(runtime, token=token_of(runtime)) as http:
        got: httpx.Response = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": "s",
                    "entrypoint": "python main.py {items_file} {out}",
                    "split": {
                        "over": {"range": {"stop": 300}},
                        "chunk": {"items": 10},
                        "speculative": False,
                    },
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {},
            },
        )
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def test_split_decision_read_back_replays_to_the_same_plan(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.005, 0.02, 0.02]):
        job_id = await _submit(runtime, tmp_path)
        assert runtime.jobs is not None
        for _ in range(2400):
            job = await runtime.jobs.get_job(job_id)
            assert job is not None
            if job.state in (JobState.SUCCEEDED, JobState.FAILED):
                break
            await asyncio.sleep(0.05)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
    rows = await _sql(
        runtime,
        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, candidates, "
        "replay, replay_version, objective FROM plan_decision "
        "WHERE job_id = :j AND outcome = 'dispatched' ORDER BY id",
        j=job_id,
    )
    assert rows, "no dispatched decision"
    row = rows[0]
    assert row.replay_version == 2 and row.objective == "min_time"
    replay = row.replay if isinstance(row.replay, dict) else json.loads(row.replay)
    request, snapshot, now = from_replay(replay)
    assert request.split is not None and request.split.items == 300
    again = plan(request, snapshot, row.policy, now)
    assert again.chosen is not None and again.chosen.node_id == row.chosen_node_id
    assert candidates_json(again) == row.candidates
    assert again.snapshot_digest == row.snapshot_digest
    assert again.reason_key == row.reason_key
    stored_chosen = row.candidates[0]
    assert again.chosen.mode == stored_chosen["mode"] == "split"  # Mode 3 ran
    assert list(again.chosen.node_set) == stored_chosen["node_set"]
    # the plan the replay gives is the one the controller ran: its lanes
    assert again.split is not None
    lanes = await _sql(
        runtime,
        "SELECT node_id FROM attempt WHERE job_id = :j AND role = 'lane'",
        j=job_id,
    )
    assert sorted(r.node_id for r in lanes) == sorted(again.split.node_ids)
