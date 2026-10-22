"""V0.14 G6F (D332) on a real PostgreSQL: criterion 3 counts **live** snapshot
nodes, read in SQL from the stored replay.

* real dispatch ticks on a fabric of 7 registered node rows - 3 live laptops
  (online, a live session, ``exec``), 2 stale rows (no live session; one of
  them revoked), 1 connected node that does not accept jobs and a fourth
  laptop offline: every replay holds 7 node rows and 3 live nodes, so
  criterion 3 judges nothing - the G6 rule (rows >= 4) would have judged all;
* a fourth live laptop joins (a second campaign): its decisions have 4 live
  nodes and are judged, the first campaign's still are not;
* hostile replays: a non-array ``nodes``, a NULL replay (rows from the
  candidates, live unknown), elements that are not objects or carry
  ``connected: "true"`` (a string) / ``capabilities: "exec"`` (not a list)
  are not live - never an error;
* the stored report carries both counts (``by_node_count``,
  ``by_live_node_count``) and ``judged_on: live_nodes``; the API model reads it.
"""

from __future__ import annotations

import asyncio
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


async def _campaign(
    engine: Any, laptops: list[str], connected: list[str]
) -> tuple[str, list[str]]:
    """A campaign over ``laptops``, dispatched while ``connected`` have a live
    session (the rest of the registry rows have none)."""
    campaign_id, runs = await make_campaign(engine, laptops, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(connected), runs)
    await finish_runs(engine, dispatched, medians(dict.fromkeys(laptops, 500)))
    return campaign_id, [d.job_id for d in dispatched]


@pytest.mark.asyncio
async def test_stale_rows_and_a_non_exec_node_are_not_live_laptops(
    engine: Any,
) -> None:
    laptops = await register_nodes(engine, 3)
    stale = await register_nodes(engine, 2)
    (no_exec,) = await register_nodes(engine, 1)
    (fourth,) = await register_nodes(engine, 1)
    # a stale row revoked; a connected node whose owner does not accept jobs;
    # the fourth laptop is offline (no session) during campaign 1
    await _exec(
        engine, "UPDATE node SET state = 'revoked' WHERE node_id = :n", n=stale[0]
    )
    await _exec(
        engine,
        "UPDATE node SET hardware = CAST(:h AS jsonb) WHERE node_id = :n",
        h='{"capabilities": ["exec-subprocess"]}',
        n=no_exec,
    )
    await _exec(
        engine, "UPDATE node SET state = 'offline' WHERE node_id = :n", n=fourth
    )

    camp_1, jobs_1 = await _campaign(engine, laptops, [*laptops, no_exec])
    samples = await load_latency(engine, [camp_1])
    assert len(samples) == len(jobs_1) == 18
    assert {(s.node_count, s.live_node_count) for s in samples} == {(7, 3)}

    report_1 = (await _service(engine).run([camp_1])).report
    block = report_1["latency"]
    assert block["judged_on"] == "live_nodes"
    assert list(block["by_node_count"]) == ["7"]
    assert list(block["by_live_node_count"]) == ["3"]
    assert block["by_live_node_count"]["3"]["decisions"] == block["decisions"] == 18
    assert block["judged"] == {"decisions": 0, "p99_ms": None}
    (c3,) = [v for v in report_1["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions" and c3["counted_groups"] == 0
    model = EvalReportOut.model_validate(report_1).latency
    assert model is not None and model.judged_on == "live_nodes"
    assert model.by_live_node_count is not None

    # the fourth laptop comes online with a session: 4 live laptops
    await _exec(engine, "UPDATE node SET state = 'online' WHERE node_id = :n", n=fourth)
    four = sorted([*laptops, fourth])
    camp_2, _jobs_2 = await _campaign(engine, four, [*four, no_exec])
    both = await load_latency(engine, [camp_1, camp_2])
    assert sorted({(s.node_count, s.live_node_count) for s in both}) == [(7, 3), (7, 4)]
    report_2 = (await _service(engine).run([camp_1, camp_2])).report["latency"]
    live_4 = report_2["by_live_node_count"]["4"]["decisions"]
    assert live_4 > 0
    assert report_2["judged"]["decisions"] == live_4
    assert report_2["by_live_node_count"]["3"]["decisions"] == 18


@pytest.mark.asyncio
async def test_hostile_replays_are_not_live_and_never_an_error(engine: Any) -> None:
    laptops = await register_nodes(engine, 3)
    camp, jobs = await _campaign(engine, laptops, laptops)
    odd = (
        '[1, "x", null, [], {}, '
        '{"state": "online", "connected": "true", "capabilities": ["exec"]}, '
        '{"state": "online", "connected": true, "capabilities": "exec"}, '
        '{"state": "ONLINE", "connected": true, "capabilities": ["exec"]}, '
        '{"state": "online", "connected": false, "capabilities": ["exec"]}, '
        '{"state": "online", "connected": true, "capabilities": ["exec", "gpu"]}]'
    )
    set_nodes = (
        "UPDATE plan_decision SET replay = jsonb_set(replay, '{snapshot,nodes}', "
        "CAST(:n AS jsonb)) WHERE job_id = :j AND outcome = 'dispatched'"
    )
    await _exec(engine, set_nodes, n=odd, j=jobs[0])
    await _exec(engine, set_nodes, n='{"a": 1}', j=jobs[1])
    await _exec(
        engine,
        "UPDATE plan_decision SET replay = NULL, replay_version = NULL "
        "WHERE job_id = :j AND outcome = 'dispatched'",
        j=jobs[2],
    )
    samples = await load_latency(engine, [camp])
    assert len(samples) == 18
    pairs = [(s.node_count, s.live_node_count) for s in samples]
    # one row each for the three edited jobs, the rest untouched (3 live)
    assert pairs.count((10, 1)) == 1  # only the last element is live
    assert pairs.count((None, None)) == 1  # nodes not an array
    no_replay = [p for p in pairs if p[1] is None and p[0] is not None]
    assert len(no_replay) == 1 and no_replay[0][0] >= 1  # rows from candidates
    assert pairs.count((3, 3)) == 15
    block = (await _service(engine).run([camp])).report["latency"]
    assert block["by_live_node_count"] == {
        "1": {"decisions": 1, "p99_ms": block["by_live_node_count"]["1"]["p99_ms"]},
        "3": {"decisions": 15, "p99_ms": block["by_live_node_count"]["3"]["p99_ms"]},
        "unknown": {
            "decisions": 2,
            "p99_ms": block["by_live_node_count"]["unknown"]["p99_ms"],
        },
    }
