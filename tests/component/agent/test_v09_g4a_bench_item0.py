"""V0.9 G4 lane A item 0 on a real controller, real agents and PostgreSQL.

* 0.5 (D239.1): runs at the head of the queue on a node that defers (its owner
  accepts no jobs) do not stall ``submit?count=k``: the call pages past them
  and submits k runs of the other node; the deferring node is tried once;
* 0.6 (D239.2): a crash between the job's commit and the run's link (injected:
  the link write fails) leaves no planned run behind - the next campaign read
  links the job by its ``bench:<run_id>`` key, and no second job is made;
* 0.8 (D239.4): only a benchmark job gets the shadow plan - a user's pinned
  job's dispatched decision has none;
* 0.4 (D233.4): the export over real HTTPS is chunked (no Content-Length),
  arrives in several row groups and is byte-identical to the pure export.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meshforge.controller.bench.store import BenchStoreError
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry import export
from meshforge.controller.telemetry.record import RecordSource, build_record

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

POLICY = {"accept_jobs": True, "allow_unsandboxed": True}


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def engine(pg_settings: Any) -> AsyncIterator[Any]:
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _agent(grpc_target: str, path: Path, **policy: Any) -> ExecAgent:
    overrides = {"policy": {**POLICY, **policy}}
    return await ExecAgent.start(grpc_target, path, agent_overrides=overrides)


def _spec(origin: str, *, strategies: str, reps: int) -> str:
    return (
        "name: item0\n"
        "seed: 5\n"
        "families:\n  - family: cpu.primes\n"
        f"strategies: {strategies}\n"
        f"origin: {origin}\n"
        f"repetitions: {reps}\n"
        "budget: {max_runs: 100}\n"
    )


async def _create(http: httpx.AsyncClient, spec: str) -> dict[str, Any]:
    resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _runs(http: httpx.AsyncClient, campaign_id: str) -> list[dict[str, Any]]:
    resp = await http.get(
        f"/v1/bench/campaigns/{campaign_id}/runs", params={"limit": 1000}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


async def _cancel_all(http: httpx.AsyncClient, campaign_id: str) -> None:
    for run in await _runs(http, campaign_id):
        if run["job_id"]:
            await http.post(f"/v1/jobs/{run['job_id']}/cancel")
    for run in await _runs(http, campaign_id):
        if run["job_id"]:
            await wait_job(http, run["job_id"], timeout_s=60)


# ------------------------------------------------------------------ 0.5


async def test_deferring_head_runs_do_not_stall_the_submit(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    # b's owner allows at most 64 MiB per job: every benchmark run defers there
    # for a reason of the run (policy_ram), so each b run is tried and listed
    b = await _agent(grpc_target, tmp_path / "b", max_ram_bytes=64 * 2**20)
    try:
        created = await _create(
            http, _spec(a.node_id, strategies="[local, mode1]", reps=4)
        )
        campaign_id = created["campaign_id"]
        # put every run on b (mode1) at the head of the queue
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE bench_run SET seq = seq + 1000 "
                    "WHERE campaign_id = :c AND node_id = :a"
                ),
                {"c": campaign_id, "a": a.node_id},
            )
        head = await _runs(http, campaign_id)
        assert {r["node_id"] for r in head[:4]} == {b.node_id}

        body = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 3}
            )
        ).json()
        outcomes = body["outcomes"]
        assert body["submitted"] == 3, body  # k submitted past the stalled head
        deferred = [o for o in outcomes if o["outcome"] == "deferred"]
        assert len(deferred) == 4, outcomes  # per run: every b run tried, listed
        assert [o["outcome"] for o in outcomes[:4]] == ["deferred"] * 4
        assert body["counts"]["planned"] == 5  # 4 on b + 1 on a still planned
        runs = {r["run_id"]: r for r in await _runs(http, campaign_id)}
        for outcome in outcomes:
            if outcome["outcome"] == "submitted":
                assert runs[outcome["run_id"]]["node_id"] == a.node_id

        # b goes offline: a node-wide deferral - tried once, its runs passed over
        await b.stop()
        deadline = asyncio.get_running_loop().time() + 20
        while runtime.manager.get(b.node_id) is not None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
        rest = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 5}
            )
        ).json()
        assert rest["submitted"] == 1 and rest["deferred"] == 1, rest
        assert rest["outcomes"][0]["outcome"] == "deferred"  # b's head run, once
        assert rest["counts"]["planned"] == 4
        await _cancel_all(http, campaign_id)
    finally:
        await b.stop()
        await a.stop()


# ------------------------------------------------------------------ 0.6


async def test_a_crash_between_job_and_link_is_linked_on_the_next_read(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=3))
        campaign_id = created["campaign_id"]
        store = runtime.bench
        assert store is not None

        async def crash(run_id: str, job_id: str, **_kw: object) -> bool:
            raise BenchStoreError("injected crash after the job's commit")

        monkeypatch.setattr(store, "mark_submitted", crash)
        failed = await http.post(
            f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 1}
        )
        assert failed.status_code == 503, failed.text
        monkeypatch.undo()

        first = (await _runs(http, campaign_id))[0]  # this read syncs (links)
        assert first["state"] == "submitted", first
        assert first["job_id"], first
        async with engine.connect() as conn:
            key = (
                await conn.execute(
                    text("SELECT idempotency_key FROM job WHERE id = :j"),
                    {"j": first["job_id"]},
                )
            ).scalar_one()
        assert key == f"bench:{first['run_id']}"
        detail = (await http.get(f"/v1/bench/campaigns/{campaign_id}")).json()
        assert detail["counts"]["submitted"] == 1 and detail["counts"]["planned"] == 2

        again = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 5}
            )
        ).json()
        assert first["run_id"] not in [o["run_id"] for o in again["outcomes"]]
        assert again["submitted"] == 2  # only the other runs
        async with engine.connect() as conn:
            bench_jobs = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM job WHERE idempotency_key LIKE 'bench:%'"
                    )
                )
            ).scalar_one()
        assert bench_jobs == 3  # one job per run, never a second for the linked one
        await _cancel_all(http, campaign_id)
    finally:
        await a.stop()


# ------------------------------------------------------------------ 0.8


async def test_only_benchmark_jobs_get_the_shadow_plan(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        user = tmp_path / "user"
        user.mkdir()
        (user / "main.py").write_text("print('user')\n")
        user_job = await submit(
            http, user, a.node_id, {"name": "user", "entrypoint": ["python", "main.py"]}
        )
        assert (await wait_job(http, user_job, timeout_s=60))["state"] == "succeeded"

        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=3))
        body = (
            await http.post(
                f"/v1/bench/campaigns/{created['campaign_id']}/submit",
                params={"count": 1},
            )
        ).json()
        bench_job = body["outcomes"][0]["job_id"]
        await wait_job(
            http, bench_job, states=frozenset({"running", "succeeded"}), timeout_s=60
        )

        async def dispatched(job_id: str) -> dict[str, Any]:
            got = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()
            found = [d for d in got["decisions"] if d["outcome"] == "dispatched"]
            assert found, got
            return dict(found[-1])

        assert (await dispatched(user_job))["shadow_chosen_node_id"] is None
        assert (await dispatched(bench_job))["shadow_chosen_node_id"] == a.node_id
        await _cancel_all(http, created["campaign_id"])
    finally:
        await a.stop()


# ------------------------------------------------------------------ 0.4


async def test_the_export_streams_several_row_groups_chunked_over_https(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    monkeypatch: pytest.MonkeyPatch,
    engine: Any,
) -> None:
    t0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)
    records = [
        build_record(
            RecordSource(
                attempt_id=f"att_{i:04d}",
                job_id=f"job_{i}",
                attempt_no=1,
                node_id="node_a",
                state="done",
                created_at=t0 + dt.timedelta(seconds=i),
                campaign={
                    "campaign_id": "camp_s",
                    "config_id": f"c{i % 3}",
                    "condition": "none" if i % 2 else "queue:1",
                    "condition_facts": {"fillers_ahead": 1, "i": i},
                },
            )
        )
        for i in range(130)
    ]
    async with engine.begin() as conn:
        await conn.execute(pg_insert(RunRecord).values(records))
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 50)  # 130 rows -> 3 groups

    async with http.stream("GET", "/v1/telemetry/export") as reply:
        assert reply.status_code == 200
        assert "content-length" not in reply.headers
        assert reply.headers.get("transfer-encoding") == "chunked"
        assert reply.headers["x-meshforge-telemetry-rows"] == "130"
        pieces = [chunk async for chunk in reply.aiter_raw()]
    data = b"".join(pieces)
    meta = pq.read_metadata(io.BytesIO(data))
    assert meta.num_rows == 130 and meta.num_row_groups == 3
    assert data == export.parquet_bytes(await export.fetch_records(engine))
    table = pq.read_table(io.BytesIO(data))
    assert set(table.column("condition").to_pylist()) == {"none", "queue:1"}
    async with engine.connect() as conn:
        stored = (
            await conn.execute(select(func.count()).select_from(RunRecord))
        ).scalar_one()
    assert stored == 130
