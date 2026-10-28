"""V0.9 G3 lane A items 0.8 + 0.9 on real PostgreSQL: the run recorder past its
skip cap (D229, D230.2) and with a job deleted mid-sweep (D230.3).

0.8: skipped ids are excluded by id up to ``MAX_SKIPPED_IDS``; past it the
sweep scans from a watermark, so a batch's worth of skipped attempts can never
starve newer ones, and the cap raises **one** alert, not one per sweep.
0.9: an attempt whose job row disappears between the sweep's two reads is
skipped (alert ``job_missing``), and the rest of the batch is still written -
it used to be a ``KeyError`` that failed the whole sweep.

The attempts are inserted directly (a real job row, a real node row): the
recorder is what is under test, not the execution path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import func, select, text, update

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.enrollment import AuditEvent
from meshforge.controller.store.models.job import Attempt
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.recorder import RunRecorder

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

DIGEST = "sha256:" + "ab" * 32
T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
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
                    ended_at=T0 + dt.timedelta(seconds=start_s + i + 1),
                )
            )
    return made


class _Clock:
    """The recorder's monotonic clock, moved by the test (asyncio keeps the real one)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(
        recorder_module, "time", types.SimpleNamespace(monotonic=fake.monotonic)
    )
    return fake


def _poison(monkeypatch: pytest.MonkeyPatch, victims: set[str]) -> None:
    real = recorder_module.build_record

    def poisoned(source: Any) -> dict[str, Any]:
        if source.job_id in victims:
            raise RuntimeError("a builder bug")
        return real(source)

    monkeypatch.setattr(recorder_module, "build_record", poisoned)


async def _recorded(engine: Any) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(RunRecord.attempt_id))
        return {str(r[0]) for r in rows}


async def _alerts(engine: Any) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(AuditEvent.details)
            .where(AuditEvent.action == "telemetry_record_skipped")
            .order_by(AuditEvent.id)
        )
        return [dict(r[0]) for r in rows]


async def test_past_the_cap_newer_attempts_are_never_starved_and_alerted_once(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 2)
    bad = await _attempts(engine, node_id, 7, start_s=0)  # the oldest
    good = await _attempts(engine, node_id, 3, start_s=100)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(10):
        await recorder.sweep()
    # every good attempt recorded although 5 un-excludable skips sit before them
    assert await _recorded(engine) == {a for _, a in good}
    assert recorder.past_cap
    assert recorder.skipped_attempts == {bad[0][1], bad[1][1]}
    alerts = await _alerts(engine)
    assert [a["reason"] for a in alerts] == [
        "unbuildable",
        "unbuildable",
        "skip_cap_reached",
    ]
    assert alerts[2]["cap"] == 2 and alerts[2]["attempt_id"] == bad[2][1]
    # later sweeps and rescans (which retry the skipped ones) alert nothing new
    skipped_before = recorder.skipped
    for _ in range(3):
        clock.now += recorder_module.PAST_CAP_RESCAN_S
        for _ in range(4):
            await recorder.sweep()
    assert len(await _alerts(engine)) == 3
    assert recorder.skipped > skipped_before  # they really were retried
    # a new attempt is still recorded within one sweep of a rescan
    newest = await _attempts(engine, node_id, 1, start_s=200)
    await recorder.sweep()
    await recorder.sweep()
    await recorder.sweep()
    assert newest[0][1] in await _recorded(engine)


async def test_below_the_cap_behaviour_is_unchanged(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = await _attempts(engine, node_id, 2, start_s=0)
    good = await _attempts(engine, node_id, 2, start_s=50)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=10)
    assert await recorder.sweep() == 2
    assert not recorder.past_cap
    assert recorder.skipped_attempts == {a for _, a in bad}
    assert await recorder.sweep() == 0
    assert [a["reason"] for a in await _alerts(engine)] == ["unbuildable"] * 2
    assert await _recorded(engine) == {a for _, a in good}


async def test_past_the_cap_an_older_attempt_that_ends_late_is_recorded_after_a_rescan(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    long_job = await _attempts(engine, node_id, 1, start_s=0, state="running")
    bad = await _attempts(engine, node_id, 3, start_s=10)
    good = await _attempts(engine, node_id, 1, start_s=20)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(4):
        await recorder.sweep()
    assert recorder.past_cap and await _recorded(engine) == {good[0][1]}
    # the oldest attempt (behind the watermark) finishes now
    async with engine.begin() as conn:
        await conn.execute(
            update(Attempt)
            .where(Attempt.id == long_job[0][1])
            .values(state="done", ended_at=func.now())
        )
    await recorder.sweep()  # forward scan from the watermark: not seen yet
    assert long_job[0][1] not in await _recorded(engine)
    clock.now += recorder_module.PAST_CAP_RESCAN_S  # the bounded delay
    await recorder.sweep()  # rescan from the oldest
    assert long_job[0][1] in await _recorded(engine)
    assert [a["reason"] for a in await _alerts(engine)] == [
        "unbuildable",
        "skip_cap_reached",
    ]


async def test_a_job_deleted_mid_sweep_is_skipped_and_the_rest_written(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    victim_job, victim_attempt = made[1]
    real_sources = recorder_module._sources

    async def deleting(db: Any, attempts: Any, campaign_of: Any) -> Any:
        # another connection deletes the job after the sweep read the attempts
        # and before it reads the jobs. Since F-4 (D412) the sweep row-locks
        # its attempts, so a real cascading delete waits for the sweep
        # (test_v10_g4_recorder_flag_pg); the job row is removed without its
        # cascade (replica role skips FK triggers) to reach the defensive
        # job_missing branch all the same.
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
            await conn.execute(text("DELETE FROM job WHERE id = :j"), {"j": victim_job})
        return await real_sources(db, attempts, campaign_of)

    monkeypatch.setattr(recorder_module, "_sources", deleting)
    recorder = RunRecorder(engine, batch=10)
    assert await recorder.sweep() == 2
    assert await _recorded(engine) == {made[0][1], made[2][1]}
    assert recorder.skipped == 1 and victim_attempt in recorder.skipped_attempts
    alerts = await _alerts(engine)
    assert len(alerts) == 1
    assert alerts[0]["attempt_id"] == victim_attempt
    assert alerts[0]["job_id"] == victim_job
    assert alerts[0]["error_type"] == "job_missing"
    monkeypatch.setattr(recorder_module, "_sources", real_sources)
    assert await recorder.sweep() == 0  # nothing left, no crash
