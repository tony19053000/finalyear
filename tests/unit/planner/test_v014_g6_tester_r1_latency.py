"""V0.14 G6 tester round 1 - criterion 3 and the latency block, from the brief.

Derived from D324 / roadmap 0.14.8 criterion 3 ("decision latency < 100 ms for
4 nodes"), not from the implementation: nearest-rank percentiles are compared
with an independent exact (``Fraction``) reference on random inputs; the
boundaries n = 1, 2, 100, 101; budget exactly at the threshold; the
``too_few_decisions`` minimum at 29 / 30; NULL / NaN / inf / negative values
never counted as zero; old (pre-G6) reports through the API model, Markdown
and CLI; CLI lines == the API block.
"""

from __future__ import annotations

import json
import math
import random
from fractions import Fraction
from typing import Any

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
from meshforge.controller.planner_eval.model import EvalConfig, default_policies
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

CFG = LatencyConfig(budget_ms=100.0, node_count=4, min_decisions=30)


_ALL: Any = object()


def _s(
    values: list[float | None], nodes: int | None = 4, live: int | None = _ALL
) -> list[LatencySample]:
    """Samples of ``nodes`` node rows, ``live`` of them live (default: all,
    G6F D332)."""
    count = nodes if live is _ALL else live
    return [LatencySample(v, nodes, count) for v in values]


def _verdict(samples: list[LatencySample], cfg: LatencyConfig = CFG) -> dict[str, Any]:
    return latency_verdict(latency_block(samples, cfg), leaked=False)


# ------------------------------------------------------------ percentile edges


@pytest.mark.parametrize("pct", [1, 50, 99, 100])
def test_one_value_is_every_percentile(pct: int) -> None:
    assert nearest_rank([7.5], pct) == 7.5


def test_two_values_p50_is_the_lower_p99_the_upper() -> None:
    assert nearest_rank([1.0, 9.0], 50) == 1.0
    assert nearest_rank([1.0, 9.0], 51) == 9.0
    assert nearest_rank([1.0, 9.0], 99) == 9.0
    assert nearest_rank([1.0, 9.0], 100) == 9.0


def test_p99_of_100_values_is_the_99th_not_the_max() -> None:
    values = [float(i) for i in range(1, 101)]
    assert nearest_rank(values, 99) == 99.0
    assert nearest_rank(values, 100) == 100.0
    assert nearest_rank(values, 50) == 50.0
    assert nearest_rank(values, 1) == 1.0


def test_p99_of_101_values_is_the_100th() -> None:
    values = [float(i) for i in range(1, 102)]
    assert nearest_rank(values, 99) == 100.0  # ceil(99 * 101 / 100) = 100


def test_one_outlier_among_100_does_not_decide_p99_but_among_99_it_does() -> None:
    hundred = [1.0] * 99 + [500.0]
    assert latency_block(_s(hundred), CFG)["p99_ms"] == 1.0
    assert latency_block(_s(hundred), CFG)["max_ms"] == 500.0
    ninety_nine = [1.0] * 98 + [500.0]
    assert latency_block(_s(ninety_nine), CFG)["p99_ms"] == 500.0


def test_matches_an_exact_fraction_reference_on_random_inputs() -> None:
    rng = random.Random(14)  # noqa: S311
    for _ in range(300):
        n = rng.randint(1, 250)
        values = sorted(round(rng.uniform(0, 300), 3) for _ in range(n))
        for pct in (1, 25, 50, 75, 90, 95, 99, 100):
            rank = math.ceil(Fraction(pct * n, 100))
            assert nearest_rank(values, pct) == values[rank - 1], (n, pct)


@pytest.mark.parametrize("pct", [0, -1, 101, 1000])
def test_out_of_range_percent_is_refused(pct: int) -> None:
    with pytest.raises(ValueError):
        nearest_rank([1.0], pct)


def test_no_values_has_no_percentile() -> None:
    assert nearest_rank([], 99) is None


# -------------------------------------------------- unusable values, never zero


@pytest.mark.parametrize("bad", [None, math.nan, math.inf, -math.inf, -0.001, -1e9])
def test_an_unusable_value_is_counted_not_timed_and_never_zero(
    bad: float | None,
) -> None:
    block = latency_block(_s([bad, 10.0, 20.0]), CFG)
    assert block["decisions"] == 2 and block["without_latency"] == 1
    assert block["p50_ms"] == 10.0 and block["max_ms"] == 20.0
    assert block["judged"]["decisions"] == 2
    assert block["by_node_count"]["4"]["decisions"] == 2


def test_all_unusable_gives_an_empty_block_and_too_few() -> None:
    samples = _s([None, math.nan, math.inf, -1.0] * 20)
    block = latency_block(samples, CFG)
    assert block["decisions"] == 0 and block["without_latency"] == 80
    assert block["p50_ms"] is None and block["p99_ms"] is None
    assert block["max_ms"] is None and block["over_budget"] == 0
    assert block["by_node_count"] == {}
    assert block["judged"] == {"decisions": 0, "p99_ms": None}
    verdict = latency_verdict(block, leaked=False)
    assert verdict["status"] == "too_few_decisions"
    assert verdict["value"] is None and verdict["counted_groups"] == 0


def test_no_samples_at_all() -> None:
    block = latency_block([], CFG)
    assert block["decisions"] == 0 and block["without_latency"] == 0
    assert latency_verdict(block, leaked=False)["status"] == "too_few_decisions"


def test_zero_is_a_real_time() -> None:
    block = latency_block(_s([0.0, 0.0, 4.0]), CFG)
    assert block["decisions"] == 3 and block["without_latency"] == 0
    assert block["p50_ms"] == 0.0


def test_nan_cannot_poison_the_json() -> None:
    samples = _s([math.nan, math.inf, 5.0] * 40)
    block = latency_block(samples, CFG)
    text = json.dumps(block, allow_nan=False)  # raises on NaN / Infinity
    assert "NaN" not in text and "Infinity" not in text


def test_huge_but_finite_values_survive() -> None:
    block = latency_block(_s([1e300, 1.0]), CFG)
    assert block["max_ms"] == 1e300 and block["over_budget"] == 1
    json.dumps(block, allow_nan=False)


def test_the_block_is_independent_of_input_order() -> None:
    rng = random.Random(3)  # noqa: S311
    base = [
        LatencySample(rng.uniform(0, 150), rng.choice([2, 3, 4, 5, None]))
        for _ in range(200)
    ]
    expected = latency_block(base, CFG)
    for seed in range(5):
        shuffled = list(base)
        random.Random(seed).shuffle(shuffled)  # noqa: S311
        assert latency_block(shuffled, CFG) == expected


# ------------------------------------------------------------------ the verdict


def test_exactly_29_judged_is_too_few_exactly_30_is_judged() -> None:
    assert _verdict(_s([1.0] * 29))["status"] == "too_few_decisions"
    assert _verdict(_s([1.0] * 30))["status"] == "PASS"
    assert _verdict(_s([1.0] * 30))["counted_groups"] == 30


def test_too_few_is_never_pass_even_when_every_decision_is_instant() -> None:
    assert _verdict(_s([0.0] * 29))["status"] == "too_few_decisions"
    assert _verdict(_s([0.0] * 29))["status"] != "PASS"


def test_too_few_even_when_every_decision_is_hopelessly_slow() -> None:
    assert _verdict(_s([5000.0] * 29))["status"] == "too_few_decisions"


def test_other_snapshot_sizes_do_not_fill_the_minimum() -> None:
    mixed = _s([1.0] * 100, nodes=3) + _s([1.0] * 29, nodes=4)
    assert _verdict(mixed)["status"] == "too_few_decisions"
    assert _verdict(mixed)["counted_groups"] == 29
    unknown = _s([1.0] * 100, nodes=None) + _s([1.0] * 10, nodes=4)
    assert _verdict(unknown)["status"] == "too_few_decisions"


def test_a_negative_node_count_is_unknown_and_never_judged() -> None:
    block = latency_block(_s([1.0] * 40, nodes=-1), CFG)
    assert list(block["by_node_count"]) == ["unknown"]
    assert block["judged"]["decisions"] == 0


@pytest.mark.parametrize(
    ("p99", "status"),
    [
        (99.0, "PASS"),
        (99.999, "PASS"),
        (100.0, "FAIL"),  # exactly the threshold: strictly below is required
        (100.001, "FAIL"),
    ],
)
def test_budget_at_the_threshold(p99: float, status: str) -> None:
    # 100 values: the 99th is the p99 -> 98 fast + p99 + one huge (not the p99)
    samples = _s([1.0] * 98 + [p99, 10_000.0])
    assert _verdict(samples)["status"] == status
    assert _verdict(samples)["value"] == p99


def test_max_exactly_at_budget_with_a_fast_p99_passes_and_is_not_over_budget() -> None:
    samples = _s([2.0] * 99 + [100.0])
    block = latency_block(samples, CFG)
    assert block["over_budget"] == 0  # > budget, 100.0 is not over
    assert latency_verdict(block, leaked=False)["status"] == "PASS"


def test_a_custom_budget_moves_the_boundary() -> None:
    cfg = LatencyConfig(budget_ms=5.0, node_count=4, min_decisions=30)
    samples = _s([5.0] * 98 + [4.999, 6.0])
    assert _verdict(_s([4.0] * 40), cfg)["status"] == "PASS"
    assert _verdict(_s([5.0] * 40), cfg)["status"] == "FAIL"
    assert _verdict(samples, cfg)["status"] == "FAIL"
    assert _verdict(_s([4.0] * 40), cfg)["threshold"] == 5.0


def test_over_budget_counts_every_timed_decision_not_only_the_judged() -> None:
    samples = _s([200.0] * 3, nodes=2) + _s([1.0] * 30, nodes=4)
    block = latency_block(samples, CFG)
    assert block["over_budget"] == 3
    assert latency_verdict(block, leaked=False)["status"] == "PASS"


def test_slow_small_snapshots_do_not_fail_the_four_node_criterion() -> None:
    samples = _s([900.0] * 100, nodes=2) + _s([3.0] * 40, nodes=4)
    assert _verdict(samples)["status"] == "PASS"


def test_a_slow_bigger_snapshot_fails_it() -> None:
    samples = _s([3.0] * 40, nodes=4) + _s([300.0] * 40, nodes=6)
    # the documented rule: "at least 4" -> 80 judged, p99 300 -> FAIL
    assert _verdict(samples)["status"] == "FAIL"


def test_three_live_plus_two_stale_rows_is_a_five_node_snapshot_and_not_judged() -> (
    None
):
    # reviewer note; G6 judged the snapshot's size (PASS here). D332 (G6F)
    # judges live nodes: 3 laptops + 2 stale rows are not 4 laptops
    assert _verdict(_s([1.0] * 30, nodes=5, live=3))["status"] == "too_few_decisions"
    assert _verdict(_s([1.0] * 30, nodes=5, live=3))["counted_groups"] == 0
    assert _verdict(_s([1.0] * 30, nodes=5, live=4))["status"] == "PASS"


def test_a_leaked_report_hides_the_verdict_but_keeps_what_it_would_be() -> None:
    block = latency_block(_s([1.0] * 30), CFG)
    leaked = latency_verdict(block, leaked=True)
    assert leaked["status"] == "leaked" and leaked["would_be"] == "PASS"
    assert latency_verdict(block, leaked=False)["would_be"] is None
    few = latency_verdict(latency_block(_s([1.0] * 3), CFG), leaked=True)
    assert few["status"] == "leaked" and few["would_be"] == "too_few_decisions"


def test_a_block_missing_its_judged_part_is_too_few_not_a_crash() -> None:
    block = dict(latency_block(_s([1.0] * 40), CFG))
    del block["judged"]
    assert latency_verdict(block, leaked=False)["status"] == "too_few_decisions"
    block["judged"] = {"decisions": 40, "p99_ms": None}
    assert latency_verdict(block, leaked=False)["status"] == "too_few_decisions"


def test_the_verdict_has_the_shape_of_the_other_verdicts() -> None:
    v = _verdict(_s([1.0] * 30))
    assert v["criterion"] == 3 and v["comparisons"] == []
    assert set(v) >= {"criterion", "status", "would_be", "value", "threshold", "detail"}
    assert "30" in v["detail"] and "< 100" in v["detail"]


# --------------------------------------------------- report, old reports, CLI == API


def _report(samples: list[LatencySample]) -> dict[str, Any]:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0})
    groups = [group("g00", {"node_a": 300, "node_b": 100})]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return evaluate(
        groups,
        recs,
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=100),
        campaign_ids=["camp_1"],
        latency=samples,
    )


def _old(report: dict[str, Any]) -> dict[str, Any]:
    """The report as G4/G5 stored it: no latency block, no criterion 3."""
    old = json.loads(report_text(report))
    del old["latency"]
    old["verdicts"] = [v for v in old["verdicts"] if v["criterion"] != 3]
    return old


def test_evaluate_without_latency_input_is_a_valid_too_few_report() -> None:
    request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0})
    groups = [group("g00", {"node_a": 300, "node_b": 100})]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=100),
        campaign_ids=["camp_1"],
    )
    assert report["latency"]["decisions"] == 0
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions"
    EvalReportOut.model_validate(report)


def test_an_old_report_without_a_latency_block_still_validates_and_renders() -> None:
    old = _old(_report(_s([1.0] * 40)))
    model = EvalReportOut.model_validate(old)
    assert model.latency is None
    assert [v.criterion for v in model.verdicts] == [1, 2]
    text = render_markdown(old)
    assert "Decision latency" not in text and "| 3 |" not in text
    # CLI: no latency lines, no crash; the reports table shows a blank cell
    assert latency_lines(old.get("latency")) == []
    lines = eval_lines({"id": 1, "report": old})
    assert not any(line.startswith("decision latency") for line in lines)
    table = reports_table(
        {"reports": [{"id": 1, "verdicts": old["verdicts"], "campaign_ids": []}]}
    )
    assert [c.header for c in table.columns][-1] == "CRITERION 3"
    assert len(list(table.columns[-1].cells)) == 1


@pytest.mark.parametrize("junk", [None, [], "x", 5, {}, {"decisions": 1}])
def test_the_cli_survives_a_malformed_latency_block(junk: Any) -> None:
    lines = latency_lines(junk)
    assert isinstance(lines, list)


def test_cli_numbers_equal_the_api_block() -> None:
    samples = _s([float(i) + 0.5 for i in range(50)]) + _s([8.25] * 7, nodes=3)
    report = _report(samples)
    block = EvalReportOut.model_validate(report).latency
    assert block is not None
    lines = latency_lines(report["latency"])
    head = lines[0]
    assert f"{block.decisions} dispatched decision(s) timed" in head
    assert f"({block.without_latency} without a time)" in head
    assert f"p50 {block.p50_ms:.2f} ms" in head
    assert f"p99 {block.p99_ms:.2f} ms" in head
    assert f"max {block.max_ms:.2f} ms" in head
    assert f"{block.over_budget} over the 100 ms budget" in head
    for nodes, bucket in block.by_node_count.items():
        assert (
            f"{nodes} nodes: {bucket.decisions} decision(s), p99 {bucket.p99_ms:.2f} ms"
            in lines[1]
        )
    assert block.by_live_node_count is not None
    for nodes, bucket in block.by_live_node_count.items():
        assert (
            f"{nodes} nodes: {bucket.decisions} decision(s), p99 {bucket.p99_ms:.2f} ms"
            in lines[2]
        )
    assert (
        f"{block.judged.decisions} decision(s), p99 {block.judged.p99_ms:.2f} ms"
        in lines[3]
    )
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert f"criterion 3: {c3['status']}" in "\n".join(
        eval_lines({"id": 1, "report": report})
    )


def test_markdown_numbers_equal_the_block_and_empty_buckets_do_not_crash() -> None:
    empty = _report(_s([None, math.nan]))
    text = render_markdown(empty)
    assert "dispatched decisions timed: 0 (without a time: 2)" in text
    full = _report(_s([1.0] * 31))
    assert "| 4 | 31 |" in render_markdown(full)


def test_report_text_is_deterministic_and_strict_json_with_hostile_samples() -> None:
    hostile = _s([math.nan, math.inf, -1.0, None, 1e300, 0.0, 2.5] * 6)
    a, b = report_text(_report(hostile)), report_text(_report(list(reversed(hostile))))
    assert a == b
    json.loads(a, parse_constant=lambda c: pytest.fail(f"non-strict JSON constant {c}"))
