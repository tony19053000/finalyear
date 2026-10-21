"""V0.14 G2 (D293, D301/D306, D307) on a real PostgreSQL, end to end:

real dispatch ticks (``ExecutionStore.dispatch_planned`` + ``DispatchPlanner``)
write ``plan_decision`` rows; the attempts end ``done`` with measured phases;
:class:`CalibrationStore.refresh` matches them, fits, gates and stores a
version; :class:`CalibrationSource` hands it to the next tick, whose decision
row names it and whose replay - read back from PostgreSQL - reproduces the
decision with it.

* too few runs -> nothing is written;
* the first version (vs no calibration) activates; its factors are what the
  maths says;
* a later tick writes ``calibration_version`` and a replay ``calibration``
  block that reproduces the stored candidates, digest and choice;
* a refit no better than the active one on the held-out runs is ``rejected``
  with its reason and the active one stays;
* **one active row**: two concurrent refreshes (two engines) leave exactly
  one ``active`` row; the partial unique index refuses a second one outright.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.calibration_store import (
    CalibrationSource,
    CalibrationStore,
)
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    CalibrationConfig,
    calibration_json,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import calibration_from_replay, from_replay
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g2", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32
CFG = CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0))
DEFAULT_COMPUTE_S = 60.0


@dataclass
class Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def calibrations(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> AsyncIterator[CalibrationStore]:
    engine = create_engine(pg_settings, pool_size=3)
    try:
        yield CalibrationStore(engine)
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
            hardware_scores=None,
            cpu_slots=8,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


def _planner(manager: Manager) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=DEFAULT_COMPUTE_S),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _dispatch_one(
    store: ExecutionStore, planner: DispatchPlanner, *, fingerprint: str
) -> str:
    """A new job, one real dispatch tick; returns its attempt id."""
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g2",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fingerprint, input_bytes=0),
    )
    results = await store.dispatch_planned(
        planner.decide,
        limit=10,
        max_per_node=64,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    (result,) = [r for r in results if r.job.id == job.id]
    assert result.attempt is not None
    return result.attempt.id


async def _finish(
    pg: DatabaseSettings, attempt_id: str, *, run_s: float, at: int
) -> None:
    """The attempt ended ``done`` ``at`` seconds from a fixed base; its run
    phase measured ``run_s``; stage / env / collect never ran (skipped)."""
    await _sql(
        pg,
        "UPDATE attempt SET state = 'done', "
        "ended_at = timestamptz '2026-10-01 00:00:00+00' + make_interval(secs => :at) "
        "WHERE id = :a",
        a=attempt_id,
        at=at,
    )
    for phase, ms, skipped in (
        ("stage", 0, True),
        ("env", 0, True),
        ("run", round(run_s * 1000), False),
        ("collect", 0, True),
    ):
        await _sql(
            pg,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt_id,
            p=phase,
            ms=ms,
            s=skipped,
        )


async def _seed(
    pg: DatabaseSettings,
    store: ExecutionStore,
    planner: DispatchPlanner,
    run_seconds: list[float],
    *,
    start: int = 0,
) -> None:
    for i, run_s in enumerate(run_seconds):
        # a fresh fingerprint each: no run history, compute stays assumed 60 s
        attempt = await _dispatch_one(store, planner, fingerprint=f"{start + i:064x}")
        await _finish(pg, attempt, run_s=run_s, at=start + i)


async def _statuses(pg: DatabaseSettings) -> list[tuple[int, str]]:
    rows = await _sql(
        pg, "SELECT version, status FROM planner_calibration ORDER BY version"
    )
    return [(int(r.version), str(r.status)) for r in rows]


def _shrunk(m: float, n: int) -> float:
    return round(min(max(1.0 + (m - 1.0) * n / (n + 5.0), 0.2), 5.0), 6)


async def test_too_few_runs_write_nothing(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    result = await calibrations.refresh(CFG)
    assert result.row is None and result.skipped == "too_few_runs"
    assert result.matched_runs == 0
    node = await _node(pg_settings)
    await _seed(pg_settings, store, _planner(Manager([node])), [120.0])
    result = await calibrations.refresh(CFG)
    assert result.row is None and result.skipped == "too_few_runs"
    assert result.matched_runs == 1
    assert await _statuses(pg_settings) == []


async def test_fit_activate_dispatch_with_it_and_replay_from_postgres(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [120.0] * 10)  # 2x the 60 s guess

    first = await calibrations.refresh(CFG)
    assert first.row is not None and first.matched_runs == 10
    row = first.row
    assert row.status == "active" and row.reason is None
    assert row.fit_inputs["pairs"] == 8 and row.fit_inputs["held_out"] == 2
    assert row.holdout_error_new is not None and row.holdout_error_active is not None
    assert row.holdout_error_new < row.holdout_error_active  # beats uncalibrated
    assert row.calibration is not None
    found = row.calibration.lookup("compute", node, None)
    assert found is not None and found[1] == "node"
    assert found[0].n == 8 and found[0].median == 2.0
    assert found[0].factor == _shrunk(2.0, 8)
    assert row.calibration.lookup("transfer", node, None) is None  # skipped stage
    # the stored factors are the canonical JSON of the fit
    (stored,) = await _sql(
        pg_settings,
        "SELECT factors FROM planner_calibration WHERE version = :v",
        v=row.version,
    )
    assert stored.factors == calibration_json(row.calibration)

    # the dispatcher picks it up on its next tick ...
    source = CalibrationSource(calibrations, "auto")
    planner.calibration = await source.refresh()
    assert planner.calibration is not None
    assert planner.calibration.version == row.version
    attempt = await _dispatch_one(store, planner, fingerprint="e" * 64)
    (decision,) = await _sql(
        pg_settings,
        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, candidates, "
        "replay, calibration_version FROM plan_decision WHERE attempt_id = :a",
        a=attempt,
    )
    assert decision.calibration_version == row.version
    (chosen,) = [c for c in decision.candidates if c["rank"] == 1]
    compute = chosen["estimate"]["terms"]["compute"]
    assert compute["seconds"] == round(DEFAULT_COMPUTE_S * found[0].factor, 6)
    assert compute["detail"]["calibration"]["version"] == row.version
    assert compute["detail"]["calibration"]["raw_s"] == DEFAULT_COMPUTE_S

    # ... and its row, read back from PostgreSQL, replays to itself
    replay = (
        decision.replay
        if isinstance(decision.replay, dict)
        else json.loads(decision.replay)
    )
    request, snapshot, now = from_replay(replay)
    calibration = calibration_from_replay(replay)
    assert calibration is not None and calibration.version == row.version
    again = plan(request, snapshot, decision.policy, now, calibration=calibration)
    assert candidates_json(again) == decision.candidates
    assert again.snapshot_digest == decision.snapshot_digest
    assert again.reason_key == decision.reason_key
    assert again.chosen is not None and again.chosen.node_id == decision.chosen_node_id

    # the calibrated decision's run counts with its raw prediction: a refit
    # on the same reality is as good -> it supersedes the first version
    await _finish(pg_settings, attempt, run_s=120.0, at=100)
    second = await calibrations.refresh(CFG)
    assert second.row is not None and second.row.status == "active"
    assert second.superseded == row.version
    assert await _statuses(pg_settings) == [
        (row.version, "superseded"),
        (second.row.version, "active"),
    ]
    planner.calibration = await source.refresh()
    assert planner.calibration is not None
    assert planner.calibration.version == second.row.version


async def test_a_worse_refit_is_rejected_and_the_active_version_stays(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    # older runs matched the guess; the newest (held-out) ones took 2x
    await _seed(pg_settings, store, planner, [60.0] * 8 + [120.0] * 2)
    # an operator-known active version that is exactly right on the newest runs
    exact = {
        "format": 1,
        "terms": {
            "compute": {
                "global": {"factor": 2.0, "n": 50, "median": 2.0},
                "node": {},
                "node_family": {},
            }
        },
        "families": {},
    }
    (active,) = await _sql(
        pg_settings,
        "INSERT INTO planner_calibration (status, factors, fit_inputs) "
        "VALUES ('active', CAST(:f AS jsonb), '{}'::jsonb) RETURNING version",
        f=json.dumps(exact),
    )
    result = await calibrations.refresh(CFG)
    assert result.row is not None
    assert result.row.status == "rejected"
    assert result.row.reason is not None and result.row.reason.startswith(
        "holdout_error "
    )
    assert result.row.holdout_error_active == 0.0
    assert result.row.holdout_error_new is not None
    assert result.row.holdout_error_new > 0.0
    assert result.row.fit_inputs["active_version"] == active.version
    assert result.superseded is None
    assert await _statuses(pg_settings) == [
        (int(active.version), "active"),
        (result.row.version, "rejected"),
    ]
    source = CalibrationSource(calibrations, "auto")
    in_use = await source.refresh()
    assert in_use is not None and in_use.version == active.version


async def test_two_concurrent_refreshes_leave_one_active_row(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    await _seed(pg_settings, store, _planner(Manager([node])), [90.0] * 10)
    other_engine = create_engine(pg_settings, pool_size=2)
    try:
        other = CalibrationStore(other_engine)
        for _round in range(3):
            results = await asyncio.gather(
                calibrations.refresh(CFG), other.refresh(CFG)
            )
            assert all(r.row is not None for r in results)
            rows = await _sql(
                pg_settings,
                "SELECT count(*) AS n FROM planner_calibration WHERE status = 'active'",
            )
            assert rows[0].n == 1
    finally:
        await other_engine.dispose()
    statuses = await _statuses(pg_settings)
    assert len(statuses) == 6
    assert [s for _v, s in statuses].count("active") == 1
    assert statuses[-1][1] in ("active", "superseded", "rejected")


async def test_the_index_refuses_a_second_active_row(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    insert = (
        "INSERT INTO planner_calibration (status, factors, fit_inputs) "
        "VALUES (:s, '{}'::jsonb, '{}'::jsonb)"
    )
    await _sql(pg_settings, insert, s="active")
    await _sql(pg_settings, insert, s="superseded")
    await _sql(pg_settings, insert, s="superseded")
    with pytest.raises(IntegrityError):
        await _sql(pg_settings, insert, s="active")
    with pytest.raises(IntegrityError):  # rejected needs its reason
        await _sql(pg_settings, insert, s="rejected")
    rows = await _sql(pg_settings, "SELECT status FROM planner_calibration")
    assert sorted(r.status for r in rows) == ["active", "superseded", "superseded"]
