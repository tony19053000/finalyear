"""V0.9 G5c exit test (0.9.8, D220): the data quality report on a real
PostgreSQL with a seeded campaign - every number asserted.

One group x 3 configs x 3 repetitions (lane A's seeding helpers):

* A (local @ node A) - clean: 100, 101, 102;
* B (mode1 @ node B) - high CV **and** an outlier: 100, 102, 5000;
* C (mode1 @ node C) - one infrastructure failure (n = 2) and one record with
  ``bytes_out`` missing;

plus noise: an ``owner_preempted`` attempt of A (requeued), a ``condition_failed``
record of C, and a **filler** of B with a huge ``total_ms``. Checked through
:func:`campaign_quality` (the route's read): per-config numbers, the summary,
``pending`` then the verdicts, byte-identical JSON twice (D222), the labels'
medians, and the status's infrastructure failure rate.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5a_labels_db import (  # type: ignore[import-not-found]
    NODE_A,
    NODE_B,
    NODE_C,
    by_node,
    engine,  # noqa: F401 - the fixture
    finish,
    insert_records,
    labels_of,
    make_campaign,
    record,
)

from meshforge.controller.api.bench_quality_models import quality_out
from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.telemetry.quality_store import campaign_quality

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]

TOTALS = {NODE_A: [100, 101, 102], NODE_B: [100, 102, 5000], NODE_C: [None, 300, 301]}


async def _seed(eng: AsyncEngine) -> tuple[str, dict[str, Any]]:
    store = BenchStore(eng)
    cid, runs = await make_campaign(store)
    records = await finish(eng, runs, TOTALS)
    nodes = by_node(runs)
    a0, b1, c1, c2 = (
        nodes[NODE_A][0],
        nodes[NODE_B][1],
        nodes[NODE_C][1],
        nodes[NODE_C][2],
    )
    # C's second ok record has no bytes_out (a missing field); the helpers
    # leave phase timings and the start snapshot empty: fill them in first
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "UPDATE run_record SET queue_ms = 2, run_ms = 50, "
                "node_state = 'online', node_cpu_percent = 3.0 "
                "WHERE campaign_id = :cid"
            ),
            {"cid": cid},
        )
        await conn.execute(
            text(
                "UPDATE run_record SET bytes_out = NULL "
                "WHERE config_id = :c AND repetition = :r AND failure_class = 'ok'"
            ),
            {"c": c1.config_id, "r": c1.repetition},
        )
    noise = [
        record(
            a0,
            0,
            job_id="job_preempted0001",
            at_s=10,
            attempt_state="failed",
            failure_class="infrastructure",
            failure_type="owner_preempted",
        ),
        record(
            c2,
            0,
            job_id="job_condfail00001",
            at_s=11,
            attempt_state="failed",
            failure_class="condition_failed",
            failure_type="config_error",
            condition="cpu_load:1",
        ),
        record(
            b1,
            9_999_999,
            job_id="job_filler0000001",
            at_s=500,
            condition_measured={"bench_role": "filler", "verified": True},
        ),
    ]
    for failed in noise[:2]:
        failed["total_ms"] = None
    await insert_records(eng, noise)
    by_total = {r["total_ms"]: r for r in records if r["total_ms"] is not None}
    configs = {
        "A": nodes[NODE_A][0].config_id,
        "B": nodes[NODE_B][0].config_id,
        "C": nodes[NODE_C][0].config_id,
        "outlier": by_total[5000]["attempt_id"],
    }
    return cid, configs


async def test_every_number_of_a_seeded_campaign(engine: AsyncEngine) -> None:  # noqa: F811
    cid, ids = await _seed(engine)
    open_q = await campaign_quality(engine, cid, campaign_state="active")
    assert open_q.summary.pending
    assert (
        open_q.summary.crit1_infra_ok is None and open_q.summary.crit3_reps_ok is None
    )

    q = await campaign_quality(engine, cid, campaign_state="done")
    by = {c.config_id: c for c in q.configs}
    a, b, c = by[ids["A"]], by[ids["B"]], by[ids["C"]]
    assert [x.config_id for x in q.configs] == sorted(by)

    assert (a.planned, a.done, a.n, a.node_id, a.strategy) == (3, 3, 3, NODE_A, "local")
    assert a.total_ms is not None and a.total_ms.median == 101.0
    assert a.total_ms.cv is not None and a.total_ms.cv < 0.10
    assert not a.flagged and a.outlier_attempt_ids == ()
    assert a.compute_per_iter_ns is not None and a.compute_per_iter_ns.n == 3

    assert b.n == 3 and b.cv_flag and not b.reps_flag and b.flagged
    assert b.total_ms is not None and b.total_ms.median == 102.0  # filler ignored
    assert b.outlier_attempt_ids == (ids["outlier"],)

    assert (c.planned, c.done, c.n) == (3, 2, 2)
    assert c.reps_flag and c.flagged
    assert c.missing["bytes_out"] == 1 and c.records_checked == 2
    assert sum(c.missing.values()) == 1

    s = q.summary
    assert s.runs_by_state == {
        "planned": 0,
        "submitted": 0,
        "done": 8,
        "failed": 1,
        "skipped": 0,
    }
    assert s.records == 11  # 9 + owner_preempted + condition_failed; filler apart
    assert s.bench_role_records == {"filler": 1}
    assert (s.infra_failures, s.owner_preempted) == (1, 1)
    assert s.infra_failure_rate == 0.1  # 1 / (11 - 1)
    assert s.condition_failures == {"cpu_load:1": 1}
    assert s.workload_failures == {}
    assert s.crit1_infra_ok is False and s.crit3_reps_ok is False
    assert (s.configs, s.flagged_configs, s.cv_threshold) == (3, 2, 0.10)

    # the same rate as `compute bench status` (one helper, D254)
    record_ = await BenchStore(engine).get_campaign(cid)
    assert record_ is not None
    status = await CampaignControl(engine).status(cid, record_.counts)
    assert status.infra_failure_rate == s.infra_failure_rate
    assert (status.records, status.preempted) == (s.records, s.owner_preempted)

    # the labels' medians for the same configurations
    labels = await labels_of(engine, cid)
    lab = {x.config_id: x for g in labels.groups for x in g.configs}
    for conf in q.configs:
        mine, theirs = conf.total_ms, lab[conf.config_id].metrics["total_ms"]
        assert mine is not None and theirs is not None
        assert (mine.median, mine.ci_low, mine.ci_high, mine.n) == (
            theirs.median,
            theirs.ci_low,
            theirs.ci_high,
            theirs.n,
        )


async def test_two_reports_are_byte_identical(engine: AsyncEngine) -> None:  # noqa: F811
    cid, _ids = await _seed(engine)
    first = quality_out(await campaign_quality(engine, cid, campaign_state="done"))
    second = quality_out(await campaign_quality(engine, cid, campaign_state="done"))
    assert first.model_dump_json() == second.model_dump_json()
    assert first.quality_version == 1


async def test_an_empty_campaign(engine: AsyncEngine) -> None:  # noqa: F811
    cid, _runs = await make_campaign(BenchStore(engine))
    q = await campaign_quality(engine, cid, campaign_state="active")
    assert q.summary.records == 0 and q.summary.pending
    assert q.summary.runs_by_state["planned"] == 9
    assert all(c.n == 0 and c.reps_flag for c in q.configs) and len(q.configs) == 3
