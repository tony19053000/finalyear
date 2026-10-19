"""V0.9 G5c tester r1: the quality report against a real PostgreSQL with
adversarial record mixes (the labels' own noise set, retries, 10 000 records).

* the report's per-config n / median equals the labels' for the same data,
  with retries and every kind of rejected record present;
* every non-filler record is accounted for somewhere in the summary (a record
  the label filter rejects must not vanish silently);
* 10 000 records: bounded time and memory through the streamed query;
* an empty campaign and a campaign with only fillers.
"""

from __future__ import annotations

import time
import tracemalloc
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from test_v09_g5a_labels_db import (  # type: ignore[import-not-found]
    WINNER,
    by_node,
    engine,  # noqa: F401 - the fixture
    finish,
    insert_records,
    labels_of,
    make_campaign,
    noise,
    record,
)

from meshforge.controller.bench.store import BenchStore
from meshforge.controller.telemetry.quality_store import campaign_quality

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


async def test_report_agrees_with_labels_under_noise_and_retries(engine: Any) -> None:  # noqa: F811
    store = BenchStore(engine)
    cid, runs = await make_campaign(store)
    await finish(engine, runs, WINNER)
    extra: list[dict[str, Any]] = []
    for run in runs:
        extra += noise(run)
    # a retry: a newer ok record for one repetition replaces the older one
    first = by_node(runs)["node_a"][0] if "node_a" in by_node(runs) else runs[0]
    extra.append(record(first, 777, job_id="job_retry000001", at_s=2000, attempt_no=2))
    await insert_records(engine, extra)

    q = await campaign_quality(engine, cid, campaign_state="done")
    labels = await labels_of(engine, cid)
    lab = {c.config_id: c for g in labels.groups for c in g.configs}
    for conf in q.configs:
        theirs = lab[conf.config_id].metrics["total_ms"]
        assert conf.total_ms is not None and theirs is not None
        assert conf.total_ms.median == theirs.median
        assert conf.total_ms.cv == theirs.cv
        assert conf.n == lab[conf.config_id].repetitions
    retried = next(c for c in q.configs if c.config_id == first.config_id)
    assert retried.total_ms is not None and retried.total_ms.ci_high == 777.0
    assert retried.n == 3  # the retry replaced a repetition, did not add one
    assert q.summary.bench_role_records == {"filler": 9, "warmup": 9}


async def test_every_record_is_accounted_for_in_the_summary(engine: Any) -> None:  # noqa: F811
    store = BenchStore(engine)
    cid, runs = await make_campaign(store)
    await finish(engine, runs, WINNER)
    extra: list[dict[str, Any]] = []
    for run in runs:
        extra += noise(run)
    await insert_records(engine, extra)
    q = await campaign_quality(engine, cid, campaign_state="done")
    # accounting: each non-role record is an ok measurement (label row) or is
    # counted as a failure of some kind - none may vanish
    s = q.summary
    ok_rows = sum(c.records_checked for c in q.configs)
    accounted = (
        ok_rows
        + s.infra_failures
        + s.owner_preempted
        + sum(s.condition_failures.values())
        + sum(s.workload_failures.values())
        # G5c fix r1 (T-3): the buckets the fix added for the records that
        # used to vanish (class ok but refused by the label filter; any other
        # class)
        + sum(s.rejected_ok.values())
        + sum(s.other_failures.values())
    )
    assert s.measured_records == ok_rows
    assert dict(s.rejected_ok) == {
        "check_failed": len(runs),
        "condition_unverified": len(runs),
    }
    assert accounted == s.accounted
    assert accounted == s.records, (
        "records the label filter rejects with failure_class 'ok' "
        "(check_failed / unverified condition) are in no bucket",
        accounted,
        s.records,
        dict(s.condition_failures),
        dict(s.workload_failures),
    )


async def test_ten_thousand_records_are_streamed_in_bounded_time_and_memory(
    engine: Any,  # noqa: F811
) -> None:
    store = BenchStore(engine)
    cid, runs = await make_campaign(store)
    await finish(engine, runs, WINNER)
    base = runs[0]
    rows = [
        record(
            base,
            100 + (i % 9),
            job_id=f"job_big{i:08d}",
            at_s=10 + i,
            attempt_no=i + 2,
            repetition=1 + i % 3,
        )
        for i in range(10_000)
    ]
    for lo in range(0, len(rows), 2000):
        await insert_records(engine, rows[lo : lo + 2000])
    tracemalloc.start()
    t = time.monotonic()
    q = await campaign_quality(engine, cid, campaign_state="done")
    elapsed = time.monotonic() - t
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 20, elapsed
    assert peak < 200 * 1024 * 1024, peak
    assert q.summary.records >= 10_000
    big = next(c for c in q.configs if c.config_id == base.config_id)
    assert big.n == 3 and big.records_checked >= 10_000


async def test_fillers_only_campaign(engine: Any) -> None:  # noqa: F811
    store = BenchStore(engine)
    cid, runs = await make_campaign(store)
    fillers = [
        record(
            r,
            10**9,
            job_id=f"job_fill{i:08d}",
            at_s=5 + i,
            condition_measured={"bench_role": "filler"},
        )
        for i, r in enumerate(runs)
    ]
    await insert_records(engine, fillers)
    q = await campaign_quality(engine, cid, campaign_state="active")
    assert q.summary.records == 0 and q.summary.bench_role_records == {"filler": 9}
    assert all(c.n == 0 and c.total_ms is None for c in q.configs)
    assert q.summary.infra_failure_rate is None and q.summary.pending
