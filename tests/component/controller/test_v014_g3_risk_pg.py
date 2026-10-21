"""V0.14 G3 (D296) on a real PostgreSQL:

* the snapshot read counts each node's attempt ends in the risk window in one
  grouped query - node-caused (``lost``, ``node_lost``, ``owner_preempted``,
  ``disk_full``, ``env_error``, ``transfer_error`` ...) as failures, ``done``
  and workload failures (``user_error``, ``oom``, ``timeout`` ...) in the
  total only, ``cancelled`` / ``fenced`` / live attempts not at all, ends
  before the window not at all;
* a real dispatch tick with that history: the decision row's candidates carry
  the ``risk`` term, and reading the row back and replaying it reproduces the
  stored decision exactly (risk included).
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

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import read_fabric
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.planner.risk import RISK_TERM
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

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


async def _seed(pg: DatabaseSettings, store: ExecutionStore) -> tuple[str, str, str]:
    """A: 3 node-caused of 8 counted; B: 0 of 5 (only workload failures and
    successes); C: nothing counted in the window."""
    a, b, c = await _node(pg, 100.0), await _node(pg, 100.0), await _node(pg, 100.0)
    job = await _history_job(store, pg)
    token = [0]
    await _ended(
        pg,
        job,
        a,
        [
            ("lost", "node_lost", 60.0),
            ("failed", "disk_full", 3600.0),
            ("failed", "owner_preempted", 2 * DAY),
            ("done", None, 10.0),
            ("done", None, 20.0),
            ("failed", "user_error", 30.0),  # the job's own crash
            ("failed", "oom", 40.0),
            ("failed", "timeout", 50.0),
            ("failed", "cancelled", 60.0),  # not counted
            ("fenced", "fenced", 70.0),  # not counted
            ("failed", "node_lost", 30 * DAY),  # outside the window
            ("running", None, None),  # live: not counted
        ],
        token,
    )
    await _ended(
        pg,
        job,
        b,
        [
            ("done", None, 10.0),
            ("done", None, 11.0),
            ("failed", "user_error", 12.0),
            ("failed", "deadline_exceeded", 13.0),
            ("failed", "output_error", 14.0),
            ("failed", "integrity_error", 15.0),  # not counted (the holder's)
        ],
        token,
    )
    await _ended(pg, job, c, [("lost", None, 20 * DAY)], token)
    return a, b, c


async def test_the_reliability_read_counts_the_right_rows(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b, c = await _seed(pg_settings, store)
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            fabric = await read_fabric(
                db, [], history_runs=20, keys_of=lambda _j: [], risk_since=since
            )
            assert fabric.reliability == {a: (3, 8), b: (0, 5)}
            assert c not in fabric.reliability
            off = await read_fabric(db, [], history_runs=20, keys_of=lambda _j: [])
            assert off.reliability == {}
    finally:
        await engine.dispose()


async def test_a_dispatch_tick_with_history_records_and_replays_the_risk(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    a, b, c = await _seed(pg_settings, store)
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
        manager=Manager([a, b, c]),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(risk_window_days=7.0),
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
        risk_window_s=7 * DAY,
    )
    assert result.attempt is not None
    # a, b, c are equal but for their reliability: the unreliable A loses
    assert result.attempt.node_id != a
    (row,) = await _sql(
        pg_settings,
        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, candidates, "
        "replay, objective FROM plan_decision WHERE job_id = :j",
        j=job.id,
    )
    assert row.objective == "min_time"
    risks = {r["node_id"]: r["estimate"]["terms"][RISK_TERM] for r in row.candidates}
    assert risks[a]["detail"]["failures"] == 3 and risks[a]["detail"]["total"] == 8
    assert risks[a]["source"] == "history"
    assert risks[c]["source"] == "assumed"  # the prior only
    assert risks[a]["seconds"] > risks[b]["seconds"]
    replay = row.replay if isinstance(row.replay, dict) else json.loads(row.replay)
    request, snapshot, now = from_replay(replay)
    by_id = {n.node_id: n.reliability for n in snapshot.nodes}
    assert by_id[a] is not None and (by_id[a].failures, by_id[a].total) == (3, 8)
    again = plan(request, snapshot, row.policy, now)
    assert again.chosen is not None and again.chosen.node_id == row.chosen_node_id
    assert candidates_json(again) == row.candidates
    assert again.snapshot_digest == row.snapshot_digest
    assert again.reason_key == row.reason_key


async def test_the_reliability_read_covers_exactly_the_snapshot_nodes(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G3 review N3: the grouped read is filtered by the snapshot's node ids,
    never cut by a row cap - every node the planner sees gets its counts, and
    no node outside the snapshot is read."""
    from meshforge.controller.jobs import queue_reads

    a, b, _c = await _seed(pg_settings, store)
    monkeypatch.setattr(queue_reads, "MAX_NODES", 2)  # the snapshot holds 2 nodes
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            fabric = await read_fabric(
                db, [], history_runs=20, keys_of=lambda _j: [], risk_since=since
            )
            seen = set(fabric.nodes)
            assert len(seen) == 2
            assert set(fabric.reliability) <= seen
            want = {a: (3, 8), b: (0, 5)}
            assert fabric.reliability == {n: v for n, v in want.items() if n in seen}
    finally:
        await engine.dispose()
