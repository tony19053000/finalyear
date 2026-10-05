"""V0.6 G4 on real PostgreSQL: roadmap **criterion 4** through the real
dispatch tick (``ExecutionStore.dispatch_planned`` + ``DispatchPlanner`` +
the estimator v0), and the tick's batched estimator reads (D108).

The crafted fabric (rows written as the controller writes them):

* **L** - the submitter's own laptop (Local, project seeded), CPU score 100,
  holds the project (cache index);
* **R** - a remote laptop with CPU score 400 (4x faster), reachable from L's
  peer server over a **slow measured link** (``network_link`` R <- L,
  0.25 MB/s, fresh) - the only way the project can reach it besides the
  controller, whose link to R is slower still (transfer history 0.1 MB/s).

Criterion 4: a tiny job (2 s on L) with a 4 MB project stays **Local**
(R: 16 s transfer + 0.5 s scaled compute); the inverse - a heavy job (400 s on
L) with a 64 KB project - goes to **R**. The stored decision names every term's
source: L's transfer 0 s ``measured``, R's transfer ``measured`` (the probe)
and its compute ``scaled``.

Batched reads: one tick reads history, outputs, env hashes, env holdings, env
build history, the matrix and controller links for **the whole batch** - the
number of SQL statements does not grow with the number of jobs or nodes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import event, text

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import queued_jobs, read_fabric
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

MB = 1_000_000
NOW = dt.datetime.now(dt.UTC)
MANIFEST = {"name": "c4", "entrypoint": ["python", "main.py"]}


@dataclass
class Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0


class Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _node(pg: DatabaseSettings, cpu_score: float) -> str:
    engine = create_engine(pg)
    try:
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
            hardware_scores={
                "metrics": [
                    {"name": "cpu_fp32_gflops_all", "status": "ok", "mean": cpu_score}
                ]
            },
            cpu_slots=4,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


def _digest(n: int) -> str:
    return "sha256:" + f"{n:02x}" * 32


async def _job(
    store: ExecutionStore, *, digest: str, scheduling: JobScheduling
) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="c4",
        manifest=MANIFEST,
        bundle_digest=digest,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=scheduling,
    )
    return job


async def _past_run(
    pg: DatabaseSettings,
    store: ExecutionStore,
    *,
    node: str,
    digest: str,
    fingerprint: str,
    run_ms: int,
    env_ms: int | None = None,
    env_hash: str | None = None,
    output_bytes: int | None = None,
    controller_transfer: tuple[int, int] | None = None,
) -> str:
    """A finished job + DONE attempt with its phases (as the controller
    writes them at the end of an attempt)."""
    job = await _job(
        store, digest=digest, scheduling=JobScheduling(fingerprint=fingerprint)
    )
    attempt = str(new_attempt_id())
    await _sql(pg, "UPDATE job SET state = 'succeeded' WHERE id = :j", j=job.id)
    await _sql(
        pg,
        "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, env_hash, "
        "output_bytes, created_at, updated_at, ended_at) VALUES (:a, :j, :n, 1, "
        "'done', :e, :o, now(), now(), now())",
        a=attempt,
        j=job.id,
        n=node,
        e=env_hash,
        o=output_bytes,
    )
    phases = [("run", run_ms)] + ([("env", env_ms)] if env_ms is not None else [])
    for phase, ms in phases:
        await _sql(
            pg,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms) "
            "VALUES (:a, :p, now(), :ms)",
            a=attempt,
            p=phase,
            ms=ms,
        )
    if controller_transfer is not None:
        size, ms = controller_transfer
        await _sql(
            pg,
            "INSERT INTO transfer_event (attempt_id, job_id, tree_id, source, bytes, "
            "duration_ms, units, failures) VALUES (:a, :j, :t, 'controller', :b, "
            ":ms, 1, 0)",
            a=attempt,
            j=job.id,
            t=digest,
            b=size,
            ms=ms,
        )
    return attempt


async def _fabric(pg: DatabaseSettings, store: ExecutionStore) -> tuple[str, str]:
    local = await _node(pg, 100.0)
    remote = await _node(pg, 400.0)
    # L holds the projects (its cache index); R <- L measured slow and fresh
    for n in (1, 2):
        await _sql(
            pg,
            "INSERT INTO cache_location (node_id, kind, key, size) "
            "VALUES (:n, 'blob', :k, 1)",
            n=local,
            k=_digest(n),
        )
    await _sql(
        pg,
        "INSERT INTO network_link (src_node_id, dst_node_id, rtt_ms, bw_bytes_s, "
        "samples, measured_at) VALUES (:r, :l, 4.0, 250000, 3, now())",
        r=remote,
        l=local,
    )
    # the controller's link to R is slower still (transfer history)
    await _past_run(
        pg,
        store,
        node=remote,
        digest=_digest(9),
        fingerprint="9" * 64,
        run_ms=1,
        controller_transfer=(2 * MB, 20_000),
    )
    return local, remote


def _planner(manager: Manager) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _dispatch(store: ExecutionStore, planner: DispatchPlanner) -> dict[str, Any]:
    results = await store.dispatch_planned(
        planner.decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    (result,) = results
    assert result.attempt is not None
    rows = await store.decisions(result.job.id)
    return {"node": result.attempt.node_id, "decision": rows[-1]}


def _terms(decision: Any, node: str) -> dict[str, Any]:
    (cand,) = [c for c in decision.candidates if c["node_id"] == node]
    return dict(cand["estimate"]["terms"])


async def test_criterion_4_tiny_job_large_project_stays_local(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings, store)
    await _past_run(
        pg_settings,
        store,
        node=local,
        digest=_digest(1),
        fingerprint="1" * 64,
        run_ms=2000,
    )
    await _job(
        store,
        digest=_digest(1),
        scheduling=JobScheduling(
            fingerprint="1" * 64,
            input_bytes=4 * MB,
            submitter_node_id=local,
            submitter_seeded=True,
        ),
    )
    out = await _dispatch(store, _planner(Manager([local, remote])))
    assert out["node"] == local
    lt, rt = _terms(out["decision"], local), _terms(out["decision"], remote)
    assert lt["transfer"]["seconds"] == 0 and lt["transfer"]["source"] == "measured"
    assert lt["compute"]["seconds"] == pytest.approx(2.0)
    assert lt["compute"]["source"] == "history"
    assert rt["transfer"]["source"] == "measured"
    assert rt["transfer"]["detail"]["bw_from"] == local
    assert rt["transfer"]["seconds"] == pytest.approx(16.0)
    assert rt["compute"]["source"] == "scaled"
    assert rt["compute"]["seconds"] == pytest.approx(0.5)
    modes = {c["node_id"]: c["mode"] for c in out["decision"].candidates}
    assert modes == {local: "local", remote: "offload"}


async def test_criterion_4_inverse_heavy_job_small_project_goes_remote(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings, store)
    await _past_run(
        pg_settings,
        store,
        node=local,
        digest=_digest(2),
        fingerprint="2" * 64,
        run_ms=400_000,
    )
    await _job(
        store,
        digest=_digest(2),
        scheduling=JobScheduling(
            fingerprint="2" * 64,
            input_bytes=64_000,
            submitter_node_id=local,
            submitter_seeded=True,
        ),
    )
    out = await _dispatch(store, _planner(Manager([local, remote])))
    assert out["node"] == remote
    rt = _terms(out["decision"], remote)
    assert rt["transfer"]["seconds"] == pytest.approx(0.256)
    assert rt["compute"]["seconds"] == pytest.approx(100.0)


async def test_one_tick_reads_every_term_in_a_fixed_number_of_statements(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings, store)
    env = "e" * 64
    await _past_run(
        pg_settings,
        store,
        node=local,
        digest=_digest(1),
        fingerprint="1" * 64,
        run_ms=3000,
        env_ms=45_000,
        env_hash=env,
        output_bytes=7 * MB,
    )
    await _past_run(
        pg_settings,
        store,
        node=remote,
        digest=_digest(1),
        fingerprint="1" * 64,
        run_ms=1000,
        env_ms=20,  # a cache hit: not a build
        output_bytes=9 * MB,
    )
    await _sql(
        pg_settings,
        "INSERT INTO cache_location (node_id, kind, key, size) "
        "VALUES (:n, 'env', :k, 123456)",
        n=local,
        k=env,
    )

    engine = create_engine(pg_settings)
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2])[:40])

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:

        async def tick(n_jobs: int) -> Any:
            for _ in range(n_jobs):
                await _job(
                    store,
                    digest=_digest(1),
                    scheduling=JobScheduling(fingerprint="1" * 64, input_bytes=4 * MB),
                )
            async with engine.begin() as conn:
                from sqlalchemy.ext.asyncio import AsyncSession

                async with AsyncSession(bind=conn) as db:
                    jobs = await queued_jobs(db, limit=50, now=NOW, aging_s=600.0)
                    statements.clear()
                    fabric = await read_fabric(
                        db,
                        jobs,
                        history_runs=20,
                        keys_of=lambda j: [str(j.bundle_digest)],
                    )
                    return fabric, len(statements)

        fabric, one = await tick(1)
        fabric_many, many = await tick(9)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
        await engine.dispose()
    assert one == many, (one, many)  # batched: independent of the job count
    hist = fabric_many.history["1" * 64]
    assert hist.by_node_s == {local: 3.0, remote: 1.0}
    assert hist.output_bytes == pytest.approx(8 * MB)  # median of 7 and 9
    assert hist.env_hash_by_node == {local: env}
    assert fabric_many.env_holdings == {local: {env: 123456}}
    (build,) = fabric_many.env_builds.values()
    assert build.median_s == pytest.approx(45.0) and build.samples == 1
    assert (remote, local) in fabric_many.links
    assert fabric_many.controller_links[remote].bw_bytes_s == pytest.approx(100_000)
    assert json.dumps(sorted(fabric.links)) == json.dumps(sorted(fabric_many.links))
