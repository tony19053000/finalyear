"""V0.15 G6a fix round 1 (N1, N2, N5): every wait on the replay and the
classifier examples paths is bounded.

* N1 - ``RegistryLoader.blocking(loop, timeout_s=)``: a model set load still
  running after the bound is cancelled and raises ``TimeoutError``; the
  replayer turns that into ``DecisionReplayError`` (``load_timeout``) and the
  route answers 503 with a message that says what was slow;
* N2 - ``ClassifierExamplesService.document`` has one time budget
  (``ml_classifier_timeout_s``) for the lock wait, the database reads and the
  re-planning build; over it -> ``ClassifierTimeoutError`` (504 ``timeout``),
  the build thread stops at its next group and the lock is free again;
* N5 - ``DecisionReplay.status`` is the shared ``ReplayStatus`` literal.
"""

from __future__ import annotations

import asyncio
import threading
import typing
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import FastAPI

from meshforge.common.errors import FailureType
from meshforge.controller.api import decision_replay_routes as replay_routes
from meshforge.controller.api import ml_classify_routes as classify_routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.ml import decision_replay as dr
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval import classifier_service as cs
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

STORED = dr.StoredDecision(
    decision_id=7,
    job_id="job_x",
    outcome="planned",
    policy="cost_model",
    chosen_node_id="node_a",
    snapshot_digest="d",
    candidates=[],
    replay={"ml": {"version": "sha256:" + "ab" * 32}},
)


class _HangingLoader:
    """``RegistryLoader``'s surface: a ``load`` that never finishes."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    async def load(self, version: str) -> Any:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return None

    def blocking(
        self, loop: asyncio.AbstractEventLoop, *, timeout_s: float | None = None
    ) -> Callable[[str], Any]:
        return RegistryLoader.blocking(self, loop, timeout_s=timeout_s)  # type: ignore[arg-type]


# ------------------------------------------------------------------ N1


@pytest.mark.asyncio
async def test_a_blocking_load_over_its_bound_is_cancelled_and_raises() -> None:
    loader = _HangingLoader()
    load = loader.blocking(asyncio.get_running_loop(), timeout_s=0.2)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.to_thread(load, "v"), 10.0)
    for _ in range(100):  # the cancel lands on the loop's next turn
        if loader.cancelled.is_set():
            break
        await asyncio.sleep(0.01)
    assert loader.cancelled.is_set()


@pytest.mark.asyncio
async def test_the_replayer_turns_a_slow_load_into_a_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read(_engine: Any, _decision_id: int) -> dr.StoredDecision:
        return STORED

    def replay_stored(stored: Any, load: Callable[[str], Any]) -> Any:
        return load("sha256:" + "ab" * 32)

    monkeypatch.setattr(dr, "read_decision", read)
    monkeypatch.setattr(dr, "replay_stored", replay_stored)
    replayer = dr.DecisionReplayer(
        engine=None,  # type: ignore[arg-type]
        loader=_HangingLoader(),  # type: ignore[arg-type]
        load_timeout_s=0.2,
    )
    with pytest.raises(dr.DecisionReplayError) as caught:
        await asyncio.wait_for(replayer.replay(7), 10.0)
    assert caught.value.details["cause"] == dr.LOAD_TIMEOUT
    assert caught.value.retryable or caught.value.failure_type == (
        FailureType.NETWORK_ERROR
    )


class _Principal:
    user_id = "usr_test"
    name = "t"
    is_admin = True


def _app(**state: Any) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(classify_routes.ml_classify_router)
    app.include_router(replay_routes.decision_replay_router)
    user = _Principal()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


async def _call(app: FastAPI, method: str, url: str, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, url, **kw)


@pytest.mark.asyncio
async def test_the_replay_route_answers_503_naming_the_slow_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read(_engine: Any, _decision_id: int) -> dr.StoredDecision:
        return STORED

    def replay_stored(stored: Any, load: Callable[[str], Any]) -> Any:
        return load("sha256:" + "ab" * 32)

    monkeypatch.setattr(dr, "read_decision", read)
    monkeypatch.setattr(dr, "replay_stored", replay_stored)
    replayer = dr.DecisionReplayer(
        engine=None,  # type: ignore[arg-type]
        loader=_HangingLoader(),  # type: ignore[arg-type]
        load_timeout_s=0.2,
    )
    app = _app(
        **{
            replay_routes.DECISION_REPLAY_STATE_KEY: replay_routes.DecisionReplayApi(
                replayer
            )
        }
    )
    got = await _call(app, "GET", "/v1/planner/decisions/7/replay")
    assert got.status_code == 503
    error = got.json()["error"]
    assert error["code"] == "network_error"
    assert "took longer than 0.2 s" in error["message"]
    assert got.headers["retry-after"] == "2"


def test_the_replay_and_classify_bounds_are_settings() -> None:
    planner = PlannerSettings()
    assert planner.ml_replay_load_timeout_s == dr.DEFAULT_LOAD_TIMEOUT_S
    assert planner.ml_classifier_timeout_s == cs.DEFAULT_TIMEOUT_S
    with pytest.raises(ValueError):
        PlannerSettings(ml_classifier_timeout_s=0.0)
    with pytest.raises(ValueError):
        PlannerSettings(ml_replay_load_timeout_s=0.0)


# ------------------------------------------------------------------ N5


def test_the_status_field_is_the_shared_literal() -> None:
    hints = typing.get_type_hints(dr.DecisionReplay)
    assert hints["status"] is dr.ReplayStatus
    assert replay_routes.ReplayStatus is dr.ReplayStatus
    assert set(typing.get_args(dr.ReplayStatus.__value__)) == set(dr.REPLAY_STATUSES)


def test_an_unexpected_unplanned_answer_reads_as_unreadable() -> None:
    from meshforge.controller.ml.replay import ReplayResult

    found = dr.compare(STORED, ReplayResult(None, "something_new", None))
    assert found.status == dr.REPLAY_UNREADABLE
    gone = dr.compare(STORED, ReplayResult(None, "ml_model_unavailable", "v"))
    assert gone.status == "ml_model_unavailable"


# ------------------------------------------------------------------ N2


class _Inputs:
    groups = ("g1", "g2", "g3", "g4", "g5", "g6")
    records = ()


class _Calibrations:
    async def matched_runs(self, _config: Any) -> list[Any]:
        return []


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.t

    def advance(self, s: float) -> None:
        with self._lock:
            self.t += s


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_s: float,
    clock: Callable[[], float] | None = None,
    slow_load_s: float = 0.0,
    step: Callable[[], None] | None = None,
) -> tuple[cs.ClassifierExamplesService, list[str]]:
    built: list[str] = []

    async def missing(_engine: Any, _ids: Any) -> list[str]:
        return []

    async def load(_engine: Any, _ids: Any, *, max_rows: int) -> Any:
        if slow_load_s:
            await asyncio.sleep(slow_load_s)
        return _Inputs()

    def build(
        groups: Any, _records: Any, _choices: Any, *, check: Callable[[], None]
    ) -> tuple[list[Any], dict[str, int]]:
        for g in groups:
            check()
            built.append(g)
            if step is not None:
                step()
        return [], {"groups": len(groups), "examples": 0}

    monkeypatch.setattr(cs, "missing_campaigns", missing)
    monkeypatch.setattr(cs, "load_campaigns", load)
    monkeypatch.setattr(cs, "families_of", lambda _inputs: {})
    monkeypatch.setattr(cs, "refit_choices", lambda *_a: {})
    monkeypatch.setattr(cs, "build_examples", build)
    service = cs.ClassifierExamplesService(
        None,  # type: ignore[arg-type]
        _Calibrations(),  # type: ignore[arg-type]
        CalibrationConfig(),
        max_rows=10,
        timeout_s=timeout_s,
        **({} if clock is None else {"monotonic": clock}),
    )
    return service, built


@pytest.mark.asyncio
async def test_a_build_over_its_budget_stops_at_the_next_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    # every group costs 1 s of (fake) time; the budget is 3.5 s
    service, built = _service(
        monkeypatch, timeout_s=3.5, clock=clock, step=lambda: clock.advance(1.0)
    )
    with pytest.raises(cs.ClassifierTimeoutError) as caught:
        await service.document(["c1"])
    assert caught.value.details == {"phase": "build", "timeout_s": 3.5}
    assert caught.value.failure_type == FailureType.TIMEOUT
    assert "ml_classifier_timeout_s" in caught.value.message
    assert built == ["g1", "g2", "g3", "g4"]  # stopped, not run to the end
    # the lock is free again: the next call runs (to the end, budget fresh)
    clock_ok = _Clock()
    again, built_again = _service(monkeypatch, timeout_s=3600.0, clock=clock_ok)
    again._lock = service._lock  # same lock: it must not be held
    doc = await asyncio.wait_for(again.document(["c1"]), 5.0)
    assert doc["examples"] == [] and len(built_again) == 6


@pytest.mark.asyncio
async def test_the_lock_wait_is_bounded_by_the_same_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, built = _service(monkeypatch, timeout_s=0.2)
    await service._lock.acquire()  # another build holds it
    try:
        with pytest.raises(cs.ClassifierTimeoutError) as caught:
            await asyncio.wait_for(service.document(["c1"]), 10.0)
    finally:
        service._lock.release()
    assert caught.value.details["phase"] == "lock_wait"
    assert "another classifier examples build" in caught.value.message
    assert built == []
    assert not service._lock.locked()


@pytest.mark.asyncio
async def test_slow_database_reads_count_against_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, built = _service(monkeypatch, timeout_s=0.2, slow_load_s=30.0)
    with pytest.raises(cs.ClassifierTimeoutError) as caught:
        await asyncio.wait_for(service.document(["c1"]), 10.0)
    assert caught.value.details["phase"] == "load"
    assert built == [] and not service._lock.locked()


@pytest.mark.asyncio
async def test_the_classify_route_answers_504_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(monkeypatch, timeout_s=0.2, slow_load_s=30.0)
    app = _app(
        **{
            classify_routes.ML_CLASSIFY_STATE_KEY: classify_routes.MlClassifyApi(
                service
            )
        }
    )
    got = await _call(
        app,
        "POST",
        "/v1/planner/ml/classifier-examples",
        json={"campaign_ids": ["c1"]},
    )
    assert got.status_code == 504
    error = got.json()["error"]
    assert error["code"] == "timeout"
    assert "ml_classifier_timeout_s" in error["message"]


def test_build_examples_checks_before_every_group() -> None:
    from mf_eval_world import fabric, group, records

    from meshforge.controller.planner_eval import classifier_examples as ce

    request, snapshot = fabric(
        {"node_a": 100.0, "node_b": 150.0, "node_c": 120.0, "node_d": 300.0}
    )
    groups = [
        group(
            name, {"node_a": 400.0, "node_b": 150.0, "node_c": 100.0, "node_d": 300.0}
        )
        for name in ("g1", "g2", "g3")
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    calls: list[int] = []
    ce.build_examples(groups, recs, {}, check=lambda: calls.append(1))
    assert len(calls) == 3

    class StopError(Exception):
        pass

    def stop() -> None:
        raise StopError

    with pytest.raises(StopError):
        ce.build_examples(groups, recs, {}, check=stop)


# ------------------------------------------------------------------ fix r2


@pytest.mark.asyncio
async def test_a_cancelled_build_keeps_the_lock_until_its_thread_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fix r2 D5: the request ends at once, the thread sees the cancel at its
    # next group, and only then is the lock free
    entered, release = threading.Event(), threading.Event()
    seen: list[str] = []

    def step() -> None:
        entered.set()
        release.wait(10)

    service, built = _service(monkeypatch, timeout_s=3600.0, step=step)
    task = asyncio.create_task(service.document(["c1"]))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5.0)
    assert service._lock.locked()  # the thread still runs: no second build yet
    seen.extend(built)
    release.set()
    for _ in range(200):
        if not service._lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not service._lock.locked()
    assert seen == ["g1"] and built == ["g1"]  # stopped at the next group
    doc = await asyncio.wait_for(service.document(["c1"]), 5.0)
    assert doc["examples"] == []


@pytest.mark.parametrize("big", [10**400, -(10**400)])
def test_integers_beyond_float_range_read_as_missing_or_unmeasured(big: int) -> None:
    # fix r2 D4: never an OverflowError
    pytest.importorskip("sklearn", reason="needs the ml extra")
    pytest.importorskip("lightgbm", reason="needs the ml extra")
    from meshforge.controller.ml.train import classify as cl

    doc: dict[str, Any] = {
        "examples_version": 1,
        "feature_names": ["x"],
        "examples": [
            {
                "campaign_id": "c",
                "group_id": "g",
                "family": "f",
                "label": "local",
                "features": [big],
                "classes": {
                    "local": {"config_id": "l", "status": "counted", "regret_pct": big}
                },
                "cost_model": {"config_id": "k", "status": "counted", "regret_pct": 1},
            }
        ],
    }
    (example,) = cl.parse_examples(doc)[1]
    assert example.features[0] != example.features[0]  # NaN
    assert cl.outcome_of(example, "local")["status"] == "unmeasured"
