"""V1.0 G9-E (D438): a split job is never split on assumed rates - end to end.

A real controller (PostgreSQL, gRPC sessions, the real planner, dispatcher
and chunk records) with three fake agents speaking the lane protocol and the
**default** ``[controller.split]`` settings (``require_measured_rates`` on):

1. a first run of a workload (no history anywhere) runs **whole** - one lane,
   one chunk - and its decision says why: ``blocked: rates_assumed``;
2. that run's chunk record is the workload's history: the next decision
   reads it (``rate_source`` ``history``, never ``assumed``) - for this tiny
   job the measured time still says "don't split", and nothing is blocked;
3. with a recorded rate that makes the work long, the same job splits over
   the nodes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
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
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

GIB = 1024**3
HB_S = 0.2
CAPS = ("exec", "exec-subprocess", "exec-split")
ITEMS = 600


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
    assert settings.controller.split.require_measured_rates  # the shipped default
    async with running_controller(settings) as rt:
        yield rt


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.begin() as conn:
        result = await conn.execute(text(query), params)
        return list(result.all()) if result.returns_rows else []


@contextlib.asynccontextmanager
async def _fabric(runtime: ControllerRuntime) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for _ in range(3):
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=list(CAPS),
                    ),
                    max_cpu_jobs=2,
                )
                fake.start_heartbeats(HB_S / 2)
                # measured: results carry phases (run = 0.01 s) like a real agent
                lane = FakeLane(fake, runtime, speed_s=0.01, measured=True)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


def _tree(runtime: ControllerRuntime, tmp_path: Path) -> str:
    root = tmp_path / "p"
    root.mkdir(exist_ok=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    return str(built.tree.tree_id)


async def _run(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tree_id: str
) -> tuple[str, dict[str, Any]]:
    """Submit the job, wait for it to succeed; (job id, its latest decision)."""
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "hf",
                "entrypoint": "python main.py {items_file} {out}",
                "env": {"base": "python-cpu"},
                "split": {"over": {"range": {"stop": ITEMS}}, "speculative": False},
            },
            "tree_id": tree_id,
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    job_id = str(got.json()["job_id"])
    assert runtime.jobs is not None
    job = None
    for _ in range(2400):
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            break
        await asyncio.sleep(0.05)
    assert job is not None and job.state is JobState.SUCCEEDED, job and (
        job.failure_type,
        job.failure_detail,
    )
    decisions = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()["decisions"]
    chosen = [d for d in decisions if d.get("chosen_node_id")] or decisions
    return job_id, chosen[-1]


def _split_rows(decision: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in decision["candidates"] if c.get("mode") == "split"]


def _single_rows(decision: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in decision["candidates"]
        if c.get("mode") in ("local", "offload") and c.get("feasible")
    ]


async def _lane_nodes(runtime: ControllerRuntime, job_id: str) -> list[str]:
    rows = await _sql(
        runtime,
        "SELECT node_id FROM attempt WHERE job_id = :job AND role = 'lane'",
        job=job_id,
    )
    return sorted(str(r.node_id) for r in rows)


async def test_first_run_whole_then_history_decides(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    tree_id = _tree(runtime, tmp_path)
    async with (
        _fabric(runtime),
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        # 1. nothing recorded: assumed rates only -> runs whole, says why
        first, decision = await _run(runtime, http, tree_id)
        assert len(await _lane_nodes(runtime, first)) == 1
        sets = _split_rows(decision)
        assert sets, decision
        assert all(not s["margin_test"]["passed"] for s in sets)
        assert sets[0]["margin_test"]["blocked"] == "rates_assumed"
        assert {c["rate_source"] for c in _single_rows(decision)} == {"assumed"}
        records = await _sql(
            runtime,
            "SELECT items, run_ms FROM chunk_record WHERE job_id = :job "
            "AND outcome = 'committed'",
            job=first,
        )
        assert [(r.items, r.run_ms) for r in records] == [(ITEMS, 10)]

        # 2. the run is history now: measured, tiny -> still whole, unblocked
        second, decision = await _run(runtime, http, tree_id)
        assert len(await _lane_nodes(runtime, second)) == 1
        sources = {c["rate_source"] for c in _single_rows(decision)}
        assert "assumed" not in sources and "history" in sources, sources
        sets = _split_rows(decision)
        assert all(s["margin_test"].get("blocked") is None for s in sets)
        assert all(not s["margin_test"]["passed"] for s in sets)

        # 3. a recorded rate of 1 item/s (600 s of work): now it splits
        await _sql(
            runtime,
            "UPDATE chunk_record SET run_ms = :ms WHERE outcome = 'committed'",
            ms=ITEMS * 1000,
        )
        third, decision = await _run(runtime, http, tree_id)
        assert len(await _lane_nodes(runtime, third)) == 3
        chosen = next(c for c in decision["candidates"] if c.get("rank") == 1)
        assert chosen["mode"] == "split"
        assert chosen["margin_test"]["passed"] is True
        assert chosen["margin_test"].get("blocked") is None
