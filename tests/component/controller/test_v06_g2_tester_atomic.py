"""V0.6 G2 tester: atomicity and races of the dispatch transaction (D105, D112, D115)."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchDecision,
    ExecutionStore,
    Wait,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
AGING = 600.0
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _node(pg: DatabaseSettings) -> str:
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
            cpu_slots=2,
            gpu_slots=1,
        )
        return node
    finally:
        await engine.dispose()


async def _queued(store: ExecutionStore, priority: int = 0) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(priority=priority),
    )
    return job


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


def _record(key: str = "k" * 64, policy: str = "cost_model") -> DecisionRecord:
    return DecisionRecord(
        policy=policy,
        snapshot_digest="d" * 64,
        reason_key=key,
        candidates=[{"node_id": "n", "feasible": False}],
    )


class Recorder:
    """A decider that records the order it was asked in."""

    def __init__(self, answer: Any = None) -> None:
        self.seen: list[str] = []
        self.fabrics: list[FabricRows] = []
        self.answer = answer

    def __call__(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        self.seen.append(job.id)
        self.fabrics.append(fabric)
        if callable(self.answer):
            return self.answer(job, fabric)  # type: ignore[no-any-return]
        return Wait()


async def _tick(
    store: ExecutionStore, decide: Any, now: dt.datetime | None = None
) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=100,
        max_per_node=8,
        now=now or dt.datetime.now(dt.UTC),
        aging_s=AGING,
    )


async def test_a_failure_between_two_dispatches_leaves_nothing_of_either(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """The decider answers Dispatch for job 1 and then dies on job 2 (a kill
    between statements): the attempt, the arrow and the ``dispatched`` decision of
    job 1 are all rolled back together - never an attempt without its decision
    or a decision without its attempt (D112/D115)."""
    node = await _node(pg_settings)
    first = await _queued(store, priority=5)
    second = await _queued(store)

    def answer(job: JobRecord, _f: FabricRows) -> DispatchDecision:
        if job.id == second.id:
            raise RuntimeError("killed between dispatches")
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
            record=_record("c" * 64),
        )

    with pytest.raises(Exception, match="killed between dispatches"):
        await _tick(store, Recorder(answer))
    counts = await _sql(
        pg_settings,
        "SELECT (SELECT count(*) FROM attempt), (SELECT count(*) FROM plan_decision), "
        "(SELECT count(*) FROM job WHERE state = 'queued')",
    )
    assert counts == [(0, 0, 2)]
    stored = await store.get_job(first.id)
    assert (
        stored is not None
        and stored.state is JobState.QUEUED
        and stored.attempt_epoch == 0
    )


async def test_every_attempt_has_exactly_one_dispatched_decision_after_many_ticks(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    for _ in range(12):
        await _queued(store)

    def answer(_j: JobRecord, fabric: FabricRows) -> DispatchDecision:
        if sum(fabric.used_slots(node)) >= 2:
            return Wait(_record("w" * 64))
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
            record=_record("d" * 64),
        )

    for _ in range(5):
        await _tick(store, Recorder(answer))
    rows = await _sql(
        pg_settings,
        "SELECT (SELECT count(*) FROM attempt), "
        "(SELECT count(*) FROM plan_decision WHERE outcome = 'dispatched'), "
        "(SELECT count(*) FROM plan_decision d WHERE outcome = 'dispatched' AND NOT EXISTS "
        " (SELECT 1 FROM attempt a WHERE a.id = d.attempt_id)), "
        "(SELECT count(*) FROM attempt a WHERE NOT EXISTS "
        " (SELECT 1 FROM plan_decision d WHERE d.attempt_id = a.id))",
    )
    assert rows == [(2, 2, 0, 0)]


async def test_two_dispatchers_ticking_at_once_never_double_book_a_slot(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """Two controller processes overlapping (a restart whose old process is still
    draining) tick concurrently over the same queue. Slot use is derived from
    the database inside the transaction, so the node's single slot must still be
    taken at most once."""
    node = await _node(pg_settings)  # 2 CPU slots
    for _ in range(10):
        await _queued(store)
    engine = create_engine(pg_settings, pool_size=5)
    other = ExecutionStore(engine)

    def answer(_j: JobRecord, fabric: FabricRows) -> DispatchDecision:
        if fabric.used_slots(node)[0] >= 2:
            return Wait()
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
        )

    try:
        for _ in range(3):
            await asyncio.gather(
                *(
                    s.dispatch_planned(
                        Recorder(answer),
                        limit=1,
                        max_per_node=8,
                        now=dt.datetime.now(dt.UTC),
                        aging_s=AGING,
                    )
                    for s in (store, other, store, other)
                )
            )
    finally:
        await engine.dispose()
    live = await _sql(
        pg_settings,
        "SELECT count(*) FROM attempt WHERE node_id = :n AND state = 'assigned'",
        n=node,
    )
    assert live[0][0] <= 2, f"{live[0][0]} live attempts on a 2-slot node"
