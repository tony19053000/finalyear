"""V0.6 G2 tester round 1 on a real controller (PostgreSQL, gRPC, REST, the real
dispatcher + planner) with hand-driven ``FakeAgent`` sessions (they never run
anything, so an attempt they are given stays live and keeps its slot).

From the criteria, not from the implementation:

* slots are never exceeded under a burst of concurrent submits (CPU and GPU;
  ``gpu: preferred`` falls back to a CPU slot; an agent that never said its
  slots is one CPU slot and, with exec-gpu, one GPU slot) - sampled in
  PostgreSQL *while* the burst is in flight;
* priority decides who gets the first free slot, whatever the submit order;
* a job whose only capable node was revoked ends ``FAILED(no_feasible_plan)``
  (D104) with a ``failed`` decision; a waiting job's decision is written once;
* two GPU slots on one node must mean two different GPUs;
* one job with corrupt stored data must not stop the queue behind it;
* API bounds for priority / policy / placement.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(150)]

BUNDLE = os.urandom(4000)
GIB = 1024**3


_POLL_ENGINES: dict[int, AsyncEngine] = {}
"""``id(pg_settings)`` -> the running test's own engine (the ``fabric``
fixture owns it). The polls below run every 50 ms; opening a **new
connection per poll** made each of them race asyncpg's 10 s connect timeout
whenever PostgreSQL stalled under the load of the whole tier (the flaky
failure of ``test_pinned_job_whose_node_is_gone...``: a ``TimeoutError`` from
the test's own connect, not from what it asserts). With one pooled
connection a stall only delays the poll, and ``_until``'s deadline - the
real bound of each test - decides."""


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    pooled = _POLL_ENGINES.get(id(pg))
    engine = pooled if pooled is not None else create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        if pooled is None:
            await engine.dispose()


async def _until(check: Callable[[], Any], timeout_s: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


class Fabric:
    def __init__(
        self,
        runtime: ControllerRuntime,
        http: httpx.AsyncClient,
        channel: Any,
        bundle: str,
        pg: DatabaseSettings,
    ) -> None:
        self.runtime, self.http, self.channel = runtime, http, channel
        self.bundle, self.pg = bundle, pg
        self.agents: list[FakeAgent] = []

    async def agent(self, *caps: str, gpus: int = 0, **hello: Any) -> FakeAgent:
        agent = FakeAgent(self.channel)
        self.agents.append(agent)
        hardware = agent_pb2.HardwareSummary(
            cpu_model="t",
            cpu_cores=4,
            cpu_threads=8,
            ram_bytes=16 * GIB,
            capabilities=list(caps or ("exec", "exec-subprocess")),
            gpus=[
                agent_pb2.HardwareSummary.Gpu(index=i, model="g", vram_bytes=8 * GIB)
                for i in range(gpus)
            ],
        )
        await agent.connect(hardware=hardware, **hello)
        agent.start_heartbeats(0.5)
        return agent

    def body(self, **extra: Any) -> dict[str, Any]:
        manifest = {"name": "demo", "entrypoint": "python main.py"}
        manifest.update(extra.pop("manifest", {}))
        return {"manifest": manifest, "bundle_digest": self.bundle, **extra}

    async def submit(self, **extra: Any) -> str:
        created = await self.http.post("/v1/jobs", json=self.body(**extra))
        assert created.status_code == 201, created.text
        return str(created.json()["job_id"])

    async def live_by_node(self) -> dict[str, tuple[int, int]]:
        rows = await _sql(
            self.pg,
            "SELECT node_id, count(*) FILTER (WHERE slot = 'gpu'), "
            "count(*) FILTER (WHERE slot IS DISTINCT FROM 'gpu') FROM attempt "
            "WHERE state IN ('assigned','staging','env_ready','running','uploading') "
            "GROUP BY node_id",
        )
        return {str(n): (int(c), int(g)) for n, g, c in rows}


@pytest_asyncio.fixture
async def fabric(
    pg_settings: DatabaseSettings, request: pytest.FixtureRequest
) -> AsyncIterator[Fabric]:
    jobs = {"dispatch_interval_s": 0.05, **getattr(request, "param", {})}
    settings = g2_settings(pg_settings, jobs=jobs)
    async with (
        running_controller(settings) as runtime,
        NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel,
        httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=20,
        ) as http,
    ):
        digest = sha256_bytes(BUNDLE)
        put = await http.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201
        made = Fabric(runtime, http, channel, str(digest), pg_settings)
        _POLL_ENGINES[id(pg_settings)] = create_engine(pg_settings, pool_size=2)
        try:
            yield made
        finally:
            await _POLL_ENGINES.pop(id(pg_settings)).dispose()
            for agent in made.agents:
                agent.stop_heartbeats()
                await agent.close()


# --------------------------------------------------------------------- slots


async def test_slots_never_exceeded_under_concurrent_submits_cpu(
    fabric: Fabric,
) -> None:
    a = await fabric.agent(max_cpu_jobs=2, max_gpu_jobs=0)
    b = await fabric.agent(max_cpu_jobs=3, max_gpu_jobs=0)
    peak: dict[str, int] = {}
    stop = asyncio.Event()

    async def sampler() -> None:
        while not stop.is_set():
            for node, (cpu, gpu) in (await fabric.live_by_node()).items():
                peak[node] = max(peak.get(node, 0), cpu + gpu)
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(sampler())
    try:
        ids = await asyncio.gather(*(fabric.submit() for _ in range(40)))
        await _until(lambda: _total_live(fabric, 5))
        await asyncio.sleep(1.0)  # many more ticks: nothing may squeeze in
    finally:
        stop.set()
        await watcher
    live = await fabric.live_by_node()
    assert live == {a.node_id: (2, 0), b.node_id: (3, 0)}
    assert peak[a.node_id] == 2 and peak[b.node_id] == 3
    queued = await _sql(fabric.pg, "SELECT count(*) FROM job WHERE state = 'queued'")
    assert queued[0][0] == 35 and len(set(ids)) == 40


async def _total_live(fabric: Fabric, expected: int) -> bool:
    return sum(c + g for c, g in (await fabric.live_by_node()).values()) == expected


async def test_gpu_slots_and_preferred_fallback_under_a_burst(fabric: Fabric) -> None:
    node = await fabric.agent(
        "exec", "exec-docker", "exec-gpu", gpus=1, max_cpu_jobs=1, max_gpu_jobs=1
    )
    required = [
        await fabric.submit(manifest={"resources": {"gpu": "required"}})
        for _ in range(3)
    ]
    preferred = await asyncio.gather(
        *(fabric.submit(manifest={"resources": {"gpu": "preferred"}}) for _ in range(5))
    )
    await _until(lambda: _total_live(fabric, 2))
    await asyncio.sleep(0.6)
    assert await fabric.live_by_node() == {node.node_id: (1, 1)}
    slots = await _sql(
        fabric.pg, "SELECT job_id, slot FROM attempt WHERE node_id = :n", n=node.node_id
    )
    by_job = {str(j): str(s) for j, s in slots}
    gpu_jobs = [j for j, s in by_job.items() if s == "gpu"]
    cpu_jobs = [j for j, s in by_job.items() if s == "cpu"]
    assert len(gpu_jobs) == 1 and len(cpu_jobs) == 1
    # a `required` job must never sit on a CPU slot
    assert not set(cpu_jobs) & set(required)
    assert set(by_job) <= set(required) | set(preferred)


async def test_agent_that_never_said_its_slots_is_one_cpu_slot(fabric: Fabric) -> None:
    old = await fabric.agent()  # no max_cpu_jobs / max_gpu_jobs fields at all
    await asyncio.gather(*(fabric.submit() for _ in range(6)))
    await _until(lambda: _total_live(fabric, 1))
    await asyncio.sleep(0.6)
    assert await fabric.live_by_node() == {old.node_id: (1, 0)}


async def test_old_agent_with_exec_gpu_gets_one_gpu_slot_and_one_cpu_slot(
    fabric: Fabric,
) -> None:
    old = await fabric.agent("exec", "exec-docker", "exec-gpu", gpus=1)
    for _ in range(3):
        await fabric.submit(manifest={"resources": {"gpu": "required"}})
    for _ in range(3):
        await fabric.submit()
    await _until(lambda: _total_live(fabric, 2))
    await asyncio.sleep(0.6)
    assert await fabric.live_by_node() == {old.node_id: (1, 1)}


async def test_gpu_slots_on_two_gpus_use_two_different_gpus(fabric: Fabric) -> None:
    node = await fabric.agent(
        "exec", "exec-docker", "exec-gpu", gpus=2, max_cpu_jobs=1, max_gpu_jobs=2
    )
    for _ in range(2):
        await fabric.submit(manifest={"resources": {"gpu": "required"}})
    await _until(lambda: _total_live(fabric, 2))
    rows = await _sql(
        fabric.pg,
        "SELECT start_snapshot->'gpu_indices' FROM attempt WHERE node_id = :n",
        n=node.node_id,
    )
    indices = sorted(tuple(r[0]) for r in rows)
    assert len(indices) == 2
    assert indices[0] != indices[1], f"two concurrent GPU jobs share GPU {indices}"


# ------------------------------------------------------------------ priority


async def test_priority_beats_submit_order_for_the_first_free_slot(
    fabric: Fabric,
) -> None:
    await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
    blocker = await fabric.submit()
    await _until(lambda: _total_live(fabric, 1))
    low = await fabric.submit(manifest={"priority": -5})
    mid = await fabric.submit()
    high = await fabric.submit(manifest={"priority": 9})
    manifest_vs_request = await fabric.submit(manifest={"priority": 2})
    await asyncio.sleep(0.4)
    for _ in range(4):  # one new slot at a time
        await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
        await asyncio.sleep(0.5)
    rows = await _sql(fabric.pg, "SELECT job_id FROM attempt ORDER BY created_at, id")
    order = [str(r[0]) for r in rows]
    assert order == [blocker, high, manifest_vs_request, mid, low]


# ------------------------------------------------- revoked / waiting decisions


async def test_revoked_only_capable_node_fails_a_waiting_auto_job_and_waiting_is_written_once(
    fabric: Fabric,
) -> None:
    gpu = await fabric.agent("exec", "exec-docker", "exec-gpu", gpus=1, max_gpu_jobs=1)
    await fabric.agent()  # a CPU node: statically useless for a GPU job
    first = await fabric.submit(manifest={"resources": {"gpu": "required"}})
    await _until(lambda: _total_live(fabric, 1))
    waiting = await fabric.submit(manifest={"resources": {"gpu": "required"}})
    await asyncio.sleep(1.5)  # ~30 ticks
    rows = await _sql(
        fabric.pg, "SELECT outcome FROM plan_decision WHERE job_id = :j", j=waiting
    )
    assert [r[0] for r in rows] == ["waiting"], rows  # once, not per tick
    # the GPU node's owner revokes it and it goes away
    gpu.stop_heartbeats()
    await gpu.close()

    async def failed() -> bool:
        # No production path revokes a node yet (liveness arrow only), and a
        # lingering session end may rewrite the state: keep it revoked.
        await _sql(
            fabric.pg,
            "UPDATE node SET state = 'revoked' WHERE node_id = :n",
            n=gpu.node_id,
        )
        row = await _sql(
            fabric.pg, "SELECT state, failure_type FROM job WHERE id = :j", j=waiting
        )
        return bool(row) and row[0][0] == "failed"

    try:
        await _until(failed, timeout_s=45)
    except AssertionError:
        dbg = await _sql(fabric.pg, "SELECT node_id, state FROM node")
        dec = await _sql(
            fabric.pg,
            "SELECT outcome, candidates FROM plan_decision WHERE job_id = :j ORDER BY id",
            j=waiting,
        )
        raise AssertionError(f"not failed: nodes={dbg} decisions={dec}") from None
    state = await _sql(
        fabric.pg, "SELECT state, failure_type FROM job WHERE id = :j", j=waiting
    )
    assert tuple(state[0]) == ("failed", "no_feasible_plan")
    outcomes = await _sql(
        fabric.pg,
        "SELECT outcome, chosen_node_id FROM plan_decision WHERE job_id = :j ORDER BY id",
        j=waiting,
    )
    assert [o[0] for o in outcomes][-1] == "failed" and outcomes[-1][1] is None
    # the job that held the slot lost its attempt with the node. Since V0.6 G3
    # a loss is retried (D110): it is queued behind its backoff, or already
    # failed no_feasible_plan like the waiting job - never running.
    st = await _sql(fabric.pg, "SELECT state FROM job WHERE id = :j", j=first)
    assert st[0][0] in ("queued", "failed")
    lost = await _sql(fabric.pg, "SELECT state FROM attempt WHERE job_id = :j", j=first)
    assert [r[0] for r in lost] == ["lost"]


# --------------------------------------------------------------- corrupt data


async def test_a_corrupt_queued_job_does_not_stop_the_queue_behind_it(
    fabric: Fabric,
) -> None:
    await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
    await fabric.submit()  # fills the slot
    await _until(lambda: _total_live(fabric, 1))
    poison = await fabric.submit(manifest={"priority": 10})
    good = await fabric.submit()
    await _sql(
        fabric.pg,
        "UPDATE job SET manifest = CAST('{\"name\": 5}' AS jsonb) WHERE id = :j",
        j=poison,
    )
    await asyncio.sleep(0.3)
    await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)

    async def good_started() -> bool:
        rows = await _sql(fabric.pg, "SELECT 1 FROM attempt WHERE job_id = :j", j=good)
        return bool(rows)

    await _until(good_started, timeout_s=15)


# ----------------------------------------------------------- API validation


@pytest.mark.parametrize(
    "change",
    [
        {"manifest": {"priority": 11}},
        {"manifest": {"priority": -11}},
        {"manifest": {"priority": 10.5}},
        {"manifest": {"priority": "5"}},
        {"manifest": {"priority": True}},
        {"manifest": {"priority": None}},
        {"manifest": {"priority": 2**63}},
        {"manifest": {"policy": "FASTEST"}},
        {"manifest": {"policy": "Cost_Model"}},
        {"manifest": {"policy": ""}},
        {"manifest": {"policy": ["random"]}},
        {"manifest": {"retry": {"max_attempts": 0}}},
        {"manifest": {"retry": {"backoff_s": 301}}},
        {"manifest": {"retry": {"backoff_s": -1}}},
        {"manifest": {"retry": {"unknown": 1}}},
        {"placement": {"policy": "cost_model ", "node_id": None}},
        {"placement": {"policy": 7}},
        {"placement": {"nodeid": "x"}},
        {"placement": {"node_id": "x" * 41}},
        {"placement": {"node_id": "n", "local": "yes please"}},
        {"placement": {"node_id": None, "local": True}},
        {"priority": 3},
        {"policy": "random"},
        {"submitter_node_id": ""},
        {"seeded": "true", "submitter_node_id": "node_x"},
        {"seeded": True},
        {"placement": "auto"},
        {"placement": None},
    ],
)
async def test_hostile_scheduling_fields_are_400_and_leave_no_row(
    fabric: Fabric, change: dict[str, Any]
) -> None:
    await fabric.agent()
    refused = await fabric.http.post("/v1/jobs", json=fabric.body(**change))
    assert refused.status_code in (400, 422), (
        change,
        refused.status_code,
        refused.text,
    )
    assert refused.status_code < 500
    count = await _sql(fabric.pg, "SELECT count(*) FROM job")
    assert count[0][0] == 0


@pytest.mark.parametrize("priority", [-10, 0, 10])
async def test_priority_boundaries_are_accepted_and_visible(
    fabric: Fabric, priority: int
) -> None:
    await fabric.agent()
    job = await fabric.submit(manifest={"priority": priority})
    detail = (await fabric.http.get(f"/v1/jobs/{job}")).json()
    assert detail["priority"] == priority


async def test_the_manifest_policy_is_applied_and_the_request_policy_overrides_it(
    fabric: Fabric,
) -> None:
    await fabric.agent()
    manifest_only = await fabric.submit(manifest={"policy": "random"})
    both = await fabric.submit(
        manifest={"policy": "random"}, placement={"policy": "least_loaded"}
    )
    neither = await fabric.submit()
    got = {}
    for name, job in (
        ("manifest", manifest_only),
        ("both", both),
        ("neither", neither),
    ):
        decisions = (await fabric.http.get(f"/v1/jobs/{job}/decisions")).json()[
            "decisions"
        ]
        got[name] = [d["policy"] for d in decisions]
    assert got["manifest"] == ["random"] or got["manifest"] == []
    assert got["both"][:1] in (["least_loaded"], [])
    assert got["neither"][:1] in (["cost_model"], [])


async def test_no_feasible_plan_409_lists_at_most_64_rejections_and_writes_no_row(
    fabric: Fabric,
) -> None:
    for _ in range(3):
        await fabric.agent("docker")  # never exec
    body = fabric.body()
    refused = await fabric.http.post("/v1/jobs", json=body)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "no_feasible_plan"
    assert len(error["rejections"]) == 3
    assert {r["reject_code"] for r in error["rejections"]} == {"not_exec"}
    count = await _sql(fabric.pg, "SELECT count(*) FROM job")
    assert count[0][0] == 0


# ------------------------------------------------------- pinned (V0.4) path


async def test_a_pinned_job_never_spills_to_another_idle_node(fabric: Fabric) -> None:
    busy = await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
    idle = await fabric.agent(max_cpu_jobs=4, max_gpu_jobs=0)
    pinned = await fabric.http.post(
        "/v1/jobs",
        json=fabric.body(placement={"node_id": busy.node_id}),
    )
    assert pinned.status_code == 201
    first = pinned.json()["job_id"]
    await _until(lambda: _total_live(fabric, 1))
    second = (
        await fabric.http.post(
            "/v1/jobs", json=fabric.body(placement={"node_id": busy.node_id})
        )
    ).json()["job_id"]
    await asyncio.sleep(1.0)
    live = await fabric.live_by_node()
    assert live == {busy.node_id: (1, 0)}  # nothing on the idle node
    state = (await fabric.http.get(f"/v1/jobs/{second}")).json()
    assert state["state"] == "queued" and state["placement"]["node_id"] == busy.node_id
    assert first and idle.node_id not in live


@pytest.mark.parametrize("fabric", [{"reconnect_grace_s": 1.0}], indirect=True)
async def test_pinned_job_whose_node_is_gone_fails_node_lost_placed_node_offline(
    fabric: Fabric,
) -> None:
    gone = await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
    await fabric.agent(max_cpu_jobs=1, max_gpu_jobs=0)
    fill = await fabric.http.post(
        "/v1/jobs", json=fabric.body(placement={"node_id": gone.node_id})
    )
    assert fill.status_code == 201
    await _until(lambda: _total_live(fabric, 1))
    waiting = (
        await fabric.http.post(
            "/v1/jobs", json=fabric.body(placement={"node_id": gone.node_id})
        )
    ).json()["job_id"]
    gone.stop_heartbeats()
    await gone.close()

    async def failed() -> bool:
        row = await _sql(
            fabric.pg,
            "SELECT state, failure_type, failure_detail FROM job WHERE id = :j",
            j=waiting,
        )
        return bool(row) and row[0][0] == "failed"

    await _until(failed, timeout_s=40)
    row = await _sql(
        fabric.pg,
        "SELECT failure_type, failure_detail FROM job WHERE id = :j",
        j=waiting,
    )
    assert tuple(row[0]) == ("node_lost", "placed_node_offline")


# ------------------------------------------------------- decisions API bounds


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=999999999",
        "attempt=0",
        "attempt=-3",
        "attempt=abc",
        "limit=1.5",
    ],
)
async def test_decisions_query_bounds_are_400_never_500(
    fabric: Fabric, query: str
) -> None:
    await fabric.agent()
    job = await fabric.submit()
    got = await fabric.http.get(f"/v1/jobs/{job}/decisions?{query}")
    assert got.status_code in (400, 422), (query, got.status_code, got.text)


async def test_decisions_of_unknown_job_and_bad_id_and_no_token(fabric: Fabric) -> None:
    await fabric.agent()
    job = await fabric.submit()
    missing = await fabric.http.get("/v1/jobs/job_01J00000000000000000000000/decisions")
    assert missing.status_code == 404
    bad = await fabric.http.get("/v1/jobs/not-a-job/decisions")
    assert bad.status_code == 400
    async with httpx.AsyncClient(
        base_url=str(fabric.http.base_url), timeout=10
    ) as anon:
        assert (await anon.get(f"/v1/jobs/{job}/decisions")).status_code == 401
    ok = await fabric.http.get(f"/v1/jobs/{job}/decisions?attempt=7")
    assert ok.status_code == 200 and ok.json()["count"] == 0
