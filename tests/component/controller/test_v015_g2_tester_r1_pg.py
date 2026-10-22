"""V0.15 G2 tester round 1 on a real PostgreSQL: ``node_failures`` is exactly
the D296 predicate (every attempt state x failure type, against the planner's
own ``classify_end``), and the D355 watermark moves when a non-lead attempt
(a lane / combine / cancelled sibling) ends later."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

PHASES = {"stage": 4_000, "env": 20_000, "run": 75_000, "collect": 200}
MANIFEST = {"name": "g2t", "entrypoint": ["python", "main.py"]}

# --- the G1 component test's dispatch helpers (kept local: a test module is
# not a library)


@dataclass
class _Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class _Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: _Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> _Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


async def _sql(engine: Any, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


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


async def _dispatch_one(
    store: ExecutionStore, planner: DispatchPlanner, fingerprint: str
) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g2",
        manifest=MANIFEST,
        bundle_digest="sha256:" + "cd" * 32,
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
    engine: Any, attempt_id: str, at: int, phases: dict[str, int]
) -> None:
    await _sql(
        engine,
        "UPDATE attempt SET state = 'done', ended_at = timestamptz "
        "'2026-10-22 00:00:00+00' + make_interval(secs => :at) WHERE id = :a",
        a=attempt_id,
        at=at,
    )
    for phase, ms in phases.items():
        await _sql(
            engine,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, false)",
            a=attempt_id,
            p=phase,
            ms=ms,
        )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


def _planner(nodes: list[str]) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=30.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def _ids(engine: Any, n: int) -> list[str]:
    store = ExecutionStore(engine)
    planner = _planner([await _node(engine)])
    return [await _dispatch_one(store, planner, f"{i + 500:064x}") for i in range(n)]


async def test_node_failures_is_the_d296_predicate_over_every_state_and_type(
    engine: Any,
) -> None:
    from meshforge.common.errors import FailureType
    from meshforge.common.states import AttemptState
    from meshforge.controller.ml import loader
    from meshforge.controller.planner.risk import EndClass, classify_end

    combos: list[tuple[str, str | None]] = [(s.value, None) for s in AttemptState]
    for s in (AttemptState.FAILED, AttemptState.LOST, AttemptState.DONE):
        combos.extend((s.value, f.value) for f in FailureType)
    combos.append(("failed", "no_such_type"))
    expected = 0
    applied = 0
    ids = await _ids(engine, 1)
    store = ExecutionStore(engine)
    planner = _planner([await _node(engine)])
    for i, (state, ftype) in enumerate(combos):
        att = (
            ids[0] if i == 0 else await _dispatch_one(store, planner, f"{i + 900:064x}")
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE attempt SET state = :s, failure_type = :f, "
                        "ended_at = now() WHERE id = :a"
                    ),
                    {"s": state, "f": ftype, "a": att},
                )
        except Exception:  # noqa: BLE001, S112 - the schema refuses the combination
            continue
        applied += 1
        if classify_end(state, ftype) is EndClass.NODE:
            expected += 1
    assert applied > 10
    assert expected > 0
    assert await loader.count_node_failures(engine, since=None) == expected
    # `since` cuts by ended_at
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    assert await loader.count_node_failures(engine, since=future) == 0


async def test_a_sibling_attempt_ending_later_moves_the_watermark(engine: Any) -> None:
    from meshforge.controller.ml import loader

    (att,) = await _ids(engine, 1)
    before = await loader.read_watermark(engine, max_runs=100)
    assert before == await loader.read_watermark(engine, max_runs=100)  # stable
    assert before != await loader.read_watermark(engine, max_runs=99)  # cap in key
    await _sql(
        engine,
        "UPDATE attempt SET state = 'fenced', ended_at = now() WHERE id = :a",
        a=att,
    )
    after = await loader.read_watermark(engine, max_runs=100)
    assert after != before
    assert after.matched_runs == before.matched_runs  # the matched set did not move
