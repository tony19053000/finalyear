"""V1.0 G4 F-4 (D412) on real PostgreSQL: the run recorder's ``attempt.recorded``
flag and its partial index ``ix_attempt_unrecorded`` (migration 0037).

Same semantics as the anti-join it replaced - every terminal attempt recorded
exactly once, idempotent on retry and crash - but the work list is read from a
partial index that holds only the unrecorded terminal attempts:

* the flag is set in the record's own transaction: a sweep that fails after
  the INSERT commits neither, the next sweep writes the record once;
* an attempt that turns terminal after newer ones were recorded (a long job -
  what a keyset watermark would skip) is recorded by the next sweep;
* two sweepers on one database take disjoint batches (``FOR NO KEY UPDATE SKIP
  LOCKED``) and every attempt gets exactly one record; a row locked by another
  transaction is skipped, never waited for, and taken by a later sweep;
* a cascading job delete waits for the sweep (its attempts are row-locked);
* an attempt whose record exists but whose flag is not set is only marked,
  never rebuilt; a skipped attempt stays unrecorded;
* the flag update keeps ``updated_at``;
* ``EXPLAIN`` of the recorder's own query uses the partial index.

The attempts are inserted directly (a real job row, a real node row): the
recorder is what is under test, not the execution path.
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
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.job import Attempt
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.recorder import (
    RunRecorder,
    TelemetryStoreError,
    work_query,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

DIGEST = "sha256:" + "ab" * 32
T0 = dt.datetime(2026, 10, 28, 9, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=8)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def node_id(engine: Any) -> str:
    node = str(new_node_id())
    await Registry(engine).upsert_node_on_hello(
        node_id=node,
        hostname="h",
        os="linux",
        platform="x86_64",
        hardware={},
        session_epoch=1,
        session_id=str(new_session_id()),
        state=NodeState.ONLINE,
    )
    return node


async def _attempts(
    engine: Any, node_id: str, n: int, *, start_s: int, state: str = "done"
) -> list[tuple[str, str]]:
    """``n`` jobs with one attempt each, created ``start_s``, ``start_s + 1`` ...
    seconds after T0; ``(job_id, attempt_id)`` in creation order."""
    store = JobStore(engine)
    made = []
    for _ in range(n):
        job_id = str(new_job_id())
        await store.create_job(
            job_id=job_id,
            name="demo",
            manifest={"name": "demo"},
            bundle_digest=DIGEST,
            placement={"mode": "offload", "node_id": node_id},
            request_hash="h" * 64,
            idempotency_key=None,
        )
        made.append((job_id, str(new_attempt_id())))
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        for i, (job_id, attempt_id) in enumerate(made):
            db.add(
                Attempt(
                    id=attempt_id,
                    job_id=job_id,
                    node_id=node_id,
                    fencing_token=1,
                    state=state,
                    created_at=T0 + dt.timedelta(seconds=start_s + i),
                    ended_at=None
                    if state == "running"
                    else T0 + dt.timedelta(seconds=start_s + i + 1),
                )
            )
    return made


async def _records(engine: Any) -> list[str]:
    """Every record's attempt id (a list: a duplicate would show)."""
    async with engine.connect() as conn:
        return [str(r[0]) for r in await conn.execute(select(RunRecord.attempt_id))]


async def _flags(engine: Any) -> dict[str, bool]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(Attempt.id, Attempt.recorded))
        return {str(r[0]): bool(r[1]) for r in rows}


async def _drain(recorder: RunRecorder) -> int:
    written = 0
    while True:
        written += await recorder.sweep()
        if recorder.last_examined == 0:
            return written


async def test_a_sweep_writes_the_record_and_its_flag_together(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, 5, start_s=0)
    live = await _attempts(engine, node_id, 1, start_s=10, state="running")
    assert await RunRecorder(engine).sweep() == 5
    flags = await _flags(engine)
    assert sorted(await _records(engine)) == sorted(a for _, a in made)
    assert all(flags[a] for _, a in made)
    assert flags[live[0][1]] is False  # not terminal: no record, no flag


async def test_a_failure_after_the_insert_commits_neither_and_the_retry_writes_once(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    real = RunRecorder._insert

    async def insert_then_lose_the_connection(
        self: RunRecorder, db: Any, records: Any
    ) -> int:
        count = await real(self, db, records)
        assert count == 3  # the rows are in the transaction ...
        raise OperationalError("UPDATE attempt", {}, Exception("connection lost"))

    monkeypatch.setattr(RunRecorder, "_insert", insert_then_lose_the_connection)
    recorder = RunRecorder(engine)
    with pytest.raises(TelemetryStoreError):
        await recorder.sweep()
    # ... but rolled back with the flag: no record without its flag or v.v.
    assert await _records(engine) == []
    assert not any((await _flags(engine)).values())
    monkeypatch.setattr(RunRecorder, "_insert", real)
    assert await recorder.sweep() == 3
    assert sorted(await _records(engine)) == sorted(a for _, a in made)
    assert await recorder.sweep() == 0 and recorder.last_examined == 0


async def test_a_late_completion_behind_newer_recorded_attempts_is_recorded(
    engine: Any, node_id: str
) -> None:
    """What a keyset watermark would miss: the oldest attempt (a long job)
    turns terminal after every newer one was recorded."""
    (old,) = await _attempts(engine, node_id, 1, start_s=0, state="running")
    newer = await _attempts(engine, node_id, 4, start_s=100)
    recorder = RunRecorder(engine, batch=2)
    assert await _drain(recorder) == 4
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE attempt SET state = 'done', ended_at = now() WHERE id = :a"),
            {"a": old[1]},
        )
    assert await recorder.sweep() == 1
    assert sorted(await _records(engine)) == sorted([old[1], *(a for _, a in newer)])
    assert all((await _flags(engine)).values())


async def test_concurrent_sweepers_record_every_attempt_exactly_once(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, 240, start_s=0)
    sweepers = [RunRecorder(engine, batch=20) for _ in range(4)]

    async def drain(recorder: RunRecorder) -> int:
        written = 0
        for _ in range(100):
            written += await recorder.sweep()
            if recorder.last_examined == 0:
                break
        return written

    totals = await asyncio.gather(*(drain(r) for r in sweepers))
    records = await _records(engine)
    assert sum(totals) == 240  # each record written by exactly one sweeper
    assert sorted(records) == sorted(a for _, a in made)
    assert len(set(records)) == len(records)
    assert all((await _flags(engine)).values())
    assert sum(1 for t in totals if t) >= 2  # the work really was shared


async def test_a_row_locked_elsewhere_is_skipped_not_waited_for(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    locked = made[0][1]
    recorder = RunRecorder(engine)
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM attempt WHERE id = :a FOR UPDATE"), {"a": locked}
        )
        # does not wait for the lock: records the two others
        assert await asyncio.wait_for(recorder.sweep(), 10) == 2
        assert locked not in await _records(engine)
        await holder.rollback()
    assert await recorder.sweep() == 1  # a later sweep takes it
    assert sorted(await _records(engine)) == sorted(a for _, a in made)


async def test_a_cascading_job_delete_waits_for_the_sweep(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old mid-sweep delete (D230.3) can no longer remove a locked
    attempt's job under the sweep: the delete waits, every record is written
    from a consistent read, and the delete then completes (records outlive
    their job by design)."""
    made = await _attempts(engine, node_id, 3, start_s=0)
    victim_job = made[1][0]
    real_sources = recorder_module._sources
    deleted = asyncio.Event()
    started: list[asyncio.Task[None]] = []

    async def delete_job() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM job WHERE id = :j"), {"j": victim_job})
        deleted.set()

    async def deleting(db: Any, attempts: Any, campaign_of: Any) -> Any:
        started.append(asyncio.create_task(delete_job()))
        await asyncio.sleep(0.5)
        assert not deleted.is_set()  # blocked on the sweep's row locks
        return await real_sources(db, attempts, campaign_of)

    monkeypatch.setattr(recorder_module, "_sources", deleting)
    recorder = RunRecorder(engine)
    assert await recorder.sweep() == 3
    await asyncio.wait_for(started[0], 10)
    assert recorder.skipped == 0
    assert sorted(await _records(engine)) == sorted(a for _, a in made)
    assert made[1][1] not in await _flags(engine)  # the attempt is gone


async def test_a_record_without_its_flag_is_marked_never_rebuilt(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    recorder = RunRecorder(engine)
    assert await recorder.sweep() == 3
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE attempt SET recorded = false"))

    def must_not_build(source: Any) -> dict[str, Any]:
        raise AssertionError(f"rebuilt {source.attempt_id}")

    monkeypatch.setattr(recorder_module, "build_record", must_not_build)
    assert await recorder.sweep() == 0
    assert recorder.last_examined == 3 and recorder.skipped == 0
    assert all((await _flags(engine)).values())
    assert sorted(await _records(engine)) == sorted(a for _, a in made)
    assert await recorder.sweep() == 0 and recorder.last_examined == 0


async def test_a_skipped_attempt_stays_unrecorded_and_is_not_retried(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    victim_job, victim = made[1]
    real = recorder_module.build_record

    def poisoned(source: Any) -> dict[str, Any]:
        if source.job_id == victim_job:
            raise RuntimeError("a builder bug")
        return real(source)

    monkeypatch.setattr(recorder_module, "build_record", poisoned)
    recorder = RunRecorder(engine)
    assert await recorder.sweep() == 2
    assert (await _flags(engine))[victim] is False
    assert await recorder.sweep() == 0 and recorder.last_examined == 0
    assert victim in recorder.skipped_attempts


async def test_the_flag_update_keeps_updated_at(engine: Any, node_id: str) -> None:
    await _attempts(engine, node_id, 2, start_s=0)
    async with engine.connect() as conn:
        before = dict(
            (await conn.execute(text("SELECT id, updated_at FROM attempt"))).all()
        )
    await asyncio.sleep(0.05)
    assert await RunRecorder(engine).sweep() == 2
    async with engine.connect() as conn:
        after = dict(
            (await conn.execute(text("SELECT id, updated_at FROM attempt"))).all()
        )
    assert before == after


async def test_explain_of_the_sweep_uses_the_partial_index(
    engine: Any, node_id: str
) -> None:
    """3 000 recorded attempts and a few unrecorded: the recorder's own query
    (with and without a skip list and a watermark) reads the partial index,
    never a sequential scan of the attempt table."""
    await _attempts(engine, node_id, 3, start_s=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO job (id, name, state, manifest, bundle_digest, placement,"
                " request_hash, attempt_epoch) SELECT 'job_' || lpad(g::text, 26,"
                " '0'), 'j', 'succeeded', '{}'::jsonb, 'sha256:' || repeat('ab', 32),"
                " '{}'::jsonb, repeat('h', 64), 1 FROM generate_series(1, 3000) g"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, role,"
                " recorded) SELECT 'att_' || lpad(g::text, 26, '0'), 'job_' ||"
                " lpad(g::text, 26, '0'), :n, 1, 'done', 'run', true"
                " FROM generate_series(1, 3000) g"
            ),
            {"n": node_id},
        )
        await conn.execute(text("ANALYZE attempt"))
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    for query in (
        work_query((), None, 200),
        work_query(("att_x", "att_y"), (T0, "att_0"), 200),
    ):
        sql = str(
            query.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
        )
        async with engine.connect() as conn:
            plan = "\n".join(
                str(r[0]) for r in await conn.execute(text("EXPLAIN " + sql))
            )
        assert "ix_attempt_unrecorded" in plan, plan
        assert "Seq Scan on attempt" not in plan, plan
