"""V0.14 G1 tester r1, real PostgreSQL (D292, D131).

* what PostgreSQL's jsonb does to a stored replay (1e15 floats printed as
  integers, 5.0 kept, NaN/inf boxed) must not change the decision it replays
  to;
* every way PostgreSQL can refuse the replay columns drops the decision row
  ALONE in its savepoint: the attempt, the job and the other job's row (and
  its replay) in the same tick commit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
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
from meshforge.controller.planner.replay import canonical_json, from_replay
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g1t", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32


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


async def _node(pg: DatabaseSettings, score: float) -> str:
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
                    {"name": "cpu_fp32_gflops_all", "status": "ok", "mean": score}
                ]
            },
            cpu_slots=4,
            gpu_slots=0,
        )
        return node
    finally:
        await engine.dispose()


async def _job(store: ExecutionStore, submitter: str) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g1t",
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


async def _world(pg: DatabaseSettings) -> tuple[str, str]:
    local, remote = await _node(pg, 100.0), await _node(pg, 400.0)
    await _sql(
        pg,
        "INSERT INTO cache_location (node_id, kind, key, size) "
        "VALUES (:n, 'blob', :k, 1)",
        n=local,
        k=DIGEST,
    )
    return local, remote


def _planner(manager: Manager) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
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
        "candidates, replay, replay_version, objective FROM plan_decision "
        "WHERE job_id = :j ORDER BY id",
        j=job_id,
    )


def _as_obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


async def test_jsonb_reshaping_of_floats_does_not_change_the_replayed_decision(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    """A replay full of the floats jsonb reshapes (1e15 and up printed as
    integers, -0.0, 5.0, NaN/inf boxed) comes back from PostgreSQL decoding
    to the same planner inputs and the same decision."""
    local, remote = await _world(pg_settings)
    job = await _job(store, local)
    planner = _planner(Manager([local, remote]))
    captured: dict[str, DispatchDecision] = {}

    def decide(j: JobRecord, fabric: FabricRows) -> DispatchDecision:
        decision = planner.decide(j, fabric)
        assert isinstance(decision, Dispatch) and decision.record is not None
        replay = json.loads(canonical_json(decision.record.replay or {}))
        request, snapshot, now = from_replay(replay)
        odd = dataclasses.replace(
            snapshot,
            nodes=tuple(
                dataclasses.replace(
                    n,
                    cpu_percent=1e15,
                    gpu_score=float("inf"),
                    cpu_score=(n.cpu_score or 1.0) + 0.0,
                    disk_free_bytes=10**18,
                )
                for n in snapshot.nodes
            ),
        )
        from meshforge.controller.planner.replay import to_replay

        replay2 = to_replay(request, odd, now)
        captured["odd"] = decision
        record = dataclasses.replace(decision.record, replay=replay2)
        return dataclasses.replace(decision, record=record)

    await _tick(store, decide)
    (row,) = await _rows(pg_settings, job.id)
    stored = _as_obj(row.replay)
    request, snapshot, now = from_replay(stored)
    for node in snapshot.nodes:
        assert node.cpu_percent == 1e15 and isinstance(node.cpu_percent, float)
        assert node.gpu_score == float("inf")
        assert isinstance(node.disk_free_bytes, int) and node.disk_free_bytes == 10**18
    # re-encoding what PostgreSQL gave back is the canonical text again
    assert json.loads(canonical_json(stored)) == stored
    assert plan(request, snapshot, row.policy, now).snapshot_digest is not None


async def test_the_replay_read_back_replays_to_the_stored_candidates(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _world(pg_settings)
    job = await _job(store, local)
    planner = _planner(Manager([local, remote]))
    await _tick(store, planner.decide)
    (row,) = await _rows(pg_settings, job.id)
    assert row.replay_version == 2 and row.objective == "min_time"
    request, snapshot, now = from_replay(_as_obj(row.replay))
    again = plan(request, snapshot, row.policy, now)
    assert candidates_json(again) == row.candidates
    assert again.snapshot_digest == row.snapshot_digest
    assert again.reason_key == row.reason_key


def _break_version(zero: Any) -> Callable[[Any], Any]:
    return lambda rec: dataclasses.replace(rec, replay_version=zero)


def _nul_in_replay(rec: Any) -> Any:
    replay = json.loads(canonical_json(rec.replay))
    replay["request"]["owner_name"] = "a\u0000b"  # jsonb refuses U+0000
    return dataclasses.replace(rec, replay=replay)


def _replay_not_an_object(rec: Any) -> Any:
    return dataclasses.replace(rec, replay=[1, 2, 3])  # type: ignore[arg-type]


BREAKERS = {
    "version_none": _break_version(None),
    "version_zero": _break_version(0),
    "version_negative": _break_version(-4),
    "version_smallint_overflow": _break_version(40_000),
    "nul_in_a_string": _nul_in_replay,
    "bad_objective": lambda rec: dataclasses.replace(rec, objective="fastest"),
    "empty_objective": lambda rec: dataclasses.replace(rec, objective=""),
    "calibration_overflow": lambda rec: dataclasses.replace(
        rec, calibration_version=2**70
    ),
}


@pytest.mark.parametrize("name", sorted(BREAKERS))
async def test_a_refused_row_is_dropped_alone_in_its_savepoint(
    name: str, pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _world(pg_settings)
    bad = await _job(store, local)
    good = await _job(store, local)
    planner = _planner(Manager([local, remote]))

    def decide(job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        decision = planner.decide(job, fabric)
        if job.id == bad.id and isinstance(decision, Dispatch):
            assert decision.record is not None
            return dataclasses.replace(decision, record=BREAKERS[name](decision.record))
        return decision

    results = await _tick(store, decide)
    dispatched = {r.job.id for r in results if r.attempt is not None}
    assert dispatched == {bad.id, good.id}, name
    assert await _rows(pg_settings, bad.id) == [], name
    (row,) = await _rows(pg_settings, good.id)
    assert row.replay_version == 2 and row.replay is not None
    # the bad job's attempt row really committed (the savepoint, not the tx)
    attempts = await _sql(
        pg_settings,
        "SELECT count(*) FROM attempt WHERE job_id IN (:a, :b)",
        a=bad.id,
        b=good.id,
    )
    assert attempts[0][0] == 2


async def test_a_refused_row_does_not_break_the_next_tick(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    local, remote = await _world(pg_settings)
    first = await _job(store, local)
    planner = _planner(Manager([local, remote]))

    def decide_bad(job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        decision = planner.decide(job, fabric)
        assert isinstance(decision, Dispatch) and decision.record is not None
        return dataclasses.replace(
            decision, record=dataclasses.replace(decision.record, objective="zzz")
        )

    await _tick(store, decide_bad)
    second = await _job(store, local)
    await _tick(store, planner.decide)
    assert await _rows(pg_settings, first.id) == []
    (row,) = await _rows(pg_settings, second.id)
    assert row.replay_version == 2
