"""V0.14 G6 (D324): the evaluator's decision-latency block and criterion 3.

* nearest-rank percentiles are exact (integer rank, no interpolation);
* rows without a time (NULL before migration 0033, non-finite, negative) are
  excluded and counted - never a 0 that would pull the percentiles down;
* ``by_node_count`` per snapshot size, ``unknown`` last;
* criterion 3 judges only the 4-node decisions: ``too_few_decisions`` below
  30, ``PASS`` strictly below the budget (p99 == budget is ``FAIL``), stamped
  ``leaked`` like every verdict;
* the report carries the block, validates against the API model, stays
  byte-identical for the same inputs, and ``holdout`` does not filter it;
* the Markdown section and the CLI lines show it; the loader's pure row
  mapper reads the replay's node count, else the candidates.
"""

from __future__ import annotations

import math

import pytest
from mf_eval_world import fabric, group, records

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.cli.planner_commands import eval_lines, latency_lines, reports_table
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.latency import (
    LatencyConfig,
    LatencySample,
    latency_block,
    latency_verdict,
    nearest_rank,
)
from meshforge.controller.planner_eval.loader import latency_samples
from meshforge.controller.planner_eval.model import EvalConfig, default_policies
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

CFG = LatencyConfig(budget_ms=100.0, node_count=4, min_decisions=30)


def _four(values: list[float]) -> list[LatencySample]:
    """Decisions on 4 live laptops (4 node rows, all live)."""
    return [LatencySample(v, 4, 4) for v in values]


# ---------------------------------------------------------------- percentiles


def test_nearest_rank_is_exact() -> None:
    hundred = [float(i) for i in range(1, 101)]
    assert nearest_rank(hundred, 99) == 99.0
    assert nearest_rank(hundred, 50) == 50.0
    assert nearest_rank(hundred, 100) == 100.0
    assert nearest_rank(hundred, 1) == 1.0
    two_hundred = [float(i) for i in range(1, 201)]
    assert nearest_rank(two_hundred, 99) == 198.0  # rank ceil(198.0) = 198
    assert nearest_rank([7.0], 99) == 7.0
    assert nearest_rank([1.0, 2.0, 3.0], 99) == 3.0  # rank ceil(2.97) = 3
    assert nearest_rank([1.0, 2.0, 3.0], 50) == 2.0  # rank ceil(1.5) = 2
    assert nearest_rank([1.0, 2.0], 50) == 1.0  # rank ceil(1.0) = 1
    assert nearest_rank([], 99) is None


@pytest.mark.parametrize("pct", [0, 101, -5])
def test_nearest_rank_refuses_a_percent_out_of_range(pct: int) -> None:
    with pytest.raises(ValueError, match="pct"):
        nearest_rank([1.0], pct)


# ---------------------------------------------------------------- block


def test_an_empty_block() -> None:
    block = latency_block([], CFG)
    assert block == {
        "decisions": 0,
        "without_latency": 0,
        "p50_ms": None,
        "p99_ms": None,
        "max_ms": None,
        "over_budget": 0,
        "budget_ms": 100.0,
        "node_count": 4,
        "min_decisions": 30,
        "judged_on": "live_nodes",
        "by_node_count": {},
        "by_live_node_count": {},
        "judged": {"decisions": 0, "p99_ms": None},
    }


def test_the_block_reads_sorted_values_whatever_their_order() -> None:
    values = [float(v) for v in range(100, 0, -1)]  # 100 .. 1, reversed
    block = latency_block(_four(values), CFG)
    assert block["decisions"] == 100
    assert (block["p50_ms"], block["p99_ms"], block["max_ms"]) == (50.0, 99.0, 100.0)
    assert block["by_node_count"] == {"4": {"decisions": 100, "p99_ms": 99.0}}


def test_rows_without_a_time_are_excluded_and_counted_never_zero() -> None:
    timed = [10.0, 20.0, 30.0, 40.0]
    odd = [None, math.nan, math.inf, -1.0]
    block = latency_block(_four(timed) + [LatencySample(v, 4) for v in odd], CFG)
    assert block["decisions"] == 4 and block["without_latency"] == 4
    # counted as 0 they would make p50 = 0 and the bucket 8 decisions
    assert block["p50_ms"] == 20.0 and block["p99_ms"] == 40.0
    assert block["by_node_count"]["4"]["decisions"] == 4


def test_over_budget_is_strictly_slower_than_the_budget() -> None:
    block = latency_block(_four([99.999, 100.0, 100.001, 250.0]), CFG)
    assert block["over_budget"] == 2


def test_by_node_count_groups_by_snapshot_size_unknown_last() -> None:
    samples = [
        LatencySample(5.0, 16),
        LatencySample(1.0, 3),
        LatencySample(2.0, 3),
        LatencySample(9.0, None),
        LatencySample(4.0, 4),
        LatencySample(None, 3),
    ]
    block = latency_block(samples, CFG)
    assert list(block["by_node_count"]) == ["3", "4", "16", "unknown"]
    assert block["by_node_count"]["3"] == {"decisions": 2, "p99_ms": 2.0}
    assert block["by_node_count"]["unknown"] == {"decisions": 1, "p99_ms": 9.0}
    assert block["decisions"] == 5 and block["without_latency"] == 1


# ---------------------------------------------------------------- verdict


def _verdict(values: list[float], *, leaked: bool = False, **cfg: float) -> dict:  # type: ignore[type-arg]
    config = LatencyConfig(**{"budget_ms": 100.0, **cfg})  # type: ignore[arg-type]
    return latency_verdict(latency_block(_four(values), config), leaked=leaked)


def test_fewer_than_thirty_decisions_is_too_few_never_pass() -> None:
    v = _verdict([1.0] * 29)
    assert v["criterion"] == 3 and v["status"] == "too_few_decisions"
    assert v["counted_groups"] == 29 and v["value"] == 1.0
    assert _verdict([1.0] * 30)["status"] == "PASS"


def test_pass_is_strictly_below_the_budget() -> None:
    assert _verdict([1.0] * 29 + [99.999])["status"] == "PASS"
    at = _verdict([1.0] * 29 + [100.0])
    assert at["status"] == "FAIL" and at["value"] == 100.0 and at["threshold"] == 100.0
    assert _verdict([1.0] * 29 + [100.001])["status"] == "FAIL"


def test_p99_not_max_decides() -> None:
    # 200 decisions: p99 is the 198th value; two slow outliers above it
    values = [10.0] * 198 + [5_000.0, 6_000.0]
    v = _verdict(values)
    assert v["status"] == "PASS" and v["value"] == 10.0
    # a third outlier reaches rank 198
    assert _verdict([10.0] * 197 + [5_000.0] * 3)["status"] == "FAIL"


def test_only_snapshots_of_at_least_the_criterion_size_are_judged() -> None:
    fast_three = [LatencySample(1.0, 3, 3) for _ in range(100)]
    slow_four = [LatencySample(500.0, 4, 4) for _ in range(30)]
    block = latency_block(fast_three + slow_four, CFG)
    assert block["p99_ms"] == 500.0
    assert block["judged"] == {"decisions": 30, "p99_ms": 500.0}
    assert latency_verdict(block, leaked=False)["status"] == "FAIL"
    only_three = latency_block(fast_three, CFG)
    v = latency_verdict(only_three, leaked=False)
    assert v["status"] == "too_few_decisions" and v["value"] is None
    assert v["counted_groups"] == 0


def test_a_larger_snapshot_counts_toward_the_criterion() -> None:
    # 4 laptops + an old revoked row: every snapshot holds 5 rows, 4 live
    five = [LatencySample(float(i), 5, 4) for i in range(1, 41)]
    unknown = [LatencySample(999.0, None) for _ in range(5)]
    block = latency_block(five + unknown, CFG)
    assert block["judged"] == {"decisions": 40, "p99_ms": 40.0}
    v = latency_verdict(block, leaked=False)
    assert (v["status"], v["value"], v["counted_groups"]) == ("PASS", 40.0, 40)
    assert ">= 4 live nodes" in v["detail"]
    assert list(block["by_live_node_count"]) == ["4", "unknown"]


def test_stale_rows_do_not_make_three_laptops_four_d332() -> None:
    # 3 live laptops + 2 stale/revoked rows: 5 node rows, 3 live -> not judged
    stale = [LatencySample(1.0, 5, 3) for _ in range(40)]
    block = latency_block(stale, CFG)
    assert block["by_node_count"] == {"5": {"decisions": 40, "p99_ms": 1.0}}
    assert block["by_live_node_count"] == {"3": {"decisions": 40, "p99_ms": 1.0}}
    assert block["judged"] == {"decisions": 0, "p99_ms": None}
    v = latency_verdict(block, leaked=False)
    assert v["status"] == "too_few_decisions" and v["counted_groups"] == 0
    assert "0 dispatched decision(s) with >= 4 live nodes" in v["detail"]


def test_a_decision_without_a_live_count_is_never_judged() -> None:
    # 4 node rows read from the candidates (no replay): live unknown
    block = latency_block([LatencySample(1.0, 4, None) for _ in range(40)], CFG)
    assert block["judged"]["decisions"] == 0
    assert block["by_live_node_count"] == {"unknown": {"decisions": 40, "p99_ms": 1.0}}


def test_a_leaked_report_stamps_criterion_3_too() -> None:
    v = _verdict([1.0] * 40, leaked=True)
    assert v["status"] == "leaked" and v["would_be"] == "PASS"
    assert _verdict([1.0] * 40)["would_be"] is None


def test_the_budget_comes_from_the_config() -> None:
    assert _verdict([60.0] * 40, budget_ms=50.0)["status"] == "FAIL"
    assert _verdict([60.0] * 40, budget_ms=60.5)["status"] == "PASS"


# ---------------------------------------------------------------- report


def _report(samples: list[LatencySample], **kw: object) -> dict:  # type: ignore[type-arg]
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0})
    groups = [group("g00", {"node_a": 300, "node_b": 100})]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return evaluate(
        groups,
        recs,
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=200),
        campaign_ids=["camp_1"],
        latency=samples,
        **kw,  # type: ignore[arg-type]
    )


SAMPLES = [LatencySample(float(i % 17) + 0.25, 4, 4) for i in range(40)] + [
    LatencySample(None, 4, 4),
    LatencySample(3.5, 3, 3),
]


def test_the_report_carries_the_block_and_criterion_3() -> None:
    report = _report(SAMPLES)
    assert report["latency"] == latency_block(SAMPLES, LatencyConfig())
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert c3 == latency_verdict(report["latency"], leaked=False)
    assert [v["criterion"] for v in report["verdicts"]] == [1, 2, 3]
    model = EvalReportOut.model_validate(report)
    assert model.latency is not None and model.latency.decisions == 41


def test_same_samples_same_bytes_and_holdout_does_not_filter() -> None:
    assert report_text(_report(SAMPLES)) == report_text(_report(SAMPLES))
    held = _report(SAMPLES, holdout="cpu.other")
    assert held["latency"] == _report(SAMPLES)["latency"]


def test_markdown_shows_the_latency_section() -> None:
    text = render_markdown(_report(SAMPLES))
    assert "## Decision latency (criterion 3)" in text
    assert "dispatched decisions timed: 41 (without a time: 1)" in text
    assert "| 3 | 1 | 3.50 |" in text and "| 4 | 40 |" in text
    assert (
        "criterion 3 judges snapshots of >= 4 live nodes (online, connected, "
        "accepting jobs): 40 decisions" in text
    )
    assert "| live nodes | decisions | p99 ms |" in text
    older = _report(SAMPLES)
    del older["latency"]["judged_on"], older["latency"]["by_live_node_count"]
    older_text = render_markdown(older)  # a G6 report: judged every node row
    assert "criterion 3 judges snapshots of >= 4 nodes: 40 decisions" in older_text
    assert "| live nodes |" not in older_text
    assert "| 3 | PASS |" in text  # 40 four-node decisions, p99 16.25 ms
    report = _report(SAMPLES)
    del report["latency"]  # a report stored before G6
    assert "Decision latency" not in render_markdown(report)


def test_cli_lines_and_the_reports_table_show_it() -> None:
    report = _report(SAMPLES)
    lines = latency_lines(report["latency"])
    assert lines[0].startswith("decision latency: 41 dispatched decision(s) timed")
    assert "(1 without a time)" in lines[0] and "over the 100 ms budget" in lines[0]
    assert lines[1].startswith("  by snapshot size: ") and "4 nodes: 40 " in lines[1]
    assert lines[2].startswith("  by live nodes: ") and "4 nodes: 40 " in lines[2]
    assert lines[3].startswith("  criterion 3 judges snapshots of >= 4 live nodes: 40 ")
    assert latency_lines(None) == []
    record = {"id": 3, "report": report}
    assert any(line.startswith("criterion 3: PASS") for line in eval_lines(record))
    table = reports_table(
        {"reports": [{"id": 3, "verdicts": report["verdicts"], "campaign_ids": []}]}
    )
    headers = [c.header for c in table.columns]
    assert headers[-1] == "CRITERION 3"
    assert list(table.columns[-1].cells) == ["PASS"]


# ---------------------------------------------------------------- loader rows


def test_latency_samples_reads_the_replay_node_count_else_the_candidates() -> None:
    rows = [
        (12.5, 4, 3, None),
        (None, 4, 4, None),
        (7.0, None, None, [{"node_id": "a"}, {"node_id": "b", "node_set": ["b", "c"]}]),
        (True, 3, True, None),
        ("x", None, -1, None),
        (1.0, None, None, []),
    ]
    assert latency_samples(rows) == [
        LatencySample(12.5, 4, 3),
        LatencySample(None, 4, 4),
        LatencySample(7.0, 3, None),  # candidates give rows, never live nodes
        LatencySample(None, 3, None),
        LatencySample(None, None, None),
        LatencySample(1.0, None, None),
    ]
