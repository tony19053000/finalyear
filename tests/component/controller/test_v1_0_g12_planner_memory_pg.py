"""V1.0 G12 (D459, D460) against real PostgreSQL: ``planner_memory``.

* ``node_chunk_history`` - per (node, runtime kind) the chunk costs over the
  node's latest committed chunks of ANY workload (runtime from the lane
  attempt's ``attempt.runtime``): pooled across fingerprints with each
  workload's own rate, isolated per runtime kind, too few rows -> absent,
  first chunks = no earlier lease in the lane;
* ``previous_modes`` - per fingerprint the rank-1 mode of the latest
  dispatched decision of an unconstrained job (pinned / excluded skipped,
  waiting decisions ignored);
* ``read_fabric`` carries both for a batch with a split job.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planner_memory import node_chunk_history, previous_modes
from meshforge.controller.jobs.queue_reads import read_fabric
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.job import Job
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
MANIFEST = {"name": "g12", "entrypoint": ["python", "main.py"]}


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _sql(engine: AsyncEngine, q: str, **p: Any) -> list[Any]:
    async with engine.begin() as conn:
        r = await conn.execute(text(q), p)
        return list(r.all()) if r.returns_rows else []


async def _node(engine: AsyncEngine) -> str:
    node = str(new_node_id())
    await Registry(engine).upsert_node_on_hello(
        node_id=node,
        hostname="h",
        os="linux",
        platform="x86_64",
        hardware={"capabilities": ["exec", "exec-subprocess", "exec-split"]},
        session_epoch=1,
        session_id=str(new_session_id()),
        state=NodeState.ONLINE,
        hardware_scores=None,
        cpu_slots=4,
        gpu_slots=0,
    )
    return node


_digests = iter(range(1, 10_000))


async def _job(
    engine: AsyncEngine, fingerprint: str, placement: dict[str, Any] | None = None
) -> str:
    job, _ = await ExecutionStore(engine).create_queued_job(
        job_id=str(new_job_id()),
        name="g12",
        manifest=MANIFEST,
        bundle_digest="sha256:" + f"{next(_digests):064x}",
        placement=placement or {"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fingerprint, input_bytes=1000),
    )
    return job.id


_tokens = iter(range(1, 100_000))


async def _attempt(
    engine: AsyncEngine, job_id: str, node: str, runtime: str | None
) -> str:
    attempt = str(new_attempt_id())
    await _sql(
        engine,
        "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, runtime,"
        " role, created_at, updated_at, ended_at) VALUES (:a, :j, :n, :t, 'done',"
        " :r, 'lane', now(), now(), now())",
        a=attempt,
        j=job_id,
        n=node,
        t=next(_tokens),
        r=runtime,
    )
    return attempt


_chunk_no = iter(range(100_000))


async def _lane(
    engine: AsyncEngine,
    job_id: str,
    node: str,
    runtime: str | None,
    *,
    start_s: float,
    chunks: int,
    items: int,
    spi: float,
    fixed_s: float,
    first_extra_s: float = 0.0,
) -> None:
    """A lane: ``chunks`` committed chunks of ``items`` items whose process
    ran ``items x spi`` s; wall = run + ``fixed_s`` (+ the first's extra)."""
    lane = await _attempt(engine, job_id, node, runtime)
    at = start_s
    run_s = items * spi
    for k in range(chunks):
        wall = run_s + fixed_s + (first_extra_s if k == 0 else 0.0)
        leased = T0 + dt.timedelta(seconds=at)
        await _sql(
            engine,
            "INSERT INTO chunk_record (schema_version, job_id, chunk_index,"
            " lease_seq, lane_attempt_id, node_id, first_item, items, speculative,"
            " won, outcome, failure_class, stage_ms, env_ms, run_ms, upload_ms,"
            " leased_at, ended_at) VALUES (3, :j, :i, 1, :lane, :n, 0, :items,"
            " false, true, 'committed', 'ok', 0, 0, :run, 0, :t, :e)",
            j=job_id,
            i=next(_chunk_no),
            lane=lane,
            n=node,
            items=items,
            run=round(run_s * 1000),
            t=leased,
            e=leased + dt.timedelta(seconds=wall),
        )
        at += wall + 0.5


async def _history(engine: AsyncEngine, nodes: list[str], runs: int) -> Any:
    async with AsyncSession(engine) as db:
        return await node_chunk_history(db, nodes, runs)


async def test_node_history_pools_workloads_per_runtime_kind(
    engine: AsyncEngine,
) -> None:
    win, linux = await _node(engine), await _node(engine)
    a = await _job(engine, "a" * 64)
    b = await _job(engine, "b" * 64)
    c = await _job(engine, "c" * 64)
    # Windows venv: two workloads, very different item rates, one node cost
    await _lane(
        engine,
        a,
        win,
        "venv",
        start_s=0,
        chunks=4,
        items=4,
        spi=0.5,
        fixed_s=3.0,
        first_extra_s=12.0,
    )
    await _lane(
        engine,
        b,
        win,
        "venv",
        start_s=100,
        chunks=4,
        items=2,
        spi=8.0,
        fixed_s=3.0,
        first_extra_s=12.0,
    )
    # the same node's subprocess runtime: another cost, never mixed in
    await _lane(
        engine,
        c,
        win,
        "subprocess",
        start_s=300,
        chunks=5,
        items=1,
        spi=2.0,
        fixed_s=30.0,
    )
    # Linux Docker
    await _lane(
        engine,
        a,
        linux,
        "docker",
        start_s=0,
        chunks=6,
        items=4,
        spi=0.4,
        fixed_s=0.2,
    )
    got = await _history(engine, [win, linux], runs=20)
    venv = got[(win, "venv")]
    assert venv.fixed_s == pytest.approx(3.0, abs=1e-3)
    assert venv.fixed_samples == 6  # 2 lanes x 3 steady chunks
    assert venv.first_s == pytest.approx(12.0, abs=1e-3)
    assert venv.first_samples == 2
    assert got[(win, "subprocess")].fixed_s == pytest.approx(30.0, abs=1e-3)
    assert got[(linux, "docker")].fixed_s == pytest.approx(0.2, abs=1e-3)
    assert (linux, "venv") not in got


async def test_node_history_latest_runs_window_and_too_few_rows(
    engine: AsyncEngine,
) -> None:
    node, sparse = await _node(engine), await _node(engine)
    old = await _job(engine, "d" * 64)
    new = await _job(engine, "e" * 64)
    # an old workload with a 50 s fixed cost, then a newer one with 5 s:
    # runs = 3 reads only the latest 3 steady chunks (+ the lanes' firsts)
    await _lane(
        engine, old, node, "venv", start_s=0, chunks=6, items=2, spi=1.0, fixed_s=50.0
    )
    await _lane(
        engine,
        new,
        node,
        "venv",
        start_s=1000,
        chunks=6,
        items=2,
        spi=1.0,
        fixed_s=5.0,
        first_extra_s=10.0,
    )
    got = await _history(engine, [node, sparse], runs=3)
    cost = got[(node, "venv")]
    assert cost.fixed_s == pytest.approx(5.0, abs=1e-3)
    assert cost.fixed_samples == 3
    # first chunks are read beyond the window (the latest ``runs`` lanes'
    # firsts, D456's rule): the newer lane's (residual 15 s) and the older
    # lane's (50 s) -> median 32.5 s - fixed 5 s
    assert cost.first_samples == 2
    assert cost.first_s == pytest.approx(27.5, abs=1e-3)
    # 2 steady chunks only: below the minimum -> no entry
    few = await _job(engine, "f" * 64)
    await _lane(
        engine, few, sparse, "venv", start_s=0, chunks=3, items=2, spi=1.0, fixed_s=4
    )
    assert (sparse, "venv") not in await _history(engine, [node, sparse], runs=20)
    # a chunk whose lane attempt recorded no runtime is skipped
    none = await _job(engine, "0" * 64)
    await _lane(
        engine, none, sparse, None, start_s=0, chunks=8, items=2, spi=1.0, fixed_s=4
    )
    got = await _history(engine, [node, sparse], runs=20)
    assert all(key[0] != sparse for key in got)
    assert await _history(engine, [], runs=20) == {}


async def _decision(
    engine: AsyncEngine,
    job_id: str,
    node: str,
    mode: str,
    *,
    outcome: str = "dispatched",
) -> None:
    attempt = (
        await _attempt(engine, job_id, node, "venv")
        if outcome == "dispatched"
        else None
    )
    candidates = [
        {"node_id": node, "mode": "local", "feasible": True, "rank": 2},
        {"node_id": node, "mode": mode, "feasible": True, "rank": 1},
    ]
    await _sql(
        engine,
        "INSERT INTO plan_decision (job_id, attempt_no, attempt_id, outcome, policy,"
        " chosen_node_id, snapshot_digest, reason_key, candidates) VALUES (:j, 1,"
        " :a, :o, 'cost_model', :n, :d, :d, CAST(:c AS jsonb))",
        j=job_id,
        a=attempt,
        o=outcome,
        n=node if outcome == "dispatched" else None,
        d="0" * 64,
        c=json.dumps(candidates),
    )


async def test_previous_modes_reads_the_latest_unconstrained_dispatch(
    engine: AsyncEngine,
) -> None:
    node = await _node(engine)
    fp, other, never = "1" * 64, "2" * 64, "3" * 64
    first = await _job(engine, fp)
    await _decision(engine, first, node, "split")
    second = await _job(engine, fp)
    await _decision(engine, second, node, "offload")
    # later, but constrained: a pinned job and an --exclude-node job
    pinned = await _job(engine, fp, {"mode": "node", "node_id": node})
    await _decision(engine, pinned, node, "local")
    excluded = await _job(engine, fp, {"mode": "auto", "exclude_nodes": [node]})
    await _decision(engine, excluded, node, "local")
    # later still, but only waiting
    waiting = await _job(engine, fp)
    await _decision(engine, waiting, node, "local", outcome="waiting")
    third = await _job(engine, other)
    await _decision(engine, third, node, "local")
    async with AsyncSession(engine) as db:
        got = await previous_modes(db, [fp, other, never], limit=500)
        assert got == {fp: "offload", other: "local"}
        assert await previous_modes(db, [], limit=500) == {}
        assert await previous_modes(db, [fp], limit=0) == {}


async def test_read_fabric_carries_both_for_a_split_batch(
    engine: AsyncEngine,
) -> None:
    node = await _node(engine)
    fp = "4" * 64
    done = await _job(engine, fp)
    await _lane(
        engine, done, node, "venv", start_s=0, chunks=5, items=2, spi=1.0, fixed_s=6
    )
    await _decision(engine, done, node, "split")
    queued = await _job(engine, fp)
    await _sql(
        engine,
        "UPDATE job SET split = CAST(:s AS jsonb), split_item_count = 10,"
        " split_items_digest = :d WHERE id = :j",
        s=json.dumps({"over": {"range": {"stop": 10}}}),
        d="sha256:" + "9" * 64,
        j=queued,
    )
    async with AsyncSession(engine) as db:
        jobs = list((await db.scalars(select(Job).where(Job.id == queued))).all())
        fabric = await read_fabric(db, jobs, history_runs=20, keys_of=lambda _j: ())
    assert fabric.previous_mode == {fp: "split"}
    assert fabric.node_chunk_costs[(node, "venv")].fixed_s == pytest.approx(
        6.0, abs=1e-3
    )
