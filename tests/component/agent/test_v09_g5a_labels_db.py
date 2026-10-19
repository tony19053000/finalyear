"""V0.9 G5 lane A exit test: labels on a real PostgreSQL with seeded run records.

A 3-config group (cpu.sort: local on A, mode1 on B, mode1 on C; 3 repetitions)
is stored through :class:`BenchStore`, its runs moved to ``done`` / ``failed``
and records inserted directly - the label query and rule are under test, not
the execution path (the API file runs real jobs). Scenarios: clear winner ->
best; overlapping -> tie; one config n = 2 -> insufficient; a run not finished
-> pending; and **noise records** that must be dropped by the row filter
(filler, warm-up, ``condition_failed``, infrastructure failure, workload
failure, self-check failed, unverified condition), each newer than the real
record of the same (config, repetition) and wildly slower, so a filter clause
that goes missing changes the label (D244 mutation guard).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.campaign import NodeRef, expand, parse_spec, spec_digest
from meshforge.common.ids import new_campaign_id
from meshforge.controller.bench.store import BenchStore, RunRow
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.bench import BenchRun
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry.labels import build_labels
from meshforge.controller.telemetry.labels_export import labels_parquet_bytes
from meshforge.controller.telemetry.labels_store import (
    LabelRowsLimitError,
    label_rows,
    planned_runs,
)
from meshforge.controller.telemetry.schema import COLUMN_NAMES, SCHEMA_VERSION

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]

NODE_A, NODE_B, NODE_C = (f"node_01J00000000000000000000{x}AA" for x in "ABC")
NODES = [NodeRef(NODE_A), NodeRef(NODE_B), NodeRef(NODE_C)]
SPEC: dict[str, Any] = {
    "name": "labels-test",
    "seed": 11,
    "families": [{"family": "cpu.sort"}],
    "strategies": ["local", "mode1"],
    "origin": NODE_A,
    "repetitions": 3,
    "budget": {"max_runs": 100},
}
T0 = dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.UTC)
_N = itertools.count(1)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def make_campaign(store: BenchStore) -> tuple[str, list[RunRow]]:
    """A campaign of one group x 3 configs x 3 repetitions, all planned."""
    spec = parse_spec(SPEC)
    expansion = expand(spec, NODES)
    campaign_id = new_campaign_id()
    await store.create_campaign(
        campaign_id=campaign_id,
        name=spec.name,
        spec_yaml="name: labels-test\n",
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expansion,
        nodes=[n.node_id for n in NODES],
        created_by=None,
    )
    runs = await store.runs(campaign_id, limit=100)
    assert len(runs) == 9 and len({r.config_id for r in runs}) == 3
    return campaign_id, runs


def by_node(runs: Sequence[RunRow]) -> dict[str, list[RunRow]]:
    out: dict[str, list[RunRow]] = {}
    for run in sorted(runs, key=lambda r: r.repetition):
        out.setdefault(run.node_id, []).append(run)
    return out


def record(
    run: RunRow, total_ms: int, *, job_id: str, at_s: int, **extra: Any
) -> dict[str, Any]:
    n = next(_N)
    row: dict[str, Any] = dict.fromkeys(COLUMN_NAMES)
    row.update(
        schema_version=SCHEMA_VERSION,
        attempt_id=f"att_lbl{n:08d}",
        job_id=job_id,
        attempt_no=1,
        campaign_id=run.campaign_id,
        group_id=run.group_id,
        config_id=run.config_id,
        repetition=run.repetition,
        node_id=run.node_id,
        submitter_node_id=run.origin_node_id,
        strategy=run.strategy,
        placement_mode="pinned",
        workload_family="cpu.sort",
        workload_status="ok",
        condition="none",
        condition_measured={"verified": True, "reason": None, "session_label": None},
        total_ms=total_ms,
        compute_ns=total_ms * 1_000_000 // 2,
        compute_iterations=4,
        bytes_in=1000,
        bytes_out=10,
        planner_node_id=NODE_B,
        attempt_state="done",
        failure_class="ok",
        attempt_created_at=T0 + dt.timedelta(seconds=at_s),
        attempt_ended_at=T0 + dt.timedelta(seconds=at_s + 1),
        recorded_at=T0 + dt.timedelta(seconds=at_s + 2),
    )
    row.update(extra)
    return row


async def finish(
    engine: AsyncEngine,
    runs: Sequence[RunRow],
    totals: dict[str, Sequence[int | None]],
) -> list[dict[str, Any]]:
    """Move each node's runs to done (a total) or failed (None) with a record
    each: an ok one for done, an infrastructure failure for failed."""
    records: list[dict[str, Any]] = []
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        for node, node_runs in by_node(runs).items():
            for run, total in zip(node_runs, totals[node], strict=True):
                job_id = f"job_lbl{next(_N):08d}"
                await db.execute(
                    update(BenchRun)
                    .where(BenchRun.id == run.id)
                    .values(
                        state="done" if total is not None else "failed", job_id=job_id
                    )
                )
                if total is None:
                    rec = record(
                        run,
                        0,
                        job_id=job_id,
                        at_s=100,
                        attempt_state="lost",
                        failure_class="infrastructure",
                        failure_type="node_lost",
                    )
                    rec["total_ms"] = None
                else:
                    rec = record(run, total, job_id=job_id, at_s=100 + run.repetition)
                records.append(rec)
        if records:
            await db.execute(insert(RunRecord), records)
    return records


async def insert_records(engine: AsyncEngine, rows: list[dict[str, Any]]) -> None:
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(insert(RunRecord), rows)


async def labels_of(engine: AsyncEngine, campaign_id: str) -> Any:
    runs = await planned_runs(engine, campaign_id)
    rows = await label_rows(engine, campaign_id, limit=10_000)
    return build_labels(campaign_id, runs, rows)


WINNER = {NODE_A: [100, 101, 102], NODE_B: [200, 201, 202], NODE_C: [300, 301, 302]}


async def test_clear_winner_is_best(engine: AsyncEngine) -> None:
    store = BenchStore(engine)
    cid, runs = await make_campaign(store)
    await finish(engine, runs, WINNER)
    labels = await labels_of(engine, cid)
    (group,) = labels.groups
    assert group.label == "best", group
    assert (group.best_node_id, group.best_strategy) == (NODE_A, "local")
    assert group.margin_pct == pytest.approx((201 - 101) / 101 * 100)
    assert group.planner_node_id == NODE_B and group.planner_agrees is False
    assert labels.rows == 9
    for config in group.configs:
        per_iter = config.metrics["compute_per_iter_ns"]
        total = config.metrics["total_ms"]
        assert per_iter is not None and total is not None
        assert per_iter.median == pytest.approx(total.median * 1e6 / 2 / 4)
        assert config.metrics["bytes"].median == 1010.0  # type: ignore[union-attr]


async def test_overlapping_is_a_tie(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(
        engine,
        runs,
        {NODE_A: [100, 150, 200], NODE_B: [140, 160, 180], NODE_C: [500, 501, 502]},
    )
    (group,) = (await labels_of(engine, cid)).groups
    assert group.label == "tie"
    ids = {c.node_id: c.config_id for c in group.configs}
    assert group.tied_config_ids == tuple(sorted([ids[NODE_A], ids[NODE_B]]))


async def test_one_config_with_two_repetitions_is_insufficient(
    engine: AsyncEngine,
) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(
        engine,
        runs,
        {NODE_A: [100, 101, 102], NODE_B: [200, 201, 202], NODE_C: [300, 301, None]},
    )
    (group,) = (await labels_of(engine, cid)).groups
    assert group.label == "insufficient"
    c = next(x for x in group.configs if x.node_id == NODE_C)
    assert c.repetitions == 2


async def test_a_run_still_running_is_pending(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    finished = [r for r in runs if not (r.node_id == NODE_C and r.repetition == 3)]
    open_run = next(r for r in runs if r.node_id == NODE_C and r.repetition == 3)
    await finish(
        engine,
        finished,
        {NODE_A: [100, 101, 102], NODE_B: [200, 201, 202], NODE_C: [300, 301]},
    )
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(
            update(BenchRun)
            .where(BenchRun.id == open_run.id)
            .values(state="submitted", job_id="job_lblrunning")
        )
    (group,) = (await labels_of(engine, cid)).groups
    assert group.label == "pending"
    # it ends, but its record is not written yet -> still pending
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(
            update(BenchRun).where(BenchRun.id == open_run.id).values(state="done")
        )
    assert (await labels_of(engine, cid)).groups[0].label == "pending"
    await insert_records(
        engine, [record(open_run, 302, job_id="job_lblrunning", at_s=500)]
    )
    assert (await labels_of(engine, cid)).groups[0].label == "best"


def noise(run: RunRow) -> list[dict[str, Any]]:
    """Records for ``run``'s (config, repetition), each newer than the real one
    and far slower - every one must be dropped by the row filter."""
    slow = 1_000_000
    return [
        # D244: a queue filler / cache warm-up that (wrongly) carries the
        # campaign identity - the bench_role tag alone must drop it
        record(
            run,
            slow,
            job_id="job_lblfiller",
            at_s=900,
            condition=None,
            condition_measured={"bench_role": "filler", "run_id": run.id},
        ),
        record(
            run,
            slow,
            job_id="job_lblwarmup",
            at_s=901,
            condition=None,
            condition_measured={"bench_role": "warmup", "run_id": run.id},
        ),
        record(
            run,
            slow,
            job_id="job_lblcondfail",
            at_s=902,
            attempt_state="failed",
            failure_class="condition_failed",
            condition_measured={
                "verified": False,
                "reason": "rtt",
                "session_label": None,
            },
        ),
        record(
            run,
            slow,
            job_id="job_lblinfra",
            at_s=903,
            attempt_state="lost",
            failure_class="infrastructure",
        ),
        record(
            run,
            slow,
            job_id="job_lblworkload",
            at_s=904,
            attempt_state="failed",
            failure_class="workload",
            workload_status="check_failed",
        ),
        # class ok but the self-check failed / the condition was not verified
        record(
            run, slow, job_id="job_lblcheck", at_s=905, workload_status="check_failed"
        ),
        record(
            run,
            slow,
            job_id="job_lblunverified",
            at_s=906,
            condition_measured={
                "verified": False,
                "reason": "x",
                "session_label": None,
            },
        ),
    ]


async def test_fillers_warmups_and_failures_are_ignored(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    winner_rep1 = by_node(runs)[NODE_A][0]
    clean = labels_parquet_bytes(await labels_of(engine, cid))
    await insert_records(engine, noise(winner_rep1))
    rows = await label_rows(engine, cid, limit=10_000)
    assert len(rows) == 9
    assert all(r["total_ms"] < 1_000_000 for r in rows)
    labels = await labels_of(engine, cid)
    assert labels.groups[0].label == "best"
    assert labels.groups[0].best_node_id == NODE_A
    # the noise changed nothing: the same bytes as before it arrived (D222)
    assert labels_parquet_bytes(labels) == clean


@pytest.mark.parametrize("which", ["filler", "warmup"])
async def test_a_bench_role_row_alone_is_dropped(
    engine: AsyncEngine, which: str
) -> None:
    """Mutation guard for D244: only the bench_role clause can drop this row
    (class ok, self-check ok, no verified=false)."""
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    rep1 = by_node(runs)[NODE_A][0]
    await insert_records(
        engine,
        [
            record(
                rep1,
                1_000_000,
                job_id=f"job_lbl{which}only",
                at_s=999,
                condition_measured={"bench_role": which, "run_id": rep1.id},
            )
        ],
    )
    group = (await labels_of(engine, cid)).groups[0]
    assert group.label == "best" and group.best_node_id == NODE_A


async def test_latest_ok_attempt_per_repetition_wins(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    rep1 = by_node(runs)[NODE_A][0]
    # a later ok retry of the same (config, repetition): it replaces the first
    await insert_records(engine, [record(rep1, 999, job_id="job_lblretry", at_s=950)])
    rows = await label_rows(engine, cid, limit=10_000)
    kept = [
        r for r in rows if r["config_id"] == rep1.config_id and r["repetition"] == 1
    ]
    assert [r["total_ms"] for r in kept] == [999]
    assert (await labels_of(engine, cid)).groups[0].label == "tie"


async def test_unsupported_skipped_config_is_left_out(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    measured = [r for r in runs if r.node_id != NODE_C]
    await finish(engine, measured, {NODE_A: WINNER[NODE_A], NODE_B: WINNER[NODE_B]})
    async with create_session_factory(engine)() as db, db.begin():
        await db.execute(
            update(BenchRun)
            .where(BenchRun.campaign_id == cid, BenchRun.node_id == NODE_C)
            .values(state="skipped", detail="unsupported: no netem capability")
        )
    group = (await labels_of(engine, cid)).groups[0]
    assert group.label == "best" and group.best_node_id == NODE_A
    assert {c.node_id: c.state for c in group.configs}[NODE_C] == "unsupported"


async def test_two_label_exports_are_byte_identical(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    first = labels_parquet_bytes(await labels_of(engine, cid))
    second = labels_parquet_bytes(await labels_of(engine, cid))
    assert first == second


async def test_the_row_cap_is_enforced(engine: AsyncEngine) -> None:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    with pytest.raises(LabelRowsLimitError):
        await label_rows(engine, cid, limit=8)
    assert len(await label_rows(engine, cid, limit=9)) == 9
