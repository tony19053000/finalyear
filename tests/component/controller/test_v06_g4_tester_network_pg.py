"""V0.6 G4 tester round 1 on real PostgreSQL: concurrent EWMA writers, the
migration with data in it, criterion 4 near its crossover, a hostile link
through a real dispatch tick, and the tick's statement count against node and
fingerprint counts."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from test_v06_g4_criterion4 import (
    MB,
    NOW,
    Manager,
    _digest,
    _dispatch,
    _fabric,
    _job,
    _node,
    _past_run,
    _planner,
    _sql,
    _terms,
)

from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.queue_reads import queued_jobs, read_fabric
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.network.store import NetworkStore, ProbeSample
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def test_concurrent_writers_lose_no_sample(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _node(pg_settings, 1.0), await _node(pg_settings, 1.0)
    engine = create_engine(pg_settings, pool_size=20)
    try:
        net = NetworkStore(engine)
        now = dt.datetime.now(dt.UTC)
        kw: dict[str, Any] = {"alpha": 0.3, "stale_after_s": 3600.0}
        values = [10.0 + i for i in range(40)]
        done = await asyncio.gather(
            *(net.record(ProbeSample(a, b, v, v * 1000), now=now, **kw) for v in values)
        )
    finally:
        await engine.dispose()
    assert all(done)
    ((samples, rtt, bw),) = await _sql(
        pg_settings,
        "SELECT samples, rtt_ms, bw_bytes_s FROM network_link WHERE src_node_id = :a",
        a=a,
    )
    assert samples == 40, "an upsert lost a sample under concurrency"
    assert min(values) <= rtt <= max(values)
    assert min(values) * 1000 <= bw <= max(values) * 1000


async def test_a_sample_for_a_deleted_node_is_dropped_not_raised(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a = await _node(pg_settings, 1.0)
    engine = create_engine(pg_settings)
    try:
        net = NetworkStore(engine)
        ok = await net.record(
            ProbeSample(a, "node_ghost", 1.0, 1e6),
            now=dt.datetime.now(dt.UTC),
            alpha=0.3,
            stale_after_s=3600.0,
        )
    finally:
        await engine.dispose()
    assert ok is False


async def test_down_and_up_with_data_in_the_tables(
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
        env_hash="e" * 64,
        output_bytes=5 * MB,
    )
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.downgrade, cfg, "0013")
    assert await _sql(pg_settings, "SELECT to_regclass('network_link')") == [(None,)]
    # the data the older schema knows survives the downgrade
    assert (await _sql(pg_settings, "SELECT count(*) FROM node"))[0][0] == 2
    assert (await _sql(pg_settings, "SELECT count(*) FROM attempt"))[0][0] >= 1
    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert await _sql(pg_settings, "SELECT count(*) FROM network_link") == [(0,)]
    await asyncio.to_thread(command.check, cfg)
    # and the table works again
    await _sql(
        pg_settings,
        "INSERT INTO network_link VALUES (:r, :l, 1, 1e6, 1, now())",
        r=remote,
        l=local,
    )


async def _crossover(
    pg: DatabaseSettings, store: ExecutionStore, bw: int
) -> tuple[str, str, Any]:
    local, remote = await _fabric(pg, store)
    await _sql(pg, "UPDATE network_link SET bw_bytes_s = :b", b=bw)
    await _past_run(
        pg,
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
    return local, remote, out


# Local 2 s; remote = 4 MB / bw + 0.5 s scaled compute -> crossover at 2.667 MB/s
@pytest.mark.parametrize(
    ("bw", "goes_remote"),
    [(2_600_000, False), (2_700_000, True), (2_000_000, False), (50_000_000, True)],
)
async def test_criterion_4_flips_exactly_at_the_crossover(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    bw: int,
    goes_remote: bool,
) -> None:
    local, remote, out = await _crossover(pg_settings, store, bw)
    assert out["node"] == (remote if goes_remote else local)
    total = {c["node_id"]: c["estimate"]["total_s"] for c in out["decision"].candidates}
    assert (total[remote] < total[local]) is goes_remote


async def test_a_vanishing_probed_bandwidth_does_not_break_the_dispatch_tick(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    """A row the controller itself would have written (``0 < bw``): the tick
    must still place the job, the estimate must stay finite and the stored
    decision must stay valid JSON."""
    local, remote, _first = await _crossover(pg_settings, store, 1)
    await _sql(pg_settings, "UPDATE network_link SET bw_bytes_s = 1e-300")
    await _sql(pg_settings, "DELETE FROM transfer_event")  # no faster fallback
    await _job(
        store,
        digest=_digest(1),
        scheduling=JobScheduling(
            fingerprint="1" * 64,
            input_bytes=400 * MB,
            submitter_node_id=local,
            submitter_seeded=True,
        ),
    )
    again = await _dispatch(store, _planner(Manager([local, remote])))
    assert again["node"] == local
    rt = _terms(again["decision"], remote)["transfer"]
    assert rt["seconds"] < 1e12 or rt["source"] != "measured", rt


async def test_an_offline_holder_gives_no_measured_link(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    """The holder of the project is not connected: its (fresh) probe row must
    not be shown as the measured path."""
    local, remote = await _fabric(pg_settings, store)
    await _past_run(
        pg_settings,
        store,
        node=remote,
        digest=_digest(1),
        fingerprint="1" * 64,
        run_ms=1000,
    )
    await _job(
        store,
        digest=_digest(1),
        scheduling=JobScheduling(fingerprint="1" * 64, input_bytes=4 * MB),
    )
    out = await _dispatch(store, _planner(Manager([remote])))  # local not connected
    assert out["node"] == remote
    transfer = _terms(out["decision"], remote)["transfer"]
    assert transfer["detail"].get("bw_from") != local


async def test_statement_count_is_independent_of_nodes_fingerprints_and_history(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings, store)
    engine = create_engine(pg_settings)
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(str(args[2])[:40])

    event.listen(engine.sync_engine, "before_cursor_execute", count)

    async def tick(n_jobs: int) -> int:
        for i in range(n_jobs):
            await _job(
                store,
                digest=_digest(20 + i),  # a distinct project per job
                scheduling=JobScheduling(
                    fingerprint=f"{i + 1:064x}", input_bytes=1 * MB
                ),
            )
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            jobs = await queued_jobs(db, limit=50, now=NOW, aging_s=600.0)
            statements.clear()
            await read_fabric(
                db,
                jobs,
                history_runs=20,
                keys_of=lambda j: [str(j.bundle_digest)],
            )
            return len(statements)

    try:
        await _past_run(
            pg_settings,
            store,
            node=local,
            digest=_digest(1),
            fingerprint=f"{1:064x}",
            run_ms=2000,
            env_ms=30_000,
            env_hash="f" * 64,
        )
        small = await tick(1)
        # more nodes, a lot more history, many distinct fingerprints
        extra = [await _node(pg_settings, 50.0 + i) for i in range(6)]
        for i in range(60):
            await _past_run(
                pg_settings,
                store,
                node=extra[i % 6],
                digest=_digest(100 + (i % 50)),
                fingerprint=f"{i + 1:064x}",
                run_ms=1000 + i,
                env_ms=30_000,
                env_hash=f"{i:064x}",
                output_bytes=MB,
                controller_transfer=(2 * MB, 1000 + i),
            )
        large = await tick(25)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
        await engine.dispose()
    assert small == large, (small, large)
    assert local != remote
