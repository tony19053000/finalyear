"""V0.15 G5b (D374) on a real PostgreSQL: ``GET /v1/planner/ml/predictions``.

1. **Correct** - a real dispatch tick writes a decision; its stored row is
   given the ``detail.ml`` blocks G4 writes (compute corrected, env OOD) and
   the attempt ends ``done``. The route's points equal the stored row and the
   measured phases (raw, ``raw x e^y_hat``, used / reason, the set version),
   a calibration version that cannot be read gives ``calibrated_s`` null,
   and the filters work. The decisions answer of the same job carries the
   typed ``ml`` fields (D373).
2. **Bounded and fast** - with 10 000 matched decisions stored, the default
   request (500) answers in < 300 ms (median of 5, D374 budget) and the
   maximum (2000) is bounded; the newest runs come first.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from fastapi import FastAPI
from sqlalchemy import text

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.api import auth, ml_prediction_routes
from meshforge.controller.api.decision_routes import _out
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

MANIFEST = {"name": "g5b", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "cd" * 32
SET = "sha256:" + "5e" * 32
PHASES = {"stage": 4_000, "env": 45_000, "run": 50_000, "collect": 200}
BUDGET_MS = 300.0
CLONES = 9_999


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


class _Admin:
    user_id = "usr_admin"
    name = "admin"
    is_admin = True


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _sql(engine: Any, statement: str, **params: Any) -> list[Any]:
    async with engine.begin() as conn:
        result = await conn.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


async def _node(engine: Any) -> str:
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


async def _seed(engine: Any) -> tuple[str, str]:
    """One real dispatched decision, made an ML decision, its attempt done.
    Returns (job id, attempt id)."""
    store = ExecutionStore(engine)
    nodes = [await _node(engine) for _ in range(2)]
    planner = DispatchPlanner(
        manager=Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=30.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g5b",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint="f" * 64, input_bytes=0),
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
    attempt = result.attempt.id
    (stored,) = await _sql(
        engine,
        "SELECT id, candidates, chosen_node_id FROM plan_decision WHERE attempt_id = :a",
        a=attempt,
    )
    rows = stored.candidates
    for row in rows:
        row["ml_model_version"] = SET
        terms = (row.get("estimate") or {}).get("terms") or {}
        if "compute" in terms:
            raw = terms["compute"]["seconds"]
            terms["compute"]["detail"]["ml"] = {
                "model_version": "m-compute",
                "used": True,
                "y_hat": -0.2,
                "lo": -0.3,
                "hi": -0.1,
                "raw_s": raw,
            }
            terms["compute"]["seconds"] = round(raw * math.exp(-0.2), 6)
        if "env" in terms:
            terms["env"]["detail"]["ml"] = {
                "model_version": SET,
                "used": False,
                "reason": "ood",
                "ood_reason": "range:hw_cpu_gflops",
                "y_hat": 0.5,
                "lo": 0.1,
                "hi": 0.9,
            }
    await _sql(
        engine,
        "UPDATE plan_decision SET candidates = CAST(:c AS jsonb), "
        "calibration_version = 999 WHERE id = :d",
        c=json.dumps(rows),
        d=stored.id,
    )
    await _sql(
        engine,
        "UPDATE attempt SET state = 'done', ended_at = timestamptz "
        "'2026-10-23 12:00:00+00' WHERE id = :a",
        a=attempt,
    )
    for phase, ms in PHASES.items():
        await _sql(
            engine,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, false)",
            a=attempt,
            p=phase,
            ms=ms,
        )
    return job.id, attempt


async def _columns(engine: Any, table: str) -> list[str]:
    rows = await _sql(
        engine,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t AND table_schema = current_schema() "
        "ORDER BY ordinal_position",
        t=table,
    )
    return [r[0] for r in rows]


async def _clone(engine: Any, job_id: str, attempt_id: str, n: int) -> None:
    """``n`` copies of the seeded job + attempt + phases + decision, each
    attempt ending one second older than the previous (bulk SQL)."""
    jobs = [str(new_job_id()) for _ in range(n)]
    attempts = [str(new_attempt_id()) for _ in range(n)]
    job_cols = await _columns(engine, "job")
    att_cols = await _columns(engine, "attempt")
    dec_cols = [c for c in await _columns(engine, "plan_decision") if c != "id"]
    phase_cols = await _columns(engine, "attempt_phase")

    def pick(cols: list[str], subst: dict[str, str], alias: str) -> str:
        return ", ".join(subst.get(c, f"{alias}.{c}") for c in cols)

    att_subst = {
        "id": "n.aid",
        "job_id": "n.jid",
        "ended_at": "a.ended_at - make_interval(secs => n.k)",
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO job ({', '.join(job_cols)}) "  # noqa: S608 - test SQL
                f"SELECT {pick(job_cols, {'id': 'n.jid'}, 'j')} FROM job j, "
                "unnest(CAST(:jobs AS text[])) AS n(jid) WHERE j.id = :seed"
            ),
            {"jobs": jobs, "seed": job_id},
        )
        await conn.execute(
            text(
                f"INSERT INTO attempt ({', '.join(att_cols)}) "  # noqa: S608
                f"SELECT {pick(att_cols, att_subst, 'a')} "
                "FROM attempt a, unnest(CAST(:jobs AS text[]), CAST(:atts AS text[])) "
                "WITH ORDINALITY AS n(jid, aid, k) WHERE a.id = :seed"
            ),
            {"jobs": jobs, "atts": attempts, "seed": attempt_id},
        )
        await conn.execute(
            text(
                f"INSERT INTO attempt_phase ({', '.join(phase_cols)}) "  # noqa: S608
                f"SELECT {pick(phase_cols, {'attempt_id': 'n.aid'}, 'p')} "
                "FROM attempt_phase p, unnest(CAST(:atts AS text[])) AS n(aid) "
                "WHERE p.attempt_id = :seed"
            ),
            {"atts": attempts, "seed": attempt_id},
        )
        await conn.execute(
            text(
                f"INSERT INTO plan_decision ({', '.join(dec_cols)}) "  # noqa: S608
                f"SELECT {pick(dec_cols, {'job_id': 'n.jid', 'attempt_id': 'n.aid'}, 'd')} "
                "FROM plan_decision d, unnest(CAST(:jobs AS text[]), CAST(:atts AS text[])) "
                "AS n(jid, aid) WHERE d.attempt_id = :seed"
            ),
            {"jobs": jobs, "atts": attempts, "seed": attempt_id},
        )


def _app(engine: Any, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    async def authenticate(_request: Any) -> Any:
        return _Admin()

    monkeypatch.setattr(auth, "authenticate", authenticate)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_prediction_routes.ml_predictions_router)
    setattr(
        app.state,
        ml_prediction_routes.ML_PREDICTIONS_STATE_KEY,
        ml_prediction_routes.MlPredictionsApi(engine, CalibrationStore(engine)),
    )
    return app


async def test_points_equal_the_stored_ml_decision(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id, attempt = await _seed(engine)
    (stored,) = await _sql(
        engine,
        "SELECT id, candidates, chosen_node_id FROM plan_decision WHERE attempt_id = :a",
        a=attempt,
    )
    row = next(
        c
        for c in stored.candidates
        if c["rank"] == 1 and c["node_id"] == stored.chosen_node_id
    )
    terms = row["estimate"]["terms"]
    app = _app(engine, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        body = (await client.get("/v1/planner/ml/predictions")).json()
        assert body["decisions_read"] == 1
        points = {p["term"]: p for p in body["points"]}
        compute = points["compute"]
        raw = terms["compute"]["detail"]["ml"]["raw_s"]
        assert compute["decision_id"] == stored.id
        assert compute["node_id"] == stored.chosen_node_id
        assert compute["analytical_s"] == raw
        assert compute["ml_s"] == pytest.approx(raw * math.exp(-0.2))
        assert compute["actual_s"] == PHASES["run"] / 1000.0
        assert compute["used"] is True and compute["reason"] is None
        assert compute["ml_model_version"] == SET
        # calibration v999 does not exist: never a guessed calibrated value
        assert compute["calibrated_s"] is None
        env = points["env"]
        assert env["used"] is False and env["reason"] == "ood"
        assert env["ml_s"] is None
        assert env["calibrated_s"] == env["analytical_s"]  # not scaled
        assert env["actual_s"] == PHASES["env"] / 1000.0

        only = (await client.get("/v1/planner/ml/predictions?term=env")).json()
        assert [p["term"] for p in only["points"]] == ["env"]
        other_set = "sha256:" + "0" * 64
        none = (await client.get(f"/v1/planner/ml/predictions?set={other_set}")).json()
        assert none["points"] == [] and none["decisions_read"] == 1
        mine = (await client.get(f"/v1/planner/ml/predictions?set={SET}")).json()
        assert len(mine["points"]) == len(body["points"])

    # D373: the decisions answer of the same job carries the typed fields
    records = await ExecutionStore(engine).decisions(job_id)
    (answer,) = [_out(r) for r in records]
    assert answer.ml_model_version == SET and answer.ml_fallback is None
    chosen = next(c for c in answer.candidates if c.rank == 1)
    assert chosen.estimate is not None
    ml = chosen.estimate.terms["compute"].ml
    assert ml is not None and ml.used is True and ml.raw_s == raw
    assert ml.corrected_s == terms["compute"]["seconds"]
    env_ml = chosen.estimate.terms["env"].ml
    assert env_ml is not None and env_ml.reason == "ood"
    assert env_ml.ood_reason == "range:hw_cpu_gflops"


async def test_ten_thousand_decisions_default_request_under_budget(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id, attempt = await _seed(engine)
    await _clone(engine, job_id, attempt, CLONES)
    (count,) = await _sql(engine, "SELECT count(*) FROM plan_decision")
    assert count[0] == CLONES + 1
    app = _app(engine, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        await client.get("/v1/planner/ml/predictions")  # warm (pool, imports)
        took: list[float] = []
        for _ in range(5):
            started = time.perf_counter()
            r = await client.get("/v1/planner/ml/predictions")
            took.append((time.perf_counter() - started) * 1000.0)
            assert r.status_code == 200
        body = r.json()
        assert body["decisions_read"] == 500
        ids = [p["decision_id"] for p in body["points"]]
        assert ids[0] == min(ids)  # the seed ended newest; clones are older
        biggest = time.perf_counter()
        maxed = await client.get("/v1/planner/ml/predictions?limit=2000")
        max_ms = (time.perf_counter() - biggest) * 1000.0
        assert maxed.json()["decisions_read"] == 2000
        over = await client.get("/v1/planner/ml/predictions?limit=2001")
        assert over.status_code == 400
    median = statistics.median(took)
    print(  # noqa: T201 - the recorded number (progress note)
        f"predictions @10k decisions: default 500 median {median:.1f} ms "
        f"(runs {[round(t, 1) for t in took]}), limit 2000 {max_ms:.1f} ms"
    )
    assert median < BUDGET_MS, took
