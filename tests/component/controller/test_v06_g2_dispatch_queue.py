"""V0.6 G2 on real PostgreSQL: the dispatch tick's queue and slot accounting
(D104-D106, D112).

* queue order: effective priority desc, then queue time; aging with an
  injected ``now`` lifts a -10 job to the top level after 20 aging periods;
* no head-of-line blocking: a job that cannot be placed does not stop the jobs
  behind it in the same tick; ``not_before`` is respected;
* slot use is derived from live attempts (CPU vs GPU, pre-V0.6 NULL = CPU) -
  a second store on a new engine (a restarted controller) sees the same;
* decisions: ``dispatched`` written with its attempt; ``waiting`` once per
  distinct reason key; ``failed`` with the job's FAIL arrow;
* a decision row that cannot be written is dropped alone (savepoint): the
  dispatch and the rest of the tick commit (D131, amending D112's atomicity).
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
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchDecision,
    DispatchFailure,
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


async def test_queue_order_priority_then_queue_time(store: ExecutionStore) -> None:
    low = await _queued(store, priority=-3)
    first_zero = await _queued(store, priority=0)
    high = await _queued(store, priority=7)
    second_zero = await _queued(store, priority=0)
    rec = Recorder()
    await _tick(store, rec)
    assert rec.seen == [high.id, first_zero.id, second_zero.id, low.id]


async def test_aging_lifts_an_old_low_priority_job_to_the_front(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    old_low = await _queued(store, priority=-10)
    fresh_high = await _queued(store, priority=10)
    queued = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    await _sql(
        pg_settings,
        "UPDATE job SET queued_at = :at WHERE id IN (:a, :b)",
        at=queued,
        a=old_low.id,
        b=fresh_high.id,
    )
    await _sql(
        pg_settings,
        "UPDATE job SET queued_at = :at WHERE id = :b",
        at=queued + dt.timedelta(seconds=AGING * 19 + 30),
        b=fresh_high.id,
    )
    # 19 periods: -10 + 19 = 9 < 10 -> the high job first
    rec = Recorder()
    await _tick(store, rec, now=queued + dt.timedelta(seconds=AGING * 19 + 59))
    assert rec.seen == [fresh_high.id, old_low.id]
    # 20 periods: -10 + 20 = 10 (capped) ties 10; the longer wait wins
    rec = Recorder()
    await _tick(store, rec, now=queued + dt.timedelta(seconds=AGING * 20))
    assert rec.seen == [old_low.id, fresh_high.id]
    # aging never pushes past the top level: 1000 periods is still 10
    rec = Recorder()
    await _tick(store, rec, now=queued + dt.timedelta(seconds=AGING * 1000))
    assert rec.seen == [old_low.id, fresh_high.id]


async def test_no_head_of_line_blocking_and_not_before(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    blocked = await _queued(store, priority=5)
    later = await _queued(store, priority=0)
    delayed = await _queued(store, priority=9)
    now = dt.datetime.now(dt.UTC)
    await _sql(
        pg_settings,
        "UPDATE job SET not_before = :t WHERE id = :j",
        t=now + dt.timedelta(minutes=5),
        j=delayed.id,
    )

    def answer(job: JobRecord, _f: FabricRows) -> DispatchDecision:
        if job.id == blocked.id:
            return Wait(_record())
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
            record=_record("z" * 64),
        )

    rec = Recorder(answer)
    results = await _tick(store, rec, now=now)
    assert rec.seen == [blocked.id, later.id]  # delayed is not even looked at
    assert [r.job.id for r in results] == [later.id]
    assert results[0].job.state is JobState.PREPARING
    rec = Recorder()
    await _tick(store, rec, now=now + dt.timedelta(minutes=6))
    assert delayed.id in rec.seen


async def test_slot_use_comes_from_live_attempts_and_survives_a_restart(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    jobs = [await _queued(store) for _ in range(3)]
    slots = iter(["gpu", "cpu", "cpu"])

    def answer(_job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot=next(slots),
        )

    rec = Recorder(answer)
    results = await _tick(store, rec)
    assert len(results) == 3
    # inside the tick the counts moved with every attempt
    assert rec.fabrics[0].used_slots(node) == (2, 1)
    # a pre-V0.6 attempt (slot NULL) counts as CPU
    await _sql(
        pg_settings,
        "UPDATE attempt SET slot = NULL WHERE id = :a",
        a=results[2].attempt.id,
    )
    engine = create_engine(pg_settings)  # a "restarted controller"
    try:
        fresh = ExecutionStore(engine)
        await _queued(fresh)
        rec = Recorder()
        await _tick(fresh, rec)
        assert rec.fabrics[0].used_slots(node) == (2, 1)
        assert rec.fabrics[0].nodes[node].cpu_slots == 2
        assert rec.fabrics[0].nodes[node].gpu_slots == 1
    finally:
        await engine.dispose()
    # an ended attempt frees its slot
    await _sql(
        pg_settings,
        "UPDATE attempt SET state = 'failed', failure_type = 'user_error' "
        "WHERE id = :a",
        a=results[0].attempt.id,
    )
    rec = Recorder()
    await _tick(store, rec)
    assert rec.fabrics[0].used_slots(node) == (2, 0)
    assert {j.id for j in jobs} <= {r.job.id for r in results}


async def test_decision_rows_dispatched_waiting_once_and_failed(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node = await _node(pg_settings)
    waiting = await _queued(store)
    for key in ("a" * 64, "a" * 64, "a" * 64, "b" * 64, "b" * 64, "a" * 64):
        await _tick(store, Recorder(lambda _j, _f, k=key: Wait(_record(k))))
    keys = await _sql(
        pg_settings,
        "SELECT outcome, reason_key FROM plan_decision WHERE job_id = :j ORDER BY id",
        j=waiting.id,
    )
    assert keys == [
        ("waiting", "a" * 64),
        ("waiting", "b" * 64),
        ("waiting", "a" * 64),
    ]
    await _sql(
        pg_settings, "UPDATE job SET state = 'cancelled' WHERE id = :j", j=waiting.id
    )

    doomed = await _queued(store)
    await _tick(
        store,
        Recorder(
            lambda _j, _f: DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "nothing", _record("f" * 64)
            )
        ),
    )
    failed = await _sql(
        pg_settings,
        "SELECT d.outcome, d.attempt_id, j.state FROM plan_decision d "
        "JOIN job j ON j.id = d.job_id WHERE d.job_id = :j",
        j=doomed.id,
    )
    assert failed == [("failed", None, "failed")]

    placed = await _queued(store)
    attempt_id = str(new_attempt_id())
    await _tick(
        store,
        Recorder(
            lambda _j, _f: Dispatch(
                attempt_id=attempt_id,
                start_snapshot={},
                node_id=node,
                record=_record("c" * 64),
            )
        ),
    )
    rows = await _sql(
        pg_settings,
        "SELECT d.outcome, d.attempt_no, a.fencing_token, d.policy "
        "FROM plan_decision d JOIN attempt a ON a.id = d.attempt_id "
        "WHERE d.job_id = :j",
        j=placed.id,
    )
    assert rows == [("dispatched", 1, 1, "cost_model")]


async def test_a_decision_that_cannot_be_written_is_dropped_alone(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """D131 (amends D112, G4 fix round 1): the decision row explains a
    placement, it is not a condition of it. A row the database refuses (here a
    CHECK violation) is rolled back to its savepoint and the dispatch commits:
    the attempt exists, the job is PREPARING, there is no ``plan_decision``
    row - and the job behind it in the same tick is dispatched with its row.
    (Before D131 the refused row raised and undid the whole tick.)"""
    node = await _node(pg_settings)
    job = await _queued(store, priority=5)
    behind = await _queued(store)

    def decide(queued: Any, _fabric: Any) -> Dispatch:
        policy = "not_a_policy" if queued.id == job.id else "cost_model"
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            record=_record(policy=policy),
        )

    await _tick(store, Recorder(decide))
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state is JobState.PREPARING
    assert stored.attempt_epoch == 1
    counts = await _sql(
        pg_settings,
        "SELECT (SELECT count(*) FROM attempt WHERE job_id = :j), "
        "(SELECT count(*) FROM plan_decision WHERE job_id = :j)",
        j=job.id,
    )
    assert counts == [(1, 0)]
    after = await _sql(
        pg_settings,
        "SELECT j.state, d.outcome, d.policy FROM job j "
        "JOIN plan_decision d ON d.job_id = j.id WHERE j.id = :j",
        j=behind.id,
    )
    assert after == [("preparing", "dispatched", "cost_model")]


async def test_an_unreadable_queued_row_fails_alone_and_the_batch_goes_on(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """G2 fix D-1 (store side): a queued row that cannot even be read into a
    JobRecord is FAILED(internal_error) and the job behind it still dispatches."""
    node = await _node(pg_settings)
    bad = await _queued(store, priority=5)
    good = await _queued(store)
    await _sql(
        pg_settings,
        "UPDATE job SET placement = CAST('[1, 2]' AS jsonb) WHERE id = :j",
        j=bad.id,
    )
    recorder = Recorder(
        lambda _j, _f: Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot={},
            node_id=node,
            slot="cpu",
        )
    )
    results = await _tick(store, recorder)
    assert recorder.seen == [good.id]
    assert [r.job.id for r in results] == [good.id]
    rows = await _sql(
        pg_settings,
        "SELECT id, state, failure_type FROM job ORDER BY id",
    )
    states = {str(i): (str(s), f) for i, s, f in rows}
    assert states[bad.id] == ("failed", "internal_error")
    assert states[good.id][0] == "preparing"


async def test_gpus_held_by_live_attempts_are_read_back_from_the_database(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    """G2 fix D-2: the GPU indices a live attempt was given are part of the
    tick's fabric rows (a new engine, as after a controller restart)."""
    node = await _node(pg_settings)
    await _queued(store)
    await _queued(store)
    await _tick(
        store,
        Recorder(
            lambda _j, _f: Dispatch(
                attempt_id=str(new_attempt_id()),
                start_snapshot={"gpu_indices": [1]},
                node_id=node,
                slot="gpu",
            )
        ),
    )
    await _queued(store)  # something to tick for
    engine = create_engine(pg_settings)
    try:
        recorder = Recorder()
        await _tick(ExecutionStore(engine), recorder)
    finally:
        await engine.dispose()
    assert recorder.fabrics and recorder.fabrics[0].busy_gpus(node) == {1}
