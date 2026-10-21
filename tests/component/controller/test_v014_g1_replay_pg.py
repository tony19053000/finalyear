"""V0.14 G1 (D292) on a real PostgreSQL, through the real dispatch tick
(``ExecutionStore.dispatch_planned`` + ``DispatchPlanner``):

* the dispatched ``plan_decision`` row carries ``replay`` (jsonb),
  ``replay_version`` 1, ``objective`` ``min_time`` and ``calibration_version``
  NULL; reading the row back and replaying it gives the **same chosen
  candidate**, the stored candidate JSON byte for byte, the stored digest and
  reason key - and every other policy can be replayed on it;
* a waiting decision is replayable the same way;
* over ``replay_max_bytes`` the job still dispatches, with ``replay`` NULL;
* a replay PostgreSQL refuses (the 0030 check) drops the decision row alone in
  its savepoint (D131): the attempt and the rest of the tick commit.
"""

from __future__ import annotations

import asyncio
import dataclasses
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

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.common.manifest import POLICIES
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    ExecutionStore,
)
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g1", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32


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


async def _node(pg: DatabaseSettings, cpu_score: float, *, slots: int = 4) -> str:
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
            cpu_slots=slots,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


async def _job(store: ExecutionStore, *, submitter: str | None) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g1",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(
            fingerprint="1" * 64, input_bytes=40_000_000, submitter_node_id=submitter
        ),
    )
    return job


async def _fabric(pg: DatabaseSettings) -> tuple[str, str]:
    """L holds the project (and another job's tree); R is 4x faster and
    reachable from L over a fresh measured link."""
    local = await _node(pg, 100.0)
    remote = await _node(pg, 400.0)
    for key in (DIGEST, OTHER):
        await _sql(
            pg,
            "INSERT INTO cache_location (node_id, kind, key, size) "
            "VALUES (:n, 'blob', :k, 1)",
            n=local,
            k=key,
        )
    await _sql(
        pg,
        "INSERT INTO network_link (src_node_id, dst_node_id, rtt_ms, bw_bytes_s, "
        "samples, measured_at) VALUES (:r, :l, 4.0, 2500000, 3, now())",
        r=remote,
        l=local,
    )
    return local, remote


def _planner(manager: Manager, **planner: Any) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(**planner),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _tick(store: ExecutionStore, decide: Any) -> list[Any]:
    return await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )


async def _rows(pg: DatabaseSettings, job_id: str) -> list[Any]:
    return await _sql(
        pg,
        "SELECT outcome, policy, chosen_node_id, snapshot_digest, reason_key, "
        "candidates, replay, replay_version, objective, calibration_version, "
        "attempt_id FROM plan_decision WHERE job_id = :j ORDER BY id",
        j=job_id,
    )


def _replay_row(row: Any) -> None:
    """The row read back from PostgreSQL replays to itself."""
    assert row.replay_version == 2 and row.objective == "min_time"
    assert row.calibration_version is None
    replay = row.replay if isinstance(row.replay, dict) else json.loads(row.replay)
    request, snapshot, now = from_replay(replay)
    assert all(OTHER not in n.cached for n in snapshot.nodes)  # projected
    again = plan(request, snapshot, row.policy, now)
    chosen = None if again.chosen is None else again.chosen.node_id
    assert chosen == row.chosen_node_id
    assert candidates_json(again) == row.candidates
    assert again.snapshot_digest == row.snapshot_digest
    assert again.reason_key == row.reason_key
    for policy in POLICIES:  # the evaluator's use: any policy on the same world
        other = plan(request, snapshot, policy, now)
        assert {c.node_id for c in other.candidates} == {
            c["node_id"] for c in row.candidates
        }


async def test_dispatched_row_replays_to_the_same_choice(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings)
    job = await _job(store, submitter=local)
    planner = _planner(Manager([local, remote]))
    (result,) = await _tick(store, planner.decide)
    assert result.attempt is not None
    (row,) = await _rows(pg_settings, job.id)
    assert row.outcome == "dispatched" and row.attempt_id == result.attempt.id
    assert row.chosen_node_id == result.attempt.node_id
    _replay_row(row)
    assert planner.replays_over_cap == 0 and planner.replay_failures == 0


async def test_waiting_row_replays_too(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, _remote = await _fabric(pg_settings)
    job = await _job(store, submitter=local)
    results = await _tick(store, _planner(Manager([])).decide)  # no session
    assert all(r.attempt is None for r in results)
    (row,) = await _rows(pg_settings, job.id)
    assert row.outcome == "waiting" and row.chosen_node_id is None
    _replay_row(row)


async def test_over_the_cap_dispatches_without_a_replay(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _fabric(pg_settings)
    job = await _job(store, submitter=local)
    planner = _planner(Manager([local, remote]), replay_max_bytes=1024)
    (result,) = await _tick(store, planner.decide)
    assert result.attempt is not None
    (row,) = await _rows(pg_settings, job.id)
    assert row.outcome == "dispatched"
    assert row.replay is None and row.replay_version is None
    assert row.objective == "min_time"
    assert planner.replays_over_cap == 1


async def test_a_refused_replay_drops_the_row_alone(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    """D131: the 0030 check refuses a replay without a version - the decision
    row is dropped in its savepoint; the attempt and the other job commit."""
    local, remote = await _fabric(pg_settings)
    bad = await _job(store, submitter=local)
    good = await _job(store, submitter=local)
    planner = _planner(Manager([local, remote]))

    def decide(job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        decision = planner.decide(job, fabric)
        if job.id == bad.id and isinstance(decision, Dispatch):
            assert decision.record is not None
            # a replay without its version: the 0030 check refuses the row
            broken = dataclasses.replace(decision.record, replay_version=None)
            return dataclasses.replace(decision, record=broken)
        return decision

    results = await _tick(store, decide)
    assert {r.job.id for r in results if r.attempt is not None} == {bad.id, good.id}
    assert await _rows(pg_settings, bad.id) == []
    (row,) = await _rows(pg_settings, good.id)
    _replay_row(row)
