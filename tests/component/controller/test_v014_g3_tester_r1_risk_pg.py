"""V0.14 G3 tester round 1 (real PostgreSQL):

* the SQL reliability read agrees with ``classify_end`` for EVERY attempt
  state x failure type (differential test: the SQL vs the pure table);
* the window edge is respected; live attempts never count;
* ``risk_window_days = 0`` through the real dispatcher: no risk term at all;
* many nodes are counted by the one grouped query.
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
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import read_fabric
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.planner.risk import RISK_TERM, EndClass, classify_end
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

LIVE = {"assigned", "staging", "env_ready", "running", "uploading"}

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g3", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32
DAY = 86_400.0


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


async def _node(pg: DatabaseSettings, cpu_score: float) -> str:
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
            hardware_scores={
                "metrics": [
                    {"name": "cpu_fp32_gflops_all", "status": "ok", "mean": cpu_score}
                ]
            },
            cpu_slots=4,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


async def _history_job(store: ExecutionStore, pg: DatabaseSettings) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="old",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto"},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(input_bytes=1000),
    )
    # it is history, not queue work
    await _sql(pg, "UPDATE job SET state = 'failed' WHERE id = :j", j=job.id)
    return job.id


async def _ended(
    pg: DatabaseSettings,
    job_id: str,
    node_id: str,
    ends: list[tuple[str, str | None, float | None]],
    token: list[int],
) -> None:
    """``ends`` = (state, failure_type, ended this many seconds ago or None)."""
    now = dt.datetime.now(dt.UTC)
    for state, failure, ago in ends:
        token[0] += 1
        await _sql(
            pg,
            "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, "
            "failure_type, ended_at) VALUES (:id, :j, :n, :t, :s, :f, :e)",
            id=str(new_attempt_id()),
            j=job_id,
            n=node_id,
            t=token[0],
            s=state,
            f=failure,
            e=None if ago is None else now - dt.timedelta(seconds=ago),
        )


async def test_v014_g3_tester_r1_sql_counts_agree_with_the_pure_classification(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    job = await _history_job(store, pg_settings)
    token = [0]
    expect: dict[str, tuple[int, int]] = {}
    for state in AttemptState:
        failures = (
            [None]
            if state is not AttemptState.FAILED
            else [
                *(f.value for f in FailureType),
                None,
            ]
        )
        for failure in failures:
            node = await _node(pg_settings, 100.0)
            ago = None if state.value in LIVE else 30.0
            await _ended(pg_settings, job, node, [(state.value, failure, ago)], token)
            cls = classify_end(state.value, failure)
            if ago is None:
                expect[node] = (0, 0)  # live: no ended_at
            else:
                expect[node] = (
                    1 if cls is EndClass.NODE else 0,
                    0 if cls is EndClass.NOT_COUNTED else 1,
                )
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            fabric = await read_fabric(
                db, [], history_runs=20, keys_of=lambda _j: [], risk_since=since
            )
    finally:
        await engine.dispose()
    for node, (failures, total) in expect.items():
        assert fabric.reliability.get(node, (0, 0)) == (failures, total), node


async def test_v014_g3_tester_r1_the_window_edge_and_many_nodes(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    job = await _history_job(store, pg_settings)
    token = [0]
    inside, outside = await _node(pg_settings, 1.0), await _node(pg_settings, 1.0)
    await _ended(pg_settings, job, inside, [("lost", None, 7 * DAY - 120.0)] * 3, token)
    await _ended(
        pg_settings, job, outside, [("lost", None, 7 * DAY + 120.0)] * 3, token
    )
    crowd = [await _node(pg_settings, 1.0) for _ in range(12)]
    for node in crowd:
        await _ended(
            pg_settings,
            job,
            node,
            [("done", None, 5.0), ("failed", "env_error", 6.0)],
            token,
        )
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            fabric = await read_fabric(
                db, [], history_runs=20, keys_of=lambda _j: [], risk_since=since
            )
    finally:
        await engine.dispose()
    assert fabric.reliability[inside] == (3, 3)
    assert outside not in fabric.reliability
    assert all(fabric.reliability[n] == (1, 2) for n in crowd)


async def test_v014_g3_tester_r1_window_zero_means_no_risk_term_end_to_end(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    job_hist = await _history_job(store, pg_settings)
    a, b = await _node(pg_settings, 100.0), await _node(pg_settings, 100.0)
    await _ended(
        pg_settings,
        job_hist,
        a,
        [("lost", None, 10.0)] * 5,
        [0],
    )
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g3",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto"},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(input_bytes=40_000_000, fingerprint="1" * 64),
    )
    planner = DispatchPlanner(
        manager=Manager([a, b]),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(risk_window_days=0.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )
    (result,) = await store.dispatch_planned(
        planner.decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
        risk_window_s=0.0,
    )
    assert result.attempt is not None
    (row,) = await _sql(
        pg_settings,
        "SELECT candidates, replay FROM plan_decision WHERE job_id = :j",
        j=job.id,
    )
    for cand in row.candidates:
        assert RISK_TERM not in cand["estimate"]["terms"]
    replay = row.replay if isinstance(row.replay, dict) else json.loads(row.replay)
    request, snapshot, now = from_replay(replay)
    assert all(n.reliability is None for n in snapshot.nodes)
    assert candidates_json(plan(request, snapshot, "cost_model", now)) == row.candidates
