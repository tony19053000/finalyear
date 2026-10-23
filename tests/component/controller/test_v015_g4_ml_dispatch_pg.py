"""V0.15 G4 (D336, D362-D364, Q21) on a real PostgreSQL, end to end.

Real dispatch ticks (``ExecutionStore.dispatch_planned`` + ``DispatchPlanner``
with an ML model) write ``plan_decision`` rows; everything is read back from
``jsonb``:

* every candidate row names ``ml_model_version``; each learnable term its
  ``detail.ml``; the replay ``ml: {version}`` (no new column - Q21);
* ``replay_decision`` on the stored replay with the same model reproduces the
  stored candidates, digest, reason key and choice; with the model gone it is
  ``ml_model_unavailable``;
* the model moved the dispatch (B predicted twice as fast);
* D362 through the whole loop: the attempt ends ``done``; the G1 dataset
  builder reads the decision back from PostgreSQL and its ``raw_s`` is the
  **analytical** prediction (``detail.ml.raw_s``), not the ML-corrected
  seconds, so the target stays a residual of the analytical model - and its
  features equal the features the planner fed the model, bit for bit;
* a failing model: the dispatch still happens, on the calibration fallback,
  and the stored row and replay say ``predictor_error``;
* no model: no ML key anywhere (V0.14 rows).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from mf_ml_stub import Rule, stub_ml
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.ml.export import build_dataset
from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g4", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "cd" * 32


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


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _nodes(pg: DatabaseSettings, n: int) -> list[str]:
    engine = create_engine(pg)
    try:
        out = []
        for _ in range(n):
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
            out.append(node)
        return sorted(out)
    finally:
        await engine.dispose()


def _planner(manager: Manager) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=20.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _dispatch_one(
    store: ExecutionStore, planner: DispatchPlanner, fp: str
) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g4",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fp, input_bytes=0),
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


async def _row(pg: DatabaseSettings, attempt: str) -> Any:
    (row,) = await _sql(
        pg,
        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, candidates, "
        "replay, replay_version FROM plan_decision WHERE attempt_id = :a",
        a=attempt,
    )
    return row


async def test_an_ml_dispatch_is_stored_named_and_replayable(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    planner = _planner(Manager([a, b]))
    ml, stub = stub_ml(
        {
            ("compute", None): Rule(y_hat=0.0),
            ("compute", b): Rule(y_hat=math.log(0.25)),
            ("env", None): Rule(y_hat=0.1),
        }
    )
    planner.ml = ml
    attempt = await _dispatch_one(store, planner, "1" * 64)
    live_queries = list(stub.queries)  # what the dispatch fed the model
    row = await _row(pg_settings, attempt)
    assert row.chosen_node_id == b  # the model moved the dispatch
    assert {c["ml_model_version"] for c in row.candidates} == {"stub-v1"}
    chosen = next(c for c in row.candidates if c["rank"] == 1)
    compute = chosen["estimate"]["terms"]["compute"]
    assert compute["detail"]["ml"]["used"] is True
    assert compute["detail"]["ml"]["raw_s"] == 60.0
    assert compute["seconds"] == pytest.approx(15.0)
    assert row.replay_version == 2 and row.replay["ml"] == {"version": "stub-v1"}

    again = replay_decision(row.replay, row.policy, load=lambda v: ml)
    assert again.status == "ok" and again.decision is not None
    assert candidates_json(again.decision) == row.candidates
    assert again.decision.snapshot_digest == row.snapshot_digest
    assert again.decision.reason_key == row.reason_key
    assert again.decision.chosen is not None
    assert again.decision.chosen.node_id == row.chosen_node_id

    gone = replay_decision(row.replay, row.policy, load=lambda _v: None)
    assert gone.status == ML_MODEL_UNAVAILABLE and gone.decision is None

    # D362 through the loop: the attempt ends, the dataset reads it back
    await _sql(
        pg_settings,
        "UPDATE attempt SET state = 'done', ended_at = now() WHERE id = :a",
        a=attempt,
    )
    for phase, ms in (("stage", 0), ("env", 25_000), ("run", 18_000), ("collect", 0)):
        await _sql(
            pg_settings,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt,
            p=phase,
            ms=ms,
            s=ms == 0,
        )
    engine = create_engine(pg_settings)
    try:
        built = await build_dataset(engine)
    finally:
        await engine.dispose()
    rows = {r["term"]: r for r in built.dataset.rows if r["attempt_id"] == attempt}
    assert set(rows) >= {"compute", "env"}, built.dataset.counts
    assert rows["compute"]["raw_s"] == 60.0  # analytical, not the 15 s served
    assert rows["compute"]["y"] == pytest.approx(math.log(18.0 / 60.0))
    assert rows["env"]["raw_s"] == 20.0
    for term, r in rows.items():
        (query,) = [q for q in live_queries if q.term == term and q.node_id == b]
        for name, live in zip(NUMERIC_FEATURES, query.features, strict=True):
            stored = r[name]
            assert (math.isnan(live) and math.isnan(stored)) or live == stored, (
                term,
                name,
            )


async def test_a_failing_model_still_dispatches_on_the_fallback(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    planner = _planner(Manager([a, b]))
    ml, _ = stub_ml({("compute", None): Rule()}, raises=RuntimeError("boom"))
    planner.ml = ml
    attempt = await _dispatch_one(store, planner, "2" * 64)
    row = await _row(pg_settings, attempt)
    assert row.chosen_node_id in {a, b}
    assert all(c["ml_fallback"] == "predictor_error" for c in row.candidates)
    assert row.replay["ml"] == {"version": "stub-v1", "fallback": "RuntimeError"}
    assert planner.ml_predictor_errors == 1
    again = replay_decision(row.replay, row.policy, load=lambda _v: None)
    assert again.decision is not None
    assert candidates_json(again.decision) == row.candidates


async def test_no_model_writes_no_ml_key(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b = await _nodes(pg_settings, 2)
    attempt = await _dispatch_one(store, _planner(Manager([a, b])), "3" * 64)
    row = await _row(pg_settings, attempt)
    assert all("ml_model_version" not in c for c in row.candidates)
    assert "ml" not in row.replay
