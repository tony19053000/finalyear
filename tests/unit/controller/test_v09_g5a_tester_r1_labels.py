"""V0.9 G5 lane A tester r1: adversarial cases for labels, derived from the
brief (§5f Lane A items 2-5, D218/D222/D244/D246/D247), not from the code."""

from __future__ import annotations

import io
import itertools
import math
import random
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from test_v09_g5a_labels import CAMP, row, runs, series

from meshforge.bench import stats
from meshforge.controller.api.bench_label_models import labels_out
from meshforge.controller.telemetry import labels as lb
from meshforge.controller.telemetry import labels_export as lx
from meshforge.controller.telemetry.labels import build_labels

pytestmark = pytest.mark.unit


def one(plan: Any, rows: Any) -> lb.GroupLabel:
    out = build_labels(CAMP, plan, rows)
    assert len(out.groups) == 1
    return out.groups[0]


# --- best vs tie at the boundary (D218) ---------------------------------------


def test_equal_bounds_is_a_tie_not_best_with_larger_n() -> None:
    # n = 8 -> rank 2 interval. a = [x_2, x_7]; b chosen so b.low == a.high
    a = [100, 101, 102, 103, 104, 105, 106, 107]
    ci_a = stats.median_ci(a)
    b_low = int(ci_a.high)
    b = [
        b_low,
        b_low + 1,
        b_low + 2,
        b_low + 3,
        b_low + 4,
        b_low + 5,
        b_low + 6,
        b_low + 7,
    ]
    # make b's second-smallest equal b_low as well so its CI low == b_low
    b[1] = b_low
    assert stats.median_ci(b).low == ci_a.high
    g = one(runs("a", 8) + runs("b", 8), series("a", a) + series("b", b))
    assert g.label == "tie"
    assert set(g.tied_config_ids) == {"a", "b"}


def test_one_unit_gap_is_best() -> None:
    a = [100, 100, 100]
    b = [101, 101, 101]
    g = one(runs("a") + runs("b"), series("a", a) + series("b", b))
    assert g.label == "best" and g.best_config_id == "a"
    assert g.margin_pct == pytest.approx(1.0)


def test_zero_median_best_has_no_division_error() -> None:
    g = one(runs("a") + runs("b"), series("a", [0, 0, 0]) + series("b", [5, 6, 7]))
    assert g.label == "best" and g.margin_pct is None


def test_all_equal_everywhere_is_a_tie_ordered_by_config_id() -> None:
    plan = runs("b") + runs("a") + runs("c")
    g = one(plan, series("a", [9] * 3) + series("b", [9] * 3) + series("c", [9] * 3))
    assert g.label == "tie" and g.tied_config_ids == ("a", "b", "c")
    assert g.best_config_id is None and g.planner_agrees is None


def test_a_noisy_outlier_widens_the_ci_and_blocks_best() -> None:
    # medians differ clearly (100 vs 120) but a's spread reaches 500
    g = one(
        runs("a") + runs("b"),
        series("a", [100, 100, 500]) + series("b", [120, 121, 122]),
    )
    assert g.label == "tie"


# --- pending vs insufficient (D218 item 4) -------------------------------------


@pytest.mark.parametrize("state", ["planned", "submitted"])
def test_pending_even_when_every_config_has_three_good_records(state: str) -> None:
    plan = runs("a") + runs("b") + runs("c", reps=3)
    from dataclasses import replace

    plan[-1] = replace(plan[-1], state=state, recorded=False)
    rows = (
        series("a", [1, 2, 3]) + series("b", [10, 11, 12]) + series("c", [20, 21, 22])
    )
    assert one(plan, rows).label == "pending"


def test_failed_run_with_record_and_config_short_is_insufficient_not_pending() -> None:
    from dataclasses import replace

    plan = runs("a") + runs("b")
    plan[-1] = replace(plan[-1], state="failed", recorded=True)
    g = one(plan, series("a", [1, 2, 3]) + series("b", [10, 11]))
    assert g.label == "insufficient"


def test_failed_run_whose_record_is_not_written_yet_is_pending() -> None:
    from dataclasses import replace

    plan = runs("a") + runs("b")
    plan[-1] = replace(plan[-1], state="failed", recorded=False)
    g = one(plan, series("a", [1, 2, 3]) + series("b", [10, 11]))
    assert g.label == "pending"


@pytest.mark.parametrize(
    "detail", [None, "", "cancelled", "node gone", "Unsupported: x", "unsupported:x"]
)
def test_skip_that_is_not_exactly_unsupported_prefix_is_insufficient(
    detail: str | None,
) -> None:
    plan = runs("a") + runs("b", state="skipped", recorded=False, detail=detail)
    g = one(plan, series("a", [1, 2, 3]))
    assert g.label == "insufficient"
    assert next(c for c in g.configs if c.config_id == "b").state == "measured"


def test_partly_unsupported_config_is_not_excluded() -> None:
    from dataclasses import replace

    b = runs("b")
    b[0] = replace(b[0], state="skipped", recorded=False, detail="unsupported: x")
    g = one(runs("a") + b, series("a", [1, 2, 3]) + series("b", [10, 11]))
    assert g.label == "insufficient"


def test_all_unsupported_but_one_measured_is_best_without_margin() -> None:
    plan = runs("a") + runs(
        "b", state="skipped", recorded=False, detail="unsupported: no tc"
    )
    g = one(plan, series("a", [4, 5, 6]))
    assert g.label == "best" and g.margin_pct is None and g.best_config_id == "a"
    assert [c.state for c in g.configs] == ["measured", "unsupported"]


def test_done_run_with_no_ok_record_is_pending() -> None:
    plan = runs("a", recorded=False)
    assert one(plan, []).label == "pending"


def test_group_with_no_runs_and_no_rows_is_absent() -> None:
    assert build_labels(CAMP, [], []).groups == ()


def test_orphan_rows_without_planned_runs_do_not_crash() -> None:
    g = one([], series("a", [1, 2, 3]) + series("b", [50, 51, 52]))
    assert g.label == "best"


# --- repetition / retry handling (D234) ---------------------------------------


def test_repetitions_are_distinct_values_not_row_counts() -> None:
    rows = [
        row("a", 1, 10, at_s=1),
        row("a", 1, 11, at_s=2),
        row("a", 1, 12, at_s=3),
        row("a", 2, 13),
    ]
    g = one(runs("a", 2), rows)
    c = g.configs[0]
    assert c.repetitions == 2
    assert g.label == "insufficient"  # three rows but only two repetitions


def test_latest_wins_on_total_ties_deterministically_in_every_order() -> None:
    base = [
        row("a", 1, 100, at_s=5, attempt_no=1),
        row("a", 1, 200, at_s=5, attempt_no=2),
        row(
            "a", 1, 300, at_s=5, attempt_no=2
        ),  # same time, same no: attempt id decides
    ]
    want = max(
        base, key=lambda r: (r["attempt_created_at"], r["attempt_no"], r["attempt_id"])
    )
    for perm in itertools.permutations(base):
        (kept,) = lb.latest_per_repetition(list(perm))
        assert kept["attempt_id"] == want["attempt_id"]


def test_older_attempt_with_higher_number_loses_to_newer_creation() -> None:
    old = row("a", 1, 1, at_s=1, attempt_no=9)
    new = row("a", 1, 2, at_s=2, attempt_no=1)
    (kept,) = lb.latest_per_repetition([old, new])
    assert kept["total_ms"] == 2


def test_rows_without_config_or_repetition_are_dropped_not_crashed() -> None:
    bad = [
        row("a", 1, 5),
        {**row("a", 2, 6), "repetition": None},
        {**row("a", 3, 7), "config_id": None},
    ]
    assert [r["total_ms"] for r in lb.latest_per_repetition(bad)] == [5]


# --- metrics ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ns", "iters", "want"),
    [
        (1000, 4, 250.0),
        (0, 4, 0.0),
        (1000, 0, None),
        (1000, -1, None),
        (None, 4, None),
        (1000, None, None),
        (True, 4, None),
        (1000, 4.0, None),
    ],
)
def test_compute_per_iter_edges(ns: Any, iters: Any, want: float | None) -> None:
    assert (
        lb.metric_value(
            {"compute_ns": ns, "compute_iterations": iters}, "compute_per_iter_ns"
        )
        == want
    )


@pytest.mark.parametrize(
    ("i", "o", "want"), [(5, 7, 12), (0, 0, 0), (None, 7, None), (5, None, None)]
)
def test_bytes_edges(i: Any, o: Any, want: int | None) -> None:
    assert lb.metric_value({"bytes_in": i, "bytes_out": o}, "bytes") == want


def test_metrics_summarise_only_non_null_values_and_all_null_is_none() -> None:
    rows = [
        row(
            "a",
            r,
            10 + r,
            compute_ns=None,
            compute_iterations=None,
            bytes_in=r,
            bytes_out=None,
        )
        for r in (1, 2, 3)
    ]
    g = one(runs("a"), rows)
    c = g.configs[0]
    assert c.metrics["compute_per_iter_ns"] is None and c.metrics["bytes"] is None
    assert c.metrics["total_ms"] is not None and c.metrics["total_ms"].n == 3


def test_partial_nulls_reduce_n_for_that_metric_only() -> None:
    rows = [
        row("a", 1, 10, compute_ns=100, compute_iterations=1),
        row("a", 2, 11, compute_ns=None, compute_iterations=1),
        row("a", 3, 12, compute_ns=300, compute_iterations=1),
    ]
    c = one(runs("a"), rows).configs[0]
    assert c.metrics["compute_per_iter_ns"].n == 2  # type: ignore[union-attr]
    assert c.metrics["total_ms"].n == 3  # type: ignore[union-attr]


def test_null_total_makes_config_insufficient() -> None:
    rows = [
        *series("a", [1, 2, 3]),
        row("b", 1, None),
        row("b", 2, None),
        row("b", 3, None),
    ]
    assert one(runs("a") + runs("b"), rows).label == "insufficient"


def test_huge_values_do_not_break_labels() -> None:
    big = 9_000_000_000_000_000_000
    g = one(
        runs("a") + runs("b"), series("a", [big, big, big]) + series("b", [1, 2, 3])
    )
    assert g.label == "best" and g.best_config_id == "b"


# --- determinism / parity (D222) ----------------------------------------------


def _campaign(n_groups: int) -> tuple[list[Any], list[Any]]:
    rng = random.Random(7)  # noqa: S311
    plan, rows = [], []
    for g in range(n_groups):
        gid = f"grp{g:04d}"
        for cfg in ("a", "b", "c"):
            plan += runs(cfg, group=gid)
            base = rng.choice([100, 100, 300])
            rows += series(
                cfg, [base + rng.randint(0, 40) for _ in range(3)], group=gid
            )
    return plan, rows


def _bytes(plan: list[Any], rows: list[Any]) -> bytes:
    return lx.labels_parquet_bytes(build_labels(CAMP, plan, rows))


def test_insertion_order_never_changes_the_bytes() -> None:
    plan, rows = _campaign(12)
    want = _bytes(plan, rows)
    rng = random.Random(1)  # noqa: S311
    for _ in range(4):
        rng.shuffle(plan)
        rng.shuffle(rows)
        assert _bytes(plan, rows) == want


@pytest.mark.parametrize("group_size", [1, 2, 5, 7, 36, 1000])
def test_row_group_boundaries_do_not_change_the_data_and_are_stable(
    monkeypatch: pytest.MonkeyPatch, group_size: int
) -> None:
    plan, rows = _campaign(12)  # 36 table rows
    reference = pq.read_table(io.BytesIO(_bytes(plan, rows))).to_pylist()
    monkeypatch.setattr(lx, "ROW_GROUP_SIZE", group_size)
    first, second = _bytes(plan, rows), _bytes(plan, list(reversed(rows)))
    assert first == second
    file = pq.ParquetFile(io.BytesIO(first))
    assert file.metadata.num_rows == 36
    assert file.metadata.num_row_groups == math.ceil(36 / group_size)
    assert pq.read_table(io.BytesIO(first)).to_pylist() == reference


def test_empty_campaign_is_a_valid_deterministic_file() -> None:
    a, b = _bytes([], []), _bytes([], [])
    assert a == b
    table = pq.read_table(io.BytesIO(a))
    assert table.num_rows == 0 and table.column_names == list(lx.LABEL_COLUMN_NAMES)


def test_json_and_parquet_carry_the_same_numbers() -> None:
    plan, rows = _campaign(6)
    labels = build_labels(CAMP, plan, rows)
    doc = labels_out(labels, campaign_state="done").model_dump(mode="json")
    table = pq.read_table(io.BytesIO(lx.labels_parquet_bytes(labels))).to_pylist()
    flat = {
        (g["group_id"], c["config_id"]): (g, c)
        for g in doc["groups"]
        for c in g["configs"]
    }
    assert len(flat) == len(table)
    for r in table:
        g, c = flat[(r["group_id"], r["config_id"])]
        assert r["label"] == g["label"]
        assert r["best_config_id"] == g["best_config_id"]
        assert r["is_best"] == (c["config_id"] == g["best_config_id"])
        assert r["in_tie"] == (c["config_id"] in g["tied_config_ids"])
        assert r["margin_pct"] == g["margin_pct"]
        assert r["repetitions"] == c["repetitions"]
        for metric in ("total_ms", "compute_per_iter_ns", "bytes"):
            s = c[metric]
            for stat in (
                "n",
                "median",
                "mean",
                "std",
                "cv",
                "ci_low",
                "ci_high",
                "ci_coverage",
                "outliers",
            ):
                want = s[stat] if s else (0 if stat == "n" else None)
                assert r[f"{metric}_{stat}"] == want, (metric, stat)
    ids = [(g["group_id"], c["config_id"]) for g in doc["groups"] for c in g["configs"]]
    assert ids == sorted(ids)
    assert [(r["group_id"], r["config_id"]) for r in table] == ids


def test_unicode_params_and_ids_round_trip() -> None:
    from dataclasses import replace

    plan = [
        replace(r, params={"名前": "ß" + chr(0x202E) + "✓", "n": 1}) for r in runs("a")
    ]
    labels = build_labels(CAMP, plan, series("a", [1, 2, 3]))
    out = pq.read_table(io.BytesIO(lx.labels_parquet_bytes(labels))).to_pylist()
    import json

    assert json.loads(out[0]["params"]) == {"名前": "ß" + chr(0x202E) + "✓", "n": 1}
    assert lx.labels_parquet_bytes(labels) == lx.labels_parquet_bytes(labels)
