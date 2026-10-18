"""V0.9 G3 lane A tester r1 (items 0.8, 0.9, D229, D230.2-3): try to break the
recorder's watermark past the skip cap on real PostgreSQL.

``test_defect_*`` are strict xfails documenting a defect found by the tester.
"""

# ruff: noqa: F811
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from test_v09_g3a_recorder_cap import (  # noqa: F401  (fixtures are imported)
    T0,
    _alerts,
    _attempts,
    _Clock,
    _poison,
    _recorded,
    clock,
    engine,
    node_id,
)

from meshforge.controller.store.models.job import Attempt
from meshforge.controller.telemetry import recorder as recorder_module
from meshforge.controller.telemetry.recorder import RunRecorder

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_defect_a_scan_longer_than_the_rescan_period_starves_newer_attempts(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    """Production shape: one sweep per 2 s, rescan per 60 s, batch 200 -> a scan
    covers 30 x 200 = 6000 un-excludable skips per period. Scaled down here:
    cap 1, batch 2, a rescan every 3 sweeps (clock + 1 per sweep, period 3) and
    20 un-excludable skipped attempts ahead of a good newer one."""
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 3.0)
    bad = await _attempts(engine, node_id, 21, start_s=0)
    good = await _attempts(engine, node_id, 1, start_s=500)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(60):
        clock.now += 1.0
        await recorder.sweep()
    assert good[0][1] in await _recorded(engine)


async def test_a_failed_sweep_past_the_cap_does_not_advance_the_watermark(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    bad = await _attempts(engine, node_id, 3, start_s=0)
    good = await _attempts(engine, node_id, 4, start_s=50)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    await recorder.sweep()
    await recorder.sweep()
    assert recorder.past_cap
    real = recorder_module._sources
    boom = {"left": 1}

    async def flaky(db: Any, attempts: Any, campaign_of: Any) -> Any:
        if boom["left"]:
            boom["left"] -= 1
            raise OperationalError("select", {}, Exception("connection lost"))
        return await real(db, attempts, campaign_of)

    monkeypatch.setattr(recorder_module, "_sources", flaky)
    with pytest.raises(recorder_module.TelemetryStoreError):
        await recorder.sweep()
    for _ in range(6):
        await recorder.sweep()
    assert await _recorded(engine) == {a for _, a in good}


async def test_equal_created_at_is_walked_by_the_id_tie_break(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    """Every attempt shares one created_at: the (created_at, id) watermark must
    not skip or repeat any of them."""
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    bad = await _attempts(engine, node_id, 4, start_s=0)
    good = await _attempts(engine, node_id, 7, start_s=0)
    async with engine.begin() as conn:
        await conn.execute(update(Attempt).values(created_at=T0))
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(12):
        await recorder.sweep()
    assert await _recorded(engine) == {a for _, a in good}


async def test_a_skipped_attempt_that_becomes_buildable_is_recorded_after_a_rescan(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    bad = await _attempts(engine, node_id, 4, start_s=0)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(4):
        await recorder.sweep()
    assert await _recorded(engine) == set()
    monkeypatch.undo()  # builder fixed (also drops the module patches below)
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(
        recorder_module,
        "time",
        __import__("types").SimpleNamespace(monotonic=clock.monotonic),
    )
    clock.now += recorder_module.PAST_CAP_RESCAN_S
    for _ in range(6):
        await recorder.sweep()
    # the one excluded by id stays excluded until restart; the rest come back
    assert await _recorded(engine) == {a for _, a in bad[1:]}


async def test_every_job_of_the_batch_missing_is_all_skips_and_no_crash(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await _attempts(engine, node_id, 3, start_s=0)
    real = recorder_module._sources

    async def deleting(db: Any, attempts: Any, campaign_of: Any) -> Any:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM job"))
        return await real(db, attempts, campaign_of)

    monkeypatch.setattr(recorder_module, "_sources", deleting)
    recorder = RunRecorder(engine, batch=10)
    assert await recorder.sweep() == 0
    assert recorder.skipped == 3
    assert [a["error_type"] for a in await _alerts(engine)] == ["job_missing"] * 3
    assert {a["attempt_id"] for a in await _alerts(engine)} == {a for _, a in made}
    monkeypatch.setattr(recorder_module, "_sources", real)
    assert await recorder.sweep() == 0


async def test_a_restart_realerts_at_most_cap_plus_one_and_still_records(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    """The exclusion set is in memory (documented 'until restart'): a restarted
    recorder alerts again, but boundedly - at most cap alerts plus the single
    skip_cap_reached - and newer attempts are recorded."""
    cap = 2
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", cap)
    bad = await _attempts(engine, node_id, 6, start_s=0)
    _poison(monkeypatch, {job for job, _ in bad})
    first = RunRecorder(engine, batch=2)
    for _ in range(8):
        await first.sweep()
    after_first = len(await _alerts(engine))
    assert after_first == cap + 1
    good = await _attempts(engine, node_id, 2, start_s=100)
    second = RunRecorder(engine, batch=2)
    for _ in range(12):
        await second.sweep()
    assert await _recorded(engine) == {a for _, a in good}
    assert len(await _alerts(engine)) - after_first <= cap + 1
    # and the first recorder, still running, adds nothing new
    count = len(await _alerts(engine))
    for _ in range(4):
        clock.now += recorder_module.PAST_CAP_RESCAN_S
        await first.sweep()
    assert len(await _alerts(engine)) == count


# ------------------------------------------------------------- tester r2 (D236)


async def test_r2_arrivals_during_a_long_pass_do_not_make_it_endless(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    """New un-excludable skips keep arriving (one per sweep, batch 2, so the
    scan outpaces them): the pass still reaches the end, and a good attempt
    that arrives last is recorded."""
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 3.0)
    all_bad = await _attempts(engine, node_id, 12, start_s=0)
    victims = {job for job, _ in all_bad}
    recorder = RunRecorder(engine, batch=2)
    _poison(monkeypatch, victims)
    next_start = 100
    for i in range(40):
        clock.now += 1.0
        more = await _attempts(engine, node_id, 1, start_s=next_start + i)
        victims.add(more[0][0])
        await recorder.sweep()
    good = await _attempts(engine, node_id, 1, start_s=900)
    for _ in range(40):
        clock.now += 1.0
        await recorder.sweep()
    assert good[0][1] in await _recorded(engine)


async def test_r2_the_watermark_never_resets_while_a_pass_is_unfinished(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 1.0)
    bad = await _attempts(engine, node_id, 30, start_s=0)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    marks = []
    for _ in range(10):  # 20 of 30 rows seen, the clock far past the period
        clock.now += 100.0
        await recorder.sweep()
        marks.append(recorder._watermark)
    assert marks == sorted(marks, key=lambda m: (m[0], m[1]))
    assert len(set(marks)) == len(marks)  # strictly forward every sweep


async def test_r2_a_late_finishing_old_attempt_is_recorded_after_the_next_full_pass(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 5.0)
    running = await _attempts(engine, node_id, 1, start_s=0, state="running")
    bad = await _attempts(engine, node_id, 4, start_s=10)
    good = await _attempts(engine, node_id, 2, start_s=50)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(8):
        clock.now += 1.0
        await recorder.sweep()
    assert recorder.past_cap
    assert await _recorded(engine) == {a for _, a in good}
    async with engine.begin() as conn:  # the old attempt finishes late
        await conn.execute(
            update(Attempt).where(Attempt.id == running[0][1]).values(state="done")
        )
    for _ in range(30):
        clock.now += 1.0
        await recorder.sweep()
    assert running[0][1] in await _recorded(engine)
    assert recorder.past_cap


async def test_r2_a_restart_mid_pass_loses_nothing_and_the_old_recorder_is_inert(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 2.0)
    bad = await _attempts(engine, node_id, 15, start_s=0)
    good = await _attempts(engine, node_id, 2, start_s=100)
    _poison(monkeypatch, {job for job, _ in bad})
    first = RunRecorder(engine, batch=2)
    for _ in range(4):  # mid-pass
        clock.now += 1.0
        await first.sweep()
    assert first.past_cap and not first._pass_done
    second = RunRecorder(engine, batch=2)  # the controller restarted
    for _ in range(30):
        clock.now += 1.0
        await second.sweep()
    assert await _recorded(engine) == {a for _, a in good}
    assert second._watermark is not None


async def test_r2_a_pass_with_an_exact_batch_multiple_still_ends(
    engine: Any,
    node_id: str,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
) -> None:
    """Rows == k * batch: the last full batch is followed by an empty sweep, which
    must set the pass-done flag (a rescan can still happen)."""
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 2.0)
    bad = await _attempts(engine, node_id, 6, start_s=0)
    _poison(monkeypatch, {job for job, _ in bad})
    recorder = RunRecorder(engine, batch=2)
    for _ in range(6):
        clock.now += 1.0
        await recorder.sweep()
    assert recorder.skipped > 6  # the pass ended and a rescan retried the skips
    monkeypatch.undo()
    monkeypatch.setattr(recorder_module, "MAX_SKIPPED_IDS", 1)
    monkeypatch.setattr(recorder_module, "PAST_CAP_RESCAN_S", 2.0)
    monkeypatch.setattr(
        recorder_module,
        "time",
        __import__("types").SimpleNamespace(monotonic=clock.monotonic),
    )
    for _ in range(12):
        clock.now += 1.0
        await recorder.sweep()
    assert await _recorded(engine) == {a for _, a in bad[1:]}
