"""V0.6 G4 tester round 2 - the D131 savepoint against a real PostgreSQL.

D131 (amends D112): a ``plan_decision`` row is written inside a savepoint. A
row PostgreSQL refuses is dropped alone; the dispatch / the FAIL arrow / the
rest of the tick commit. So an attempt without its decision row is allowed; a
decision row without its attempt must never exist (D115).

The existing tests refuse a ``dispatched`` row with ``Infinity`` or a CHECK
violation. Here the refusal is a NUL character (``jsonb`` and ``text`` both
refuse it) on **every** outcome - ``dispatched``, ``failed``, ``waiting`` -
in one tick next to clean jobs, and the invariants are asserted in SQL, not
through the store's own views.
"""

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

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs import execution_store
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchFailure,
    ExecutionStore,
    Wait,
)
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}
NUL = "bad\x00text"


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
            cpu_slots=64,
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


def _record(*, poisoned: bool, tag: str) -> DecisionRecord:
    return DecisionRecord(
        policy="cost_model",
        snapshot_digest="d" * 64,
        reason_key=f"{tag}-" + "k" * 40,
        candidates=[
            {
                "node_id": "n",
                "feasible": False,
                "reject_text": NUL if poisoned else "ok",
            }
        ],
    )


async def _tick(store: ExecutionStore, decide: Any) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=100,
        max_per_node=64,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )


async def test_refused_rows_on_every_outcome_never_undo_the_tick_or_orphan_a_row(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    jobs = [await _queued(store) for _ in range(12)]
    kind = {j.id: i % 4 for i, j in enumerate(jobs)}
    poisoned = {j.id: (i // 4) % 2 == 0 for i, j in enumerate(jobs)}

    def decide(job: JobRecord, _fabric: Any) -> Any:
        k, bad = kind[job.id], poisoned[job.id]
        if k == 0 or k == 1:  # dispatch
            return Dispatch(
                attempt_id=str(new_attempt_id()),
                start_snapshot={},
                node_id=node,
                slot="cpu",
                record=_record(poisoned=bad, tag="d"),
            )
        if k == 2:  # a FAIL arrow with its decision
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN,
                "nothing_fits",
                _record(poisoned=bad, tag="f"),
            )
        return Wait(_record(poisoned=bad, tag="w"))

    results = await _tick(store, decide)
    dispatched = {j.id for j in jobs if kind[j.id] in (0, 1)}
    failed = {j.id for j in jobs if kind[j.id] == 2}
    waiting = {j.id for j in jobs if kind[j.id] == 3}
    assert {r.job.id for r in results} == dispatched | failed

    attempts = dict(
        await _sql(pg_settings, "SELECT job_id, count(*) FROM attempt GROUP BY job_id")
    )
    assert dict(attempts) == dict.fromkeys(dispatched, 1)
    states = dict(await _sql(pg_settings, "SELECT id, state FROM job"))
    assert {states[j] for j in dispatched} == {"preparing"}
    assert {states[j] for j in failed} == {"failed"}
    assert {states[j] for j in waiting} == {"queued"}

    rows = await _sql(
        pg_settings,
        "SELECT job_id, outcome, attempt_id FROM plan_decision ORDER BY job_id",
    )
    expected_rows = {j.id for j in jobs if not poisoned[j.id]}
    assert {r[0] for r in rows} == expected_rows
    # D115: never a row for a placement that did not happen
    for job_id, outcome, attempt_id in rows:
        if outcome == "dispatched":
            assert job_id in dispatched and attempt_id is not None
        else:
            assert attempt_id is None
    orphans = await _sql(
        pg_settings,
        "SELECT count(*) FROM plan_decision d LEFT JOIN attempt a "
        "ON a.id = d.attempt_id WHERE d.outcome = 'dispatched' AND a.id IS NULL",
    )
    assert orphans == [(0,)]


async def test_a_refused_waiting_row_never_blocks_the_jobs_behind_it(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``waiting`` row of a stuck job is refused (NUL in its text). The
    job stays QUEUED, the job behind it is dispatched in the same tick, and the
    next tick serves what is left. The refused row is attempted once, not
    once per tick (G4 follow-up: the store remembers the refused key)."""
    node = await _node(pg_settings)
    stuck = await _queued(store, priority=9)
    behind = await _queued(store)
    tries: list[str] = []
    real = execution_store._record_decision

    async def counting(db: Any, job_id: str, *args: Any, **kwargs: Any) -> bool:
        if job_id == stuck.id:
            tries.append(job_id)
        return bool(await real(db, job_id, *args, **kwargs))

    monkeypatch.setattr(execution_store, "_record_decision", counting)

    def decide(job: JobRecord, _fabric: Any) -> Any:
        if job.id == stuck.id:
            return Wait(_record(poisoned=True, tag="w"))
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
            record=_record(poisoned=False, tag="d"),
        )

    first = await _tick(store, decide)
    assert [r.job.id for r in first] == [behind.id]
    assert await _tick(store, decide) == []
    assert len(tries) == 1  # once, not again on the next tick
    counts = await _sql(
        pg_settings,
        "SELECT (SELECT count(*) FROM plan_decision WHERE job_id = :s), "
        "(SELECT state FROM job WHERE id = :s)",
        s=stuck.id,
    )
    assert counts == [(0, "queued")]


async def test_a_refused_waiting_row_is_tried_once_until_its_reason_changes(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G4 follow-up: ``fabric.last_reason`` is re-read from the table every
    tick, so a refused ``waiting`` row used to be retried every tick. The
    store remembers the refused key; a new reason is written again."""
    job = await _queued(store)
    calls: list[str] = []
    real = execution_store._record_decision

    async def counting(db: Any, job_id: str, *a: Any, **k: Any) -> bool:
        calls.append(job_id)
        return await real(db, job_id, *a, **k)

    monkeypatch.setattr(execution_store, "_record_decision", counting)
    poisoned = _record(poisoned=True, tag="w")
    for _ in range(3):
        await _tick(store, lambda _j, _f: Wait(poisoned))
    assert calls == [job.id]
    clean = _record(poisoned=False, tag="w2")
    await _tick(store, lambda _j, _f: Wait(clean))
    await _tick(store, lambda _j, _f: Wait(clean))
    assert calls == [job.id, job.id]
    rows = await _sql(pg_settings, "SELECT reason_key FROM plan_decision")
    assert rows == [(clean.reason_key,)]
