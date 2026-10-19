"""V0.9 G5 lane A tester r1 on a real PostgreSQL: row-filter and ordering edge
cases the lane's own file does not pin (JSON-null / empty-string tags, NULL
condition_measured, other campaigns, insertion order, attempt-order ties,
concurrent exports)."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5a_labels_db import (  # noqa: F401 - fixtures + helpers
    NODE_A,
    NODE_B,
    NODE_C,
    WINNER,
    by_node,
    engine,
    finish,
    insert_records,
    labels_of,
    make_campaign,
    record,
)

from meshforge.controller.bench.store import BenchStore
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.labels_export import labels_parquet_bytes
from meshforge.controller.telemetry.labels_store import label_rows

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(180)]

SLOW = 1_000_000


async def _seed(engine: AsyncEngine) -> tuple[str, Any]:
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(engine, runs, WINNER)
    return cid, runs


@pytest.mark.parametrize(
    "measured",
    [
        {"bench_role": ""},
        {"bench_role": "filler", "verified": True},
        {"bench_role": 0},
        {"bench_role": False},
    ],
)
async def test_any_non_null_bench_role_tag_drops_the_row(
    engine: AsyncEngine,
    measured: dict[str, Any],
) -> None:
    cid, runs = await _seed(engine)
    rep1 = by_node(runs)[NODE_A][0]
    await insert_records(
        engine,
        [record(rep1, SLOW, job_id="job_tag", at_s=999, condition_measured=measured)],
    )
    rows = await label_rows(engine, cid, limit=100)
    assert len(rows) == 9 and all(r["total_ms"] < SLOW for r in rows)


@pytest.mark.parametrize(
    "measured",
    [None, {}, {"bench_role": None}, {"verified": None}, {"verified": True}],
)
async def test_untagged_or_null_condition_measured_rows_do_count(
    engine: AsyncEngine, measured: dict[str, Any] | None
) -> None:
    """A real measurement must not be lost because condition_measured is
    NULL / empty / has a JSON-null tag (the filter must not use `!=` on NULL)."""
    cid, runs = await make_campaign(BenchStore(engine))
    await finish(
        engine,
        [r for r in runs if r.node_id != NODE_A],
        {NODE_B: WINNER[NODE_B], NODE_C: WINNER[NODE_C]},
    )
    a = by_node(runs)[NODE_A]
    await insert_records(
        engine,
        [
            record(
                run,
                100 + i,
                job_id=f"job_a{i}",
                at_s=10 + i,
                condition_measured=measured,
            )
            for i, run in enumerate(a)
        ],
    )
    async with create_session_factory(engine)() as db, db.begin():
        for i, run in enumerate(a):
            from meshforge.controller.store.models.bench import BenchRun

            await db.execute(
                update(BenchRun)
                .where(BenchRun.id == run.id)
                .values(state="done", job_id=f"job_a{i}")
            )
    rows = await label_rows(engine, cid, limit=100)
    assert sum(1 for r in rows if r["node_id"] == NODE_A) == 3
    assert (await labels_of(engine, cid)).groups[0].label == "best"


async def test_other_campaigns_and_non_campaign_records_never_leak(
    engine: AsyncEngine,
) -> None:
    cid, runs = await _seed(engine)
    other_cid, other_runs = await make_campaign(BenchStore(engine))
    rep1 = by_node(runs)[NODE_A][0]
    await insert_records(
        engine,
        [
            record(by_node(other_runs)[NODE_A][0], 1, job_id="job_other", at_s=999),
            record(rep1, 1, job_id="job_nocamp", at_s=998, campaign_id=None),
        ],
    )
    rows = await label_rows(engine, cid, limit=100)
    assert len(rows) == 9 and min(r["total_ms"] for r in rows) == 100
    assert len(await label_rows(engine, other_cid, limit=100)) == 1


async def test_labels_do_not_depend_on_insertion_order(engine: AsyncEngine) -> None:
    cid, runs = await _seed(engine)
    # a retried repetition -> two ok rows; insert the newer one FIRST (lower pk order)
    rep1 = by_node(runs)[NODE_B][0]
    await insert_records(engine, [record(rep1, 150, job_id="job_new", at_s=700)])
    await insert_records(engine, [record(rep1, 999, job_id="job_old", at_s=300)])
    first = labels_parquet_bytes(await labels_of(engine, cid))
    # delete and re-insert everything in the reverse physical order
    async with create_session_factory(engine)() as db, db.begin():
        rows = list((await db.execute(RunRecord.__table__.select())).mappings().all())
        await db.execute(delete(RunRecord))
        await db.execute(
            RunRecord.__table__.insert(), [dict(r) for r in reversed(rows)]
        )
    again = labels_parquet_bytes(await labels_of(engine, cid))
    assert first == again
    kept = [
        r
        for r in await label_rows(engine, cid, limit=100)
        if r["config_id"] == rep1.config_id and r["repetition"] == 1
    ]
    assert [r["total_ms"] for r in kept] == [150]


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
async def test_identical_creation_time_and_attempt_no_is_broken_by_attempt_id(
    engine: AsyncEngine, order: tuple[int, int]
) -> None:
    cid, runs = await _seed(engine)
    rep1 = by_node(runs)[NODE_C][0]
    twins = [
        record(rep1, 111, job_id="job_twin1", at_s=800, attempt_id="att_zz_a"),
        record(rep1, 222, job_id="job_twin2", at_s=800, attempt_id="att_zz_b"),
    ]
    await insert_records(engine, [twins[i] for i in order])
    kept = [
        r
        for r in await label_rows(engine, cid, limit=100)
        if r["config_id"] == rep1.config_id and r["repetition"] == 1
    ]
    assert [r["total_ms"] for r in kept] == [222]


async def test_concurrent_label_reads_agree(engine: AsyncEngine) -> None:
    cid, _ = await _seed(engine)
    outs = await asyncio.gather(*(labels_of(engine, cid) for _ in range(8)))
    blobs = {labels_parquet_bytes(o) for o in outs}
    assert len(blobs) == 1


async def test_a_campaign_with_no_records_is_all_pending_and_exports(
    engine: AsyncEngine,
) -> None:
    cid, _ = await make_campaign(BenchStore(engine))
    labels = await labels_of(engine, cid)
    assert [g.label for g in labels.groups] == ["pending"]
    assert labels.rows == 0
    assert labels_parquet_bytes(labels)[:4] == b"PAR1"


async def test_old_attempt_time_with_new_wallclock_does_not_win(
    engine: AsyncEngine,
) -> None:
    """'Latest' is by the attempt's creation time, not by recorded_at."""
    cid, runs = await _seed(engine)
    rep1 = by_node(runs)[NODE_A][0]
    late_record_of_old_attempt = record(rep1, 4242, job_id="job_late", at_s=1)
    late_record_of_old_attempt["recorded_at"] = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    await insert_records(engine, [late_record_of_old_attempt])
    kept = [
        r
        for r in await label_rows(engine, cid, limit=100)
        if r["config_id"] == rep1.config_id and r["repetition"] == 1
    ]
    assert [r["total_ms"] for r in kept] == [100]
