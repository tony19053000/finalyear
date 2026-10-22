"""V0.14 G6F tester round 1: D332 live-node counting under hostile snapshots."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

from alembic import command
from mf_eval_pg import (
    dispatch_runs,
    finish_runs,
    make_campaign,
    medians,
    planner,
    register_nodes,
)
from sqlalchemy import text

from meshforge.cli.planner_commands import latency_lines
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.loader import load_latency
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _exec(engine: Any, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


def _service(engine: Any) -> EvalService:
    return EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0)),
        max_rows=10_000,
    )


async def _campaign(engine: Any) -> tuple[str, list[str]]:
    laptops = await register_nodes(engine, 3)
    cid, runs = await make_campaign(engine, laptops, [20, 22])
    done = await dispatch_runs(ExecutionStore(engine), planner(laptops), runs)
    await finish_runs(engine, done, medians(dict.fromkeys(laptops, 500)))
    return cid, [d.job_id for d in done]


def _node(i: int, **over: Any) -> dict[str, Any]:
    n: dict[str, Any] = {
        "node_id": f"n{i}",
        "state": "online",
        "connected": True,
        "capabilities": ["exec"],
    }
    n.update(over)
    return n


async def _set_nodes(engine: Any, job: str, nodes: Any) -> None:
    await _exec(
        engine,
        "UPDATE plan_decision SET replay = jsonb_set(replay, '{snapshot,nodes}', "
        "CAST(:n AS jsonb)) WHERE job_id = :j AND outcome = 'dispatched'",
        n=json.dumps(nodes),
        j=job,
    )


async def _live_of(engine: Any, cid: str, nodes: Any, jobs: list[str], k: int) -> Any:
    await _set_nodes(engine, jobs[k], nodes)
    samples = await load_latency(engine, [cid])
    return samples[k]


@pytest.mark.asyncio
async def test_boundary_and_malformed_node_elements(engine: Any) -> None:
    cid, jobs = await _campaign(engine)
    samples0 = await load_latency(engine, [cid])
    assert len(samples0) == len(jobs)

    # exactly 4 live among 10 rows: judged; 3 live + 1 lacking exec: not
    ten = [_node(i) for i in range(4)] + [
        _node(i, state="offline", connected=False) for i in range(4, 10)
    ]
    s = await _live_of(engine, cid, ten, jobs, 0)
    assert (s.node_count, s.live_node_count) == (10, 4)

    four_one_no_exec = [_node(i) for i in range(3)] + [
        _node(3, capabilities=["exec-subprocess"])
    ]
    s = await _live_of(engine, cid, four_one_no_exec, jobs, 1)
    assert (s.node_count, s.live_node_count) == (4, 3)

    cases: dict[str, tuple[Any, int]] = {
        "connected null": ([_node(0, connected=None)], 0),
        "connected 1": ([_node(0, connected=1)], 0),
        "connected 'true'": ([_node(0, connected="true")], 0),
        "state ONLINE": ([_node(0, state="ONLINE")], 0),
        "state null": ([_node(0, state=None)], 0),
        "caps null": ([_node(0, capabilities=None)], 0),
        "caps object": ([_node(0, capabilities={"exec": True})], 0),
        "caps nested": ([_node(0, capabilities=[["exec"]])], 0),
        "caps 'EXEC'": ([_node(0, capabilities=["EXEC"])], 0),
        "keys missing": ([{"node_id": "x"}], 0),
        "empty nodes": ([], 0),
        "nodes null": (None, -1),
        "nodes string": ("nodes", -1),
        "nodes number": (7, -1),
        "nodes bool": (True, -1),
        "one good": ([_node(0)], 1),
    }
    for name, (nodes, expect) in cases.items():
        s = await _live_of(engine, cid, nodes, jobs, 2)
        if expect == -1:
            assert (s.node_count, s.live_node_count) == (None, None), name
        else:
            assert s.live_node_count == expect, name


@pytest.mark.asyncio
async def test_duplicate_node_ids_in_a_snapshot_are_counted_per_element(
    engine: Any,
) -> None:
    """Observation, not a spec: duplicates (cannot occur: one row per node id)
    count per element. Pinned so a change is noticed."""
    cid, jobs = await _campaign(engine)
    s = await _live_of(engine, cid, [_node(0)] * 4, jobs, 0)
    assert (s.node_count, s.live_node_count) == (4, 4)


@pytest.mark.asyncio
async def test_replay_v1_and_null_replay_rows(engine: Any) -> None:
    cid, jobs = await _campaign(engine)
    await _exec(
        engine,
        "UPDATE plan_decision SET replay_version = 1 WHERE job_id = :j",
        j=jobs[0],
    )
    s = (await load_latency(engine, [cid]))[0]
    assert s.live_node_count == 3  # v1 carries the same node keys
    await _exec(
        engine,
        "UPDATE plan_decision SET replay = NULL, replay_version = NULL "
        "WHERE job_id = :j",
        j=jobs[1],
    )
    s = (await load_latency(engine, [cid]))[1]
    assert s.live_node_count is None and s.node_count is not None
    # all rows NULL replay -> nothing judged, never PASS
    await _exec(engine, "UPDATE plan_decision SET replay = NULL, replay_version = NULL")
    report = (await _service(engine).run([cid])).report
    assert report["latency"]["judged"]["decisions"] == 0
    assert report["latency"]["by_live_node_count"].keys() == {"unknown"}
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions"


@pytest.mark.asyncio
async def test_cli_lines_equal_after_the_api_model_and_old_reports_mix(
    engine: Any,
) -> None:
    cid, _jobs = await _campaign(engine)
    new = (await _service(engine).run([cid])).report
    old = json.loads(json.dumps(new))
    for key in ("judged_on", "by_live_node_count"):
        old["latency"].pop(key)
    api_new = EvalReportOut.model_validate(new).model_dump(mode="json")
    api_old = EvalReportOut.model_validate(old).model_dump(mode="json")
    assert api_new["latency"]["judged_on"] == "live_nodes"
    assert (
        api_new["latency"]["by_live_node_count"] == new["latency"]["by_live_node_count"]
    )
    assert api_old["latency"]["judged_on"] == "snapshot_nodes"
    assert api_old["latency"].get("by_live_node_count") in (None, {})
    assert latency_lines(new["latency"]) == latency_lines(api_new["latency"])
    old_lines = latency_lines(api_old["latency"])
    assert not any("by live nodes" in line for line in old_lines)
    assert "snapshots of >= 4 nodes" in old_lines[-1]
    assert "live nodes" in latency_lines(api_new["latency"])[-1]
