# ruff: noqa: S608, S311, RUF100  # SQL from fixed state names; seeded test RNG
"""V1.0 G4 F-4 tester (D412): adversarial tests of the recorder's ``recorded``
flag and ``ix_attempt_unrecorded`` on real PostgreSQL.

Invariant under attack: every terminal attempt gets exactly one ``run_record``,
eventually, and ``recorded`` is true exactly when the record exists - under a
terminal state flipped while the row is locked, an attempt turning terminal
during a sweep, a crash between batches, job deletes, several sweepers and a
concurrently mutating table; and the index predicate always equals the real
terminal-state enum.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import OperationalError

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import ATTEMPT_TERMINAL_STATES, AttemptState
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.job import (
    LIVE_ATTEMPT_SQL,
    UNRECORDED_ATTEMPT_SQL,
    Attempt,
)
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.recorder import (
    RunRecorder,
    TelemetryStoreError,
    work_query,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

DIGEST = "sha256:" + "ab" * 32
T0 = dt.datetime(2026, 10, 28, 9, 0, tzinfo=dt.UTC)
_TERMINAL = sorted(s.value for s in ATTEMPT_TERMINAL_STATES)
_ALL = sorted(s.value for s in AttemptState)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=10)
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
    engine: Any, node_id: str, states: list[str], *, start_s: int = 0
) -> list[tuple[str, str]]:
    """One job + attempt per entry of ``states`` (created one second apart)."""
    store = JobStore(engine)
    made = []
    for _ in states:
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
        for i, ((job_id, attempt_id), state) in enumerate(
            zip(made, states, strict=True)
        ):
            db.add(
                Attempt(
                    id=attempt_id,
                    job_id=job_id,
                    node_id=node_id,
                    fencing_token=1,
                    state=state,
                    created_at=T0 + dt.timedelta(seconds=start_s + i),
                    ended_at=None
                    if state not in _TERMINAL
                    else T0 + dt.timedelta(seconds=start_s + i + 1),
                )
            )
    return made


async def _snapshot(engine: Any) -> tuple[dict[str, bool], dict[str, int]]:
    """({attempt id: recorded}, {attempt id: number of run_records})."""
    async with engine.connect() as conn:
        flags = {
            str(r[0]): bool(r[1])
            for r in await conn.execute(select(Attempt.id, Attempt.recorded))
        }
        counts: dict[str, int] = {}
        for r in await conn.execute(select(RunRecord.attempt_id)):
            counts[str(r[0])] = counts.get(str(r[0]), 0) + 1
    return flags, counts


async def _assert_consistent(engine: Any) -> None:
    """recorded <=> record exists (for attempts that still exist), no dupes."""
    flags, counts = await _snapshot(engine)
    assert all(c == 1 for c in counts.values()), counts
    for attempt_id, flag in flags.items():
        assert flag == (attempt_id in counts), (attempt_id, flag)


async def _drain(recorder: RunRecorder) -> int:
    written = 0
    for _ in range(200):
        written += await recorder.sweep()
        if recorder.last_examined == 0:
            return written
    raise AssertionError("the sweep never settled")


# -- the predicate against the real enum ------------------------------------


async def test_index_and_sweep_predicates_equal_the_real_terminal_enum(
    engine: Any,
) -> None:
    async with engine.connect() as conn:
        indexdef = str(
            (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE indexname ="
                        " 'ix_attempt_unrecorded'"
                    )
                )
            ).scalar_one()
        )
    in_index = sorted(re.findall(r"'([a-z_]+)'", indexdef.split(" WHERE ", 1)[1]))
    assert in_index == _TERMINAL
    assert sorted(re.findall(r"'([a-z_]+)'", UNRECORDED_ATTEMPT_SQL)) == _TERMINAL
    assert sorted(recorder_module._TERMINAL) == _TERMINAL
    # a state added to AttemptState must be classified: live XOR terminal, so
    # the live index and the unrecorded index together cover the whole enum.
    live = sorted(re.findall(r"'([a-z_]+)'", LIVE_ATTEMPT_SQL))
    assert sorted(live + _TERMINAL) == _ALL


async def test_work_query_returns_exactly_the_unrecorded_terminal_states(
    engine: Any, node_id: str
) -> None:
    """Every state x flag combination: only (terminal, not recorded) is work."""
    states = [s for s in _ALL for _ in (0, 1)]
    made = await _attempts(engine, node_id, states)
    async with engine.begin() as conn:  # flag every second attempt of each state
        for i, (_, attempt_id) in enumerate(made):
            if i % 2:
                await conn.execute(
                    text("UPDATE attempt SET recorded = true WHERE id = :a"),
                    {"a": attempt_id},
                )
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        got = list(await db.scalars(work_query(set(), None, 1000)))
    assert sorted(a.state for a in got) == _TERMINAL  # one unrecorded per terminal
    assert all(not a.recorded for a in got)


@pytest.mark.parametrize("state", _TERMINAL)
async def test_each_terminal_state_is_served_by_the_partial_index(
    engine: Any, node_id: str, state: str
) -> None:
    filler = "failed" if state == "done" else "done"
    await _attempts(engine, node_id, [filler] * 40 + [state] * 3 + ["running"] * 40)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE attempt SET recorded = true WHERE id IN "
                "(SELECT id FROM attempt WHERE state = :f)"
            ),
            {"f": filler},
        )
        await conn.execute(text("ANALYZE attempt"))
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            str(r[0])
            for r in await conn.execute(
                text(
                    "EXPLAIN SELECT id FROM attempt WHERE NOT recorded AND state ="
                    f" '{state}' ORDER BY created_at, id LIMIT 10"  # noqa: S608
                )
            )
        )
        assert "ix_attempt_unrecorded" in plan, plan
        rows = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM attempt WHERE NOT recorded AND state ="
                    f" '{state}'"  # noqa: S608
                )
            )
        ).scalar_one()
        assert rows == 3


# -- a terminal row changing under the sweep ---------------------------------


async def test_terminal_to_terminal_flip_while_locked_is_recorded_with_the_new_state(
    engine: Any, node_id: str
) -> None:
    ((job, att),) = await _attempts(engine, node_id, ["done"])
    recorder = RunRecorder(engine)
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM attempt WHERE id = :a FOR UPDATE"), {"a": att}
        )
        assert await asyncio.wait_for(recorder.sweep(), 10) == 0  # skipped, not waited
        await holder.execute(
            text("UPDATE attempt SET state = 'lost' WHERE id = :a"), {"a": att}
        )
        await holder.commit()
    assert await _drain(recorder) == 1
    async with engine.connect() as conn:
        state = (
            await conn.execute(
                text("SELECT attempt_state FROM run_record WHERE attempt_id = :a"),
                {"a": att},
            )
        ).scalar_one()
    assert state == "lost"
    await _assert_consistent(engine)
    assert job


async def test_an_attempt_turning_terminal_during_the_sweep_is_recorded_once_later(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, ["done", "done", "running"])
    flip = made[2][1]
    real = recorder_module._sources

    async def flipping(db: Any, attempts: Any, campaign_of: Any) -> Any:
        out = await real(db, attempts, campaign_of)
        async with engine.begin() as conn:  # commits mid-sweep, in another tx
            await conn.execute(
                text("UPDATE attempt SET state='failed', ended_at=now() WHERE id=:a"),
                {"a": flip},
            )
        return out

    monkeypatch.setattr(recorder_module, "_sources", flipping)
    recorder = RunRecorder(engine)
    assert await recorder.sweep() == 2
    flags, counts = await _snapshot(engine)
    assert flip not in counts and flags[flip] is False  # not in this batch
    monkeypatch.setattr(recorder_module, "_sources", real)
    assert await _drain(recorder) == 1
    await _assert_consistent(engine)
    assert (await _snapshot(engine))[1][flip] == 1


async def test_a_state_flip_mid_sweep_of_a_locked_row_blocks_until_commit(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that wants a row the sweep holds waits; the record is then
    built from what the sweep read, and the later change is not lost: the
    record is immutable (written once), the attempt keeps its new state."""
    ((_, att),) = await _attempts(engine, node_id, ["failed"])
    real = recorder_module._sources
    writer: list[asyncio.Task[None]] = []

    async def change() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt SET state='fenced' WHERE id=:a"), {"a": att}
            )

    async def with_writer(db: Any, attempts: Any, campaign_of: Any) -> Any:
        writer.append(asyncio.create_task(change()))
        await asyncio.sleep(0.3)
        assert not writer[0].done()  # blocked on the sweep's row lock
        return await real(db, attempts, campaign_of)

    monkeypatch.setattr(recorder_module, "_sources", with_writer)
    recorder = RunRecorder(engine)
    assert await recorder.sweep() == 1
    await asyncio.wait_for(writer[0], 10)
    monkeypatch.setattr(recorder_module, "_sources", real)
    assert await _drain(recorder) == 0
    await _assert_consistent(engine)
    flags, counts = await _snapshot(engine)
    assert flags[att] and counts[att] == 1


# -- crashes ------------------------------------------------------------------


async def test_a_crash_between_batches_leaves_flag_and_record_in_step(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, ["done"] * 7)
    real = recorder_module.RunRecorder._insert
    calls = {"n": 0}

    async def dies_on_the_third(self: RunRecorder, db: Any, records: Any) -> int:
        calls["n"] += 1
        count = await real(self, db, records)
        if calls["n"] == 3:
            raise OperationalError("INSERT", {}, Exception("process killed"))
        return count

    monkeypatch.setattr(RunRecorder, "_insert", dies_on_the_third)
    recorder = RunRecorder(engine, batch=2)
    assert await recorder.sweep() == 2
    assert await recorder.sweep() == 2
    with pytest.raises(TelemetryStoreError):
        await recorder.sweep()
    await _assert_consistent(engine)
    assert len((await _snapshot(engine))[1]) == 4
    # a *fresh* recorder (the restarted controller) finishes the job
    monkeypatch.setattr(RunRecorder, "_insert", real)
    assert await _drain(RunRecorder(engine, batch=2)) == 3
    await _assert_consistent(engine)
    assert sorted((await _snapshot(engine))[1]) == sorted(a for _, a in made)


async def test_a_builder_crash_in_the_middle_of_a_batch_rolls_the_whole_batch_back(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _attempts(engine, node_id, ["done"] * 4)
    real = recorder_module.build_record
    seen = {"n": 0}

    def explode_on_third(source: Any) -> dict[str, Any]:
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt  # not an Exception: simulates a hard stop
        return real(source)

    monkeypatch.setattr(recorder_module, "build_record", explode_on_third)
    with pytest.raises(KeyboardInterrupt):
        await RunRecorder(engine, batch=10).sweep()
    flags, counts = await _snapshot(engine)
    assert counts == {} and not any(flags.values())  # nothing half-written
    monkeypatch.setattr(recorder_module, "build_record", real)
    assert await _drain(RunRecorder(engine)) == 4
    await _assert_consistent(engine)


# -- deletes ------------------------------------------------------------------


async def test_deleting_jobs_of_unrecorded_and_recorded_attempts_never_breaks_the_sweep(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, ["done"] * 6)
    recorder = RunRecorder(engine, batch=3)
    assert await recorder.sweep() == 3  # the first three are recorded
    async with engine.begin() as conn:  # delete one recorded, two unrecorded jobs
        for job, _ in (made[0], made[4], made[5]):
            await conn.execute(text("DELETE FROM job WHERE id = :j"), {"j": job})
    assert await _drain(recorder) == 1  # only made[3] was left to record
    flags, counts = await _snapshot(engine)
    assert set(flags) == {made[1][1], made[2][1], made[3][1]}
    assert made[0][1] in counts  # a record outlives its job
    assert made[4][1] not in counts and made[5][1] not in counts
    async with engine.connect() as conn:  # no orphan index entries
        left = (
            await conn.execute(
                text(f"SELECT count(*) FROM attempt WHERE {UNRECORDED_ATTEMPT_SQL}")  # noqa: S608
            )
        ).scalar_one()
    assert left == 0


# -- past the skip cap --------------------------------------------------------


async def test_past_the_cap_a_late_completion_is_recorded_exactly_once_eventually(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    now = {"t": 1000.0}
    made = await _attempts(engine, node_id, ["running"] + ["done"] * 5 + ["done"] * 2)
    bad_jobs = {made[1][0], made[2][0], made[3][0]}
    real = recorder_module.build_record

    def poisoned(source: Any) -> dict[str, Any]:
        if source.job_id in bad_jobs:
            raise RuntimeError("builder bug")
        return real(source)

    monkeypatch.setattr(recorder_module, "build_record", poisoned)
    recorder = RunRecorder(engine, batch=2, clock=lambda: now["t"])
    for _ in range(8):
        await recorder.sweep()
    assert recorder.past_cap
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE attempt SET state='done', ended_at=now() WHERE id=:a"),
            {"a": made[0][1]},
        )
    for _ in range(6):
        now["t"] += recorder_module.PAST_CAP_RESCAN_S
        for _ in range(6):
            await recorder.sweep()
    flags, counts = await _snapshot(engine)
    good = {a for j, a in made if j not in bad_jobs}
    assert set(counts) == good and all(c == 1 for c in counts.values())
    assert {a for a, f in flags.items() if f} == good
    # the poisoned ones stay unrecorded (visible in the index), never flagged
    assert not any(flags[a] for j, a in made if j in bad_jobs)


# -- randomized concurrency --------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_random_mutation_plus_concurrent_sweepers_keep_the_invariant(
    engine: Any, node_id: str, seed: int
) -> None:
    rng = random.Random(seed)
    made = await _attempts(engine, node_id, ["running"] * 60 + ["done"] * 20)
    stop = asyncio.Event()
    sweepers = [RunRecorder(engine, batch=rng.choice([1, 3, 7])) for _ in range(3)]

    async def sweeping(r: RunRecorder) -> None:
        while not stop.is_set():
            await r.sweep()
            await asyncio.sleep(0)

    async def mutate() -> None:
        pending = [a for _, a in made[:60]]
        rng.shuffle(pending)
        for i, attempt_id in enumerate(pending):
            new = rng.choice(["done", "failed", "lost", "fenced", "uploading"])
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE attempt SET state = :s WHERE id = :a"),
                    {"s": new, "a": attempt_id},
                )
            if i % 9 == 0:  # some terminal flips terminal->terminal too
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE attempt SET state='lost' WHERE id=:a AND "
                            "state IN ('done','failed')"
                        ),
                        {"a": attempt_id},
                    )
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(sweeping(r)) for r in sweepers]
    await mutate()
    stop.set()
    await asyncio.gather(*tasks)
    await _drain(RunRecorder(engine, batch=5))
    flags, counts = await _snapshot(engine)
    async with engine.connect() as conn:
        terminal = {
            str(r[0])
            for r in await conn.execute(
                text("SELECT id FROM attempt WHERE state IN :t").bindparams(
                    bindparam("t", _TERMINAL, expanding=True)
                )
            )
        }
    assert set(counts) == terminal
    assert all(c == 1 for c in counts.values())
    assert {a for a, f in flags.items() if f} == terminal
