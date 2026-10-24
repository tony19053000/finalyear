"""V0.15 G6a tester r3: D5 adversarial cancellation (refit phase, repeated
cancels, the lock is freed only after the thread ends)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval import classifier_service as cs

pytestmark = pytest.mark.unit


class _Inputs:
    groups = ("g1", "g2", "g3")
    records = ()


class _Cal:
    async def matched_runs(self, _c: Any) -> list[Any]:
        return []


class World:
    def __init__(self, mp: pytest.MonkeyPatch, *, gate_refit: bool) -> None:
        self.gate = threading.Event()
        self.in_thread = threading.Event()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lk = threading.Lock()
        w = self

        def enter() -> None:
            with w._lk:
                w.active += 1
                w.calls += 1
                w.max_active = max(w.max_active, w.active)

        def leave() -> None:
            with w._lk:
                w.active -= 1

        async def missing(*_a: Any) -> list[str]:
            return []

        async def load(*_a: Any, **_k: Any) -> Any:
            return _Inputs()

        def refit(*_a: Any) -> dict[str, Any]:
            enter()
            try:
                if gate_refit:
                    w.in_thread.set()
                    w.gate.wait(10)
                return {}
            finally:
                leave()

        def build(groups: Any, _r: Any, _c: Any, *, check: Callable[[], None]) -> Any:
            enter()
            try:
                if not gate_refit:
                    w.in_thread.set()
                    w.gate.wait(10)
                for _g in groups:
                    check()
                return [], {"groups": 3, "examples": 0}
            finally:
                leave()

        mp.setattr(cs, "missing_campaigns", missing)
        mp.setattr(cs, "load_campaigns", load)
        mp.setattr(cs, "families_of", lambda _i: {})
        mp.setattr(cs, "refit_choices", refit)
        mp.setattr(cs, "build_examples", build)

    def service(self) -> cs.ClassifierExamplesService:
        return cs.ClassifierExamplesService(
            None,
            _Cal(),
            CalibrationConfig(),
            max_rows=10,
            timeout_s=60.0,  # type: ignore[arg-type]
        )


async def _wait(ev: threading.Event) -> None:
    for _ in range(300):
        if ev.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("thread never started")


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_refit", [True, False], ids=["refit", "build"])
async def test_cancel_keeps_the_lock_until_the_thread_ends(
    monkeypatch: pytest.MonkeyPatch, gate_refit: bool
) -> None:
    w = World(monkeypatch, gate_refit=gate_refit)
    svc = w.service()
    t1 = asyncio.create_task(svc.document(["c"]))
    await _wait(w.in_thread)
    t1.cancel()
    with pytest.raises((asyncio.CancelledError, cs.ClassifierTimeoutError)):
        await t1
    assert svc._lock.locked()  # the thread still runs
    t2 = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.2)
    first_calls = 1 if gate_refit else 2  # refit (+ build)
    assert w.max_active == 1 and w.calls == first_calls  # the second waits
    w.gate.set()
    await asyncio.wait_for(t2, 10)
    assert w.max_active == 1
    assert not svc._lock.locked()


@pytest.mark.asyncio
async def test_repeated_cancels_do_not_double_release_or_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(monkeypatch, gate_refit=False)
    svc = w.service()
    t1 = asyncio.create_task(svc.document(["c"]))
    await _wait(w.in_thread)
    for _ in range(5):
        t1.cancel()
        await asyncio.sleep(0.01)
    with pytest.raises((asyncio.CancelledError, cs.ClassifierTimeoutError)):
        await t1
    w.gate.set()
    for _ in range(300):
        if not svc._lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not svc._lock.locked()
    # a stray double release would raise here or break mutual exclusion
    w.gate.clear()
    w.in_thread.clear()
    a = asyncio.create_task(svc.document(["c"]))
    b = asyncio.create_task(svc.document(["c"]))
    await _wait(w.in_thread)
    await asyncio.sleep(0.1)
    assert w.active == 1
    w.gate.set()
    await asyncio.wait_for(asyncio.gather(a, b), 10)
    assert w.max_active == 1


@pytest.mark.asyncio
async def test_a_cancelled_waiter_behind_a_cancelled_build_gets_the_lock_once_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = World(monkeypatch, gate_refit=True)
    svc = w.service()
    t1 = asyncio.create_task(svc.document(["c"]))
    await _wait(w.in_thread)
    t2 = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.05)
    t1.cancel()
    t2.cancel()
    for t in (t1, t2):
        with pytest.raises((asyncio.CancelledError, cs.ClassifierTimeoutError)):
            await t
    w.gate.set()
    for _ in range(300):
        if not svc._lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not svc._lock.locked() and w.calls == 1
    w.gate.set()
    assert (await asyncio.wait_for(svc.document(["c"]), 10))["examples"] == []
