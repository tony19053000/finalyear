"""V0.6 G3 (D115, checklist 17): the controller dies between two statements of
one write -> nothing of that write survives (real PostgreSQL).

The "death" is ``pg_terminate_backend`` on the store's own connection at the
chosen point - the server ends the session mid-transaction exactly as when
the controller process is killed. Points:

* dispatch: the attempt row is written, its ``plan_decision`` not yet (D112:
  a decision without its attempt - or an attempt without its decision and
  ``QUEUED -> PREPARING`` - must be impossible);
* an agent's ``DONE``: the attempt is ``DONE``, the job's ``SUCCEEDED`` arrow
  not yet;
* the retry's three arrows are in ``test_v06_g3_retry_store.py``.

After each, a second try (the next tick / the agent's replay) succeeds once.
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

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.jobs import execution_store as store_module
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchDecision,
    ExecutionStore,
    StatusReport,
)
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.store import JobRecord, JobScheduling, JobStoreError
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}
KILL = text("SELECT pg_terminate_backend(pg_backend_pid())")


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
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


async def _queued(store: ExecutionStore) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(),
    )
    return job


def _decide(node: str) -> Any:
    def decide(_job: JobRecord, _fabric: FabricRows) -> DispatchDecision:
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            record=DecisionRecord(
                policy="cost_model",
                snapshot_digest="d" * 64,
                reason_key="k" * 64,
                candidates=[{"node_id": node, "feasible": True}],
                chosen_node_id=node,
            ),
        )

    return decide


async def _tick(store: ExecutionStore, decide: Any) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )


async def test_death_between_the_attempt_and_its_decision_row_leaves_neither(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = await _node(pg_settings)
    job = await _queued(store)
    real = store_module._record_decision
    calls: list[str] = []

    async def die_first(db: Any, *args: Any, **kwargs: Any) -> None:
        calls.append("x")
        if len(calls) == 1:
            await db.flush()  # the attempt row is on the server already
            await db.execute(KILL)
        await real(db, *args, **kwargs)

    monkeypatch.setattr(store_module, "_record_decision", die_first)
    with pytest.raises(JobStoreError):
        await _tick(store, _decide(node))
    rows = await _sql(
        pg_settings,
        "SELECT j.state, j.attempt_epoch, (SELECT count(*) FROM attempt a WHERE "
        "a.job_id = j.id), (SELECT count(*) FROM plan_decision d WHERE d.job_id = "
        "j.id), (SELECT count(*) FROM job_transition t WHERE t.job_id = j.id AND "
        "t.to_state = 'preparing') FROM job j WHERE j.id = :j",
        j=job.id,
    )
    assert [tuple(r) for r in rows] == [("queued", 0, 0, 0, 0)]
    (result,) = await _tick(store, _decide(node))  # the next tick
    assert result.attempt is not None and result.attempt.fencing_token == 1
    rows = await _sql(
        pg_settings,
        "SELECT count(*), bool_and(attempt_id IS NOT NULL) FROM plan_decision "
        "WHERE job_id = :j",
        j=job.id,
    )
    assert tuple(rows[0]) == (1, True)


async def test_death_between_done_and_succeeded_leaves_the_attempt_uploading(
    store: ExecutionStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = await _node(pg_settings)
    await _queued(store)
    (result,) = await _tick(store, _decide(node))
    attempt = result.attempt
    for state in (AttemptState.RUNNING, AttemptState.UPLOADING):
        await store.apply_status(StatusReport(attempt.id, 1, state))
    real = store_module._move_job

    async def die_at_collected(db: Any, row: Any, event: JobEvent, **kw: Any) -> bool:
        if event is JobEvent.COLLECTED:
            await db.flush()  # the attempt's DONE is on the server already
            await db.execute(KILL)
        return await real(db, row, event, **kw)

    monkeypatch.setattr(store_module, "_move_job", die_at_collected)
    with pytest.raises(JobStoreError):
        await store.apply_status(StatusReport(attempt.id, 1, AttemptState.DONE))
    rows = await _sql(
        pg_settings,
        "SELECT a.state, j.state, (SELECT count(*) FROM attempt_phase p WHERE "
        "p.attempt_id = a.id) FROM attempt a JOIN job j ON j.id = a.job_id "
        "WHERE a.id = :a",
        a=attempt.id,
    )
    assert [tuple(r) for r in rows] == [("uploading", "collecting", 0)]
    monkeypatch.setattr(store_module, "_move_job", real)
    replay = await store.apply_status(StatusReport(attempt.id, 1, AttemptState.DONE))
    assert replay.job is not None and replay.job.state is JobState.SUCCEEDED
    phases = await _sql(
        pg_settings,
        "SELECT count(*) FROM attempt_phase WHERE attempt_id = :a",
        a=attempt.id,
    )
    assert phases[0][0] == 5
