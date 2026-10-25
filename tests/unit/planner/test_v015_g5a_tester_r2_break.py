"""V0.15 G5a tester round 2: try to break B1 (training guard) and B2 (fold budget)."""

from __future__ import annotations

import asyncio
import datetime as dt
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")

from test_v015_g5a_fix_r1 import _inputs, _Overlap, _retrainer

from meshforge.controller.ml.child import TRAINING_GUARD, run_child
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


def _free() -> bool:
    return not TRAINING_GUARD.busy()


def test_guard_released_after_budget_stop_and_child_exception() -> None:
    async def boom(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        raise RuntimeError("child blew up")

    async def hang(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        await asyncio.sleep(30)
        return 0, ""

    async def main() -> None:
        inputs = _inputs(hang, PlannerSettings(ml_eval_timeout_s=1.0))
        await inputs.prepare({"a": frozenset(), "b": frozenset()}, allow_leak=False)
        assert _free(), "guard leaked after a budget stop"
        inputs = _inputs(boom, PlannerSettings())
        with pytest.raises(RuntimeError):
            await inputs.prepare({"a": frozenset()}, allow_leak=False)
        assert _free(), "guard leaked after a child exception"
        async with asyncio.timeout(1):
            async with TRAINING_GUARD.held():
                pass

    asyncio.run(main())


def test_budget_of_one_second_bounds_total_time_and_reports_every_family() -> None:
    cancelled = []

    async def hang(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return 0, ""

    fams = {f"f{i}": frozenset({f"g{i}"}) for i in range(6)}
    t0 = time.monotonic()
    ml = asyncio.run(
        _inputs(hang, PlannerSettings(ml_eval_timeout_s=1.0)).prepare(
            fams, allow_leak=False
        )
    )
    assert time.monotonic() - t0 < 5
    assert len(cancelled) == 1
    assert len(ml.folds) == 6
    assert all(f["reason"].startswith("eval_budget_exhausted") for f in ml.folds)
    assert all(v is None for v in ml.inputs.values())


def test_cancelled_retrain_waiting_on_guard_does_not_leak_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Overlap(hold_s=0.05)
    retrainer = _retrainer(fake, monkeypatch)

    async def main() -> None:
        async with TRAINING_GUARD.held():
            waiters = [
                asyncio.create_task(retrainer._retrain("t", NOW)) for _ in range(3)
            ]
            await asyncio.sleep(0.1)
            waiters[1].cancel()  # a middle waiter dies
            with pytest.raises(asyncio.CancelledError):
                await waiters[1]
        r = await asyncio.wait_for(asyncio.gather(waiters[0], waiters[2]), 5)
        assert all(x.error == "child_failed" for x in r)
        assert _free()

    asyncio.run(main())
    assert fake.peak == 1 and len(fake.calls) == 2


def test_retrain_wait_is_not_charged_to_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        seen.append(timeout_s)
        return 1, "x"

    retrainer = _retrainer(child, monkeypatch)

    async def main() -> None:
        async with TRAINING_GUARD.held():
            t = asyncio.create_task(retrainer._retrain("t", NOW))
            await asyncio.sleep(0.3)
        await t

    asyncio.run(main())
    assert seen == [PlannerSettings().ml_retrain_timeout_s]


def test_two_concurrent_evaluations_serialise_and_each_has_own_budget() -> None:
    state = {"active": 0, "peak": 0}

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.2)
        finally:
            state["active"] -= 1
        return 1, "x"

    async def main() -> list[Any]:
        a = _inputs(child, PlannerSettings(ml_eval_timeout_s=30.0))
        b = _inputs(child, PlannerSettings(ml_eval_timeout_s=30.0))
        fams = {f"f{i}": frozenset({f"g{i}"}) for i in range(3)}
        return await asyncio.gather(
            a.prepare(fams, allow_leak=False), b.prepare(fams, allow_leak=False)
        )

    out = asyncio.run(main())
    assert state["peak"] == 1
    assert all(len(o.folds) == 3 for o in out)


def test_second_evaluation_starved_by_first_exhausts_its_own_budget() -> None:
    async def slow(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        await asyncio.sleep(1.5)
        return 1, "x"

    async def main() -> list[Any]:
        a = _inputs(slow, PlannerSettings(ml_eval_timeout_s=30.0))
        b = _inputs(slow, PlannerSettings(ml_eval_timeout_s=1.0))
        ta = asyncio.create_task(a.prepare({"x": frozenset()}, allow_leak=False))
        await asyncio.sleep(0.2)
        tb = asyncio.create_task(b.prepare({"x": frozenset()}, allow_leak=False))
        return [await ta, await tb]

    a, b = asyncio.run(main())
    assert a.folds[0]["reason"].startswith("child_failed")
    assert b.folds[0]["reason"].startswith("eval_budget_exhausted: stopped")


@pytest.mark.skipif(sys.platform == "win32", reason="uses sleep, pgrep and ps")
def test_real_child_killed_and_reaped_when_budget_expires_mid_fold() -> None:
    pids: list[int] = []

    async def real(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        marker = str(40000 + time.time_ns() % 20000)
        pids.append(marker)  # type: ignore[arg-type]
        return await run_child(["sleep", marker], timeout_s)

    ml = asyncio.run(
        _inputs(real, PlannerSettings(ml_eval_timeout_s=1.0)).prepare(
            {"a": frozenset()}, allow_leak=False
        )
    )
    assert ml.folds[0]["reason"].startswith("eval_budget_exhausted: stopped")
    out = subprocess.run(
        ["pgrep", "-f", f"sleep {pids[0]}"], capture_output=True, text=True
    )
    assert out.stdout.strip() == "", f"child survived: {out.stdout}"
    z = subprocess.run(["ps", "-eo", "stat,cmd"], capture_output=True, text=True)
    assert not [
        ln for ln in z.stdout.splitlines() if ln.startswith("Z") and "sleep 4" in ln
    ]


def test_scratch_dir_removed_after_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshforge.controller.planner_eval import ml_inputs as mi

    roots: list[Path] = []
    orig = mi.scratch_dir

    import contextlib

    @contextlib.asynccontextmanager
    async def spy(prefix: str):  # type: ignore[no-untyped-def]
        async with orig(prefix) as p:
            roots.append(p)
            yield p

    monkeypatch.setattr(mi, "scratch_dir", spy)

    async def hang(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        await asyncio.sleep(30)
        return 0, ""

    asyncio.run(
        _inputs(hang, PlannerSettings(ml_eval_timeout_s=1.0)).prepare(
            {"a": frozenset()}, allow_leak=False
        )
    )
    assert roots and not roots[0].exists()


def test_outer_cancellation_of_evaluation_releases_guard_and_dir() -> None:
    async def hang(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        await asyncio.sleep(30)
        return 0, ""

    async def main() -> None:
        t = asyncio.create_task(
            _inputs(hang, PlannerSettings()).prepare(
                {"a": frozenset()}, allow_leak=False
            )
        )
        await asyncio.sleep(0.3)
        assert TRAINING_GUARD.busy()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert _free()

    asyncio.run(main())
