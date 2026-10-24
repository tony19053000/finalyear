# ruff: noqa: B007, SIM105
"""V0.15 G6a tester round 2: try to break the fix-round-1 bounds (replay load
timeout, classifier examples budget, settings ranges, the 1e150 feature bound)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from pydantic import ValidationError

from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.train import classify as cl
from meshforge.controller.ml.train.report import canonical_json
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval import classifier_service as cs
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ settings ranges


@pytest.mark.parametrize(
    ("field", "lo", "hi"),
    [
        ("ml_classifier_timeout_s", 1.0, 86_400.0),
        ("ml_replay_load_timeout_s", 1.0, 3600.0),
    ],
)
def test_settings_ranges(field: str, lo: float, hi: float) -> None:
    assert getattr(PlannerSettings(**{field: lo}), field) == lo
    assert getattr(PlannerSettings(**{field: hi}), field) == hi
    for bad in (
        lo - 0.001,
        0.0,
        -5.0,
        hi + 0.001,
        float("nan"),
        float("inf"),
        "abc",
        None,
    ):
        with pytest.raises((ValidationError, ValueError)):
            PlannerSettings(**{field: bad})


# ------------------------------------------------------------ 1e150 bound & parser


def _ex(fam: str, i: int, label: str, feats: list[Any]) -> dict[str, Any]:
    oc = {"config_id": "c", "status": "counted", "regret_pct": 3.0, "votes": {}}
    return {
        "campaign_id": "c",
        "group_id": f"{fam}-{i}",
        "family": fam,
        "fingerprint": None,
        "label": label,
        "oracle_class": label,
        "records_used": 1,
        "features": feats,
        "classes": {"local": dict(oc), "mode1": dict(oc)},
        "cost_model": dict(oc),
    }


def _doc(feats_for: Callable[[int], list[Any]], n: int = 12) -> dict[str, Any]:
    return {
        "examples_version": 1,
        "campaign_ids": ["c"],
        "policy": "p",
        "feature_names": ["a", "b"],
        "counts": {},
        "calibrations": {},
        "examples": [
            _ex(f"f{k % 3}", k, ["local", "mode1"][k % 2], feats_for(k))
            for k in range(n)
        ],
    }


@pytest.mark.parametrize(
    "mag", [1e150, -1e150, 1e149, 1.0000001e150, 1e151, 1e300, 1.7e308]
)
def test_feature_magnitude_boundary_never_crashes(mag: float) -> None:
    s = cl.classify(_doc(lambda k: [mag * (1 if k % 2 else 0.5), float(k)]))
    assert s["status"] == "evaluated"
    json.loads(canonical_json(s))


def test_exactly_1e150_is_kept_and_just_over_is_missing() -> None:
    _, kept = cl.parse_examples(_doc(lambda k: [1e150, 0.0]))
    assert kept[0].features[0] == 1e150
    _, over = cl.parse_examples(_doc(lambda k: [1.0000001e150, 0.0]))
    assert over[0].features[0] != over[0].features[0]  # NaN


def test_mixed_huge_and_tiny_features_in_one_column() -> None:
    s = cl.classify(
        _doc(lambda k: [1e150 if k % 2 else 1e-150, 1e150 if k % 3 else -1e150])
    )
    assert s["status"] == "evaluated"


@pytest.mark.parametrize("big", [10**400, -(10**400)])
def test_python_bigint_literals_do_not_crash_the_child(big: int) -> None:
    # JSON allows integers of any size
    doc = json.loads(json.dumps(_doc(lambda k: [float(k), float(k)])))
    doc["examples"][0]["features"][0] = big
    try:
        s = cl.classify(doc)
    except cl.ExamplesError:
        return
    assert s["status"] == "evaluated"


@pytest.mark.parametrize("big", [10**400, -(10**400)])
def test_bigint_regret_does_not_crash_the_child(big: int) -> None:
    doc = _doc(lambda k: [float(k), 1.0])
    doc["examples"][0]["classes"]["local"]["regret_pct"] = big
    try:
        s = cl.classify(doc)
    except cl.ExamplesError:
        return
    assert s["status"] == "evaluated"


# ------------------------------------------------------------ RegistryLoader.blocking


class _FlakyLoader:
    """First load hangs, later ones answer; counts live hanging loads."""

    def __init__(self) -> None:
        self.calls = 0
        self.live = 0

    async def load(self, version: str) -> Any:
        self.calls += 1
        if self.calls == 1:
            self.live += 1
            try:
                await asyncio.sleep(3600)
            finally:
                self.live -= 1
        return f"ml-{version}"

    def blocking(self, loop: Any, *, timeout_s: float | None = None) -> Any:
        return RegistryLoader.blocking(self, loop, timeout_s=timeout_s)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_timed_out_load_does_not_poison_the_next_one() -> None:
    loader = _FlakyLoader()
    load = loader.blocking(asyncio.get_running_loop(), timeout_s=0.2)
    with pytest.raises(TimeoutError):
        await asyncio.to_thread(load, "v1")
    await asyncio.sleep(0.1)
    assert loader.live == 0  # the hanging coroutine was cancelled, not leaked
    assert await asyncio.to_thread(load, "v1") == "ml-v1"


@pytest.mark.asyncio
async def test_timeout_none_means_no_bound_and_fast_loads_pass() -> None:
    loader = _FlakyLoader()
    loader.calls = 1
    load = loader.blocking(asyncio.get_running_loop(), timeout_s=None)
    assert await asyncio.to_thread(load, "x") == "ml-x"


@pytest.mark.asyncio
async def test_a_tiny_bound_with_a_fast_load_is_still_a_timeout_or_a_result() -> None:
    loader = _FlakyLoader()
    loader.calls = 1
    load = loader.blocking(asyncio.get_running_loop(), timeout_s=0.0)
    try:
        r = await asyncio.to_thread(load, "x")
        assert r == "ml-x"
    except TimeoutError:
        pass


@pytest.mark.asyncio
async def test_calling_blocking_on_the_loop_thread_still_refuses() -> None:
    loader = _FlakyLoader()
    load = loader.blocking(asyncio.get_running_loop(), timeout_s=1.0)
    with pytest.raises(RuntimeError):
        load("v")


@pytest.mark.asyncio
async def test_other_load_errors_propagate_unchanged() -> None:
    class Boom(_FlakyLoader):
        async def load(self, version: str) -> Any:
            raise KeyError("boom")

    load = Boom().blocking(asyncio.get_running_loop(), timeout_s=5.0)
    with pytest.raises(KeyError):
        await asyncio.to_thread(load, "v")


# ------------------------------------------------------------ classifier service


class _Inputs:
    groups = tuple(f"g{i}" for i in range(6))
    records = ()


class _Cal:
    async def matched_runs(self, _c: Any) -> list[Any]:
        return []


class Harness:
    def __init__(
        self, mp: pytest.MonkeyPatch, *, per_group_s: float = 0.0, load_s: float = 0.0
    ) -> None:
        self.active = 0
        self.max_active = 0
        self.started = 0
        self.finished = 0
        self.gate: threading.Event | None = None
        self._lk = threading.Lock()
        outer = self

        async def missing(_e: Any, _i: Any) -> list[str]:
            return []

        async def load(_e: Any, _i: Any, *, max_rows: int) -> Any:
            if load_s:
                await asyncio.sleep(load_s)
            return _Inputs()

        def build(groups: Any, _r: Any, _c: Any, *, check: Callable[[], None]) -> Any:
            with outer._lk:
                outer.active += 1
                outer.started += 1
                outer.max_active = max(outer.max_active, outer.active)
            try:
                if outer.gate is not None:
                    outer.gate.wait(10)
                for g in groups:
                    check()
                    time.sleep(per_group_s)
                return [], {"groups": len(groups), "examples": 0}
            finally:
                with outer._lk:
                    outer.active -= 1
                    outer.finished += 1

        mp.setattr(cs, "missing_campaigns", missing)
        mp.setattr(cs, "load_campaigns", load)
        mp.setattr(cs, "families_of", lambda _i: {})
        mp.setattr(cs, "refit_choices", lambda *_a: {})
        mp.setattr(cs, "build_examples", build)

    def service(self, timeout_s: float) -> cs.ClassifierExamplesService:
        return cs.ClassifierExamplesService(
            None,
            _Cal(),
            CalibrationConfig(),
            max_rows=10,
            timeout_s=timeout_s,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_concurrent_requests_under_budget_all_finish_and_never_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, per_group_s=0.02)
    svc = h.service(30.0)
    docs = await asyncio.gather(*(svc.document(["c"]) for _ in range(8)))
    assert len(docs) == 8 and h.max_active == 1 and h.finished == 8
    assert not svc._lock.locked()


@pytest.mark.asyncio
async def test_queue_time_counts_against_the_waiters_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, per_group_s=0.1)  # a build takes ~0.6 s
    svc = h.service(0.9)
    first = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(svc.document(["c"]))
    t0 = time.monotonic()
    ok = await first
    with pytest.raises(cs.ClassifierTimeoutError) as caught:
        await asyncio.wait_for(second, 10)
    assert ok["examples"] == []
    assert caught.value.details["phase"] in {"build", "lock_wait"}
    assert time.monotonic() - t0 < 5
    assert not svc._lock.locked()
    # the system still serves afterwards
    await asyncio.wait_for(svc.document(["c"]), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [0.0, 1e-9, -1.0])
async def test_degenerate_budgets_fail_cleanly_and_free_the_lock(
    monkeypatch: pytest.MonkeyPatch, budget: float
) -> None:
    h = Harness(monkeypatch)
    svc = h.service(budget)
    try:
        await asyncio.wait_for(svc.document(["c"]), 5)
    except cs.ClassifierTimeoutError:
        pass
    assert not svc._lock.locked()
    assert h.active == 0


@pytest.mark.asyncio
async def test_cancelling_a_lock_waiter_leaves_the_lock_to_the_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch)
    svc = h.service(30.0)
    await svc._lock.acquire()
    waiter = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert svc._lock.locked()  # still the holder's, not stolen or leaked
    svc._lock.release()
    assert not svc._lock.locked()
    await asyncio.wait_for(svc.document(["c"]), 5)


@pytest.mark.asyncio
async def test_cancelling_during_the_database_phase_frees_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(monkeypatch, load_s=30.0)
    svc = h.service(60.0)
    t = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert not svc._lock.locked()


@pytest.mark.asyncio
async def test_cancelling_mid_build_never_lets_two_builds_run_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client disconnect cancels the request while the thread builds. The
    brief for the budget says builds never overlap."""
    h = Harness(monkeypatch, per_group_s=0.05)
    h.gate = threading.Event()
    svc = h.service(30.0)
    t1 = asyncio.create_task(svc.document(["c"]))
    for _ in range(100):
        if h.started:
            break
        await asyncio.sleep(0.01)
    assert h.started == 1
    t1.cancel()
    with pytest.raises((asyncio.CancelledError, cs.ClassifierTimeoutError)):
        await t1
    t2 = asyncio.create_task(svc.document(["c"]))
    await asyncio.sleep(0.3)
    overlapped = h.max_active > 1 or h.started > 1
    h.gate.set()
    await asyncio.wait_for(t2, 10)
    assert not overlapped, "second build started while the cancelled one still runs"
