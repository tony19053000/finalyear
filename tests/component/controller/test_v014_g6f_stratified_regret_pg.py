"""V0.14 G6F: D329 end to end on a real PostgreSQL - campaigns run one after
another still give the D319 regret check enough groups.

Four campaigns (2 nodes, ``local`` + ``mode1``, 3 repetitions, 3 groups each)
run **sequentially** through the real dispatcher: every run of campaign k ends
before campaign k + 1 starts. Each group is its own workload (fingerprint),
so 12 labelled best/tie groups over 12 fingerprints, 6 matched runs each.
The economics are the G4 regret-gate test's (``_seed`` there): the planner's
pick is the measured best, but its ``run`` phase is 10x its guess.

* ``CalibrationStore.refresh`` -> the D319 check is **not** skipped:
  ``regret_groups`` >= 10 (the stratified slice holds the newest run of
  every fingerprint);
* the same matched runs under the **old** newest-overall slice (the pre-D329
  rule, recomputed here - not by changing ``src``) reach only the last
  campaign: fewer than 10 groups, so the check would have been skipped;
* the stratified slice recomputed here gives exactly the stored count.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from mf_eval_pg import (
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    preferred_node,
    register_nodes,
)
from sqlalchemy import select, text

from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import (
    HOLDOUT_FRAC,
    MIN_RUNS,
    Calibration,
    CalibrationConfig,
    MatchedRun,
    split_holdout,
)
from meshforge.controller.planner_eval.loader import load_for_jobs
from meshforge.controller.planner_eval.regret_gate import MIN_GROUPS, regret_check
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

CFG = CalibrationConfig()
T0 = dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.UTC)
CAMPAIGNS = 4
GROUPS_PER_CAMPAIGN = 3
MAX_ROWS = 100_000


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


def _fingerprint(campaign: int, group: int) -> str:
    return f"{campaign * 100 + group + 1:064x}"


async def _set_fingerprints(engine: Any, campaign_id: str, campaign: int) -> None:
    """One workload per group: its run records and jobs get their own
    fingerprint (the helper writes one for every run)."""
    async with engine.begin() as conn:
        groups = [
            str(g)
            for g in (
                await conn.execute(
                    text(
                        "SELECT DISTINCT group_id FROM run_record WHERE "
                        "campaign_id = :c ORDER BY group_id"
                    ),
                    {"c": campaign_id},
                )
            ).scalars()
        ]
        assert len(groups) == GROUPS_PER_CAMPAIGN
        for g, group_id in enumerate(groups):
            fp = _fingerprint(campaign, g)
            await conn.execute(
                text(
                    "UPDATE run_record SET fingerprint = :f WHERE campaign_id = :c "
                    "AND group_id = :g"
                ),
                {"f": fp, "c": campaign_id, "g": group_id},
            )
            await conn.execute(
                text(
                    "UPDATE job SET fingerprint = :f WHERE id IN (SELECT job_id "
                    "FROM run_record WHERE campaign_id = :c AND group_id = :g)"
                ),
                {"f": fp, "c": campaign_id, "g": group_id},
            )


async def _seed_sequential(engine: Any) -> None:
    nodes = await register_nodes(engine, 2)
    picked: str | None = None
    for k in range(CAMPAIGNS):
        first = 20 + k * GROUPS_PER_CAMPAIGN
        campaign_id, runs = await make_campaign(
            engine, nodes, list(range(first, first + GROUPS_PER_CAMPAIGN))
        )
        dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
        if picked is None:
            picked = await preferred_node(engine, dispatched[0])
        best = picked

        def total(run: Any, best: str = best) -> int:  # the pick is the best
            return (1_000 if run.node_id == best else 4_000) + run.repetition

        def run_ms(run: Any, best: str = best) -> int:  # ... 10x the 60 s guess
            return 600_000 if run.node_id == best else 60_000

        start = T0 + dt.timedelta(hours=k)  # campaign k ends before k + 1
        await finish_runs(
            engine,
            dispatched,
            total,
            run_ms=run_ms,
            ended_at=lambda _d, i, start=start: start + dt.timedelta(seconds=i),
        )
        await _set_fingerprints(engine, campaign_id, k)


def _old_newest_overall(runs: Sequence[MatchedRun]) -> list[MatchedRun]:
    """The pre-D329 held-out slice (D306/D319 as G4 shipped it): the newest
    ``HOLDOUT_FRAC`` of all matched runs, at least one."""
    ordered = sorted(runs, key=lambda r: (r.finished_at, r.decision_id, r.attempt_id))
    if len(ordered) < MIN_RUNS:
        return []
    return ordered[-max(1, int(len(ordered) * HOLDOUT_FRAC)) :]


async def _slice_groups(
    engine: Any, held: Sequence[MatchedRun], new: Calibration
) -> int:
    """The D319 group count of a held-out slice, through the product's own
    read and check (``CalibrationStore._with_regret_check``'s path)."""
    ids = sorted({r.decision_id for r in held})
    async with engine.connect() as conn:
        jobs = set(
            (
                await conn.execute(
                    select(PlanDecision.job_id).where(PlanDecision.id.in_(ids))
                )
            ).scalars()
        )
    inputs = await load_for_jobs(engine, jobs, max_rows=MAX_ROWS)
    check = regret_check(inputs.groups, inputs.records, new, None)
    return int(check.fit_inputs()["regret_groups"])


async def test_sequential_campaigns_still_reach_the_regret_check(engine: Any) -> None:
    await _seed_sequential(engine)
    store = CalibrationStore(engine)
    runs = await store.matched_runs(CFG)
    total_groups = CAMPAIGNS * GROUPS_PER_CAMPAIGN
    assert len(runs) == total_groups * 6
    fingerprints = {r.fingerprint for r in runs}
    assert len(fingerprints) == total_groups and None not in fingerprints
    assert all(sum(r.fingerprint == f for r in runs) == 6 for f in fingerprints)

    result = await store.refresh(CFG)
    assert result.row is not None, result
    row = result.row
    inputs = row.fit_inputs
    # D329: the check ran - never "skipped (too_few_groups)"
    assert inputs["regret_check"] in {"passed", "rejected"}, row
    assert inputs["regret_groups"] >= MIN_GROUPS == 10
    assert not (row.reason or "").startswith("regret check skipped")
    # the G4 economics: calibrated picks are worse -> rejected on regret
    assert row.status == "rejected" and inputs["regret_check"] == "rejected", row

    # the stratified slice: newest run of every fingerprint -> every group
    _fit, held = split_holdout(runs)
    assert {r.fingerprint for r in held} == fingerprints
    assert row.calibration is not None
    stratified = await _slice_groups(engine, held, row.calibration)
    assert stratified == inputs["regret_groups"] == total_groups

    # the same data under the old newest-overall slice: last campaign only
    old = _old_newest_overall(runs)
    assert len(old) == int(len(runs) * HOLDOUT_FRAC)
    newest_campaign = max(r.finished_at for r in runs) - dt.timedelta(hours=1)
    assert all(r.finished_at > newest_campaign for r in old)
    old_groups = await _slice_groups(engine, old, row.calibration)
    assert old_groups == GROUPS_PER_CAMPAIGN
    assert old_groups < MIN_GROUPS  # -> the check would have been skipped
