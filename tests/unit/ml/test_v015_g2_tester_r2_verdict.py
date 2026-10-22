"""V0.15 G2 tester round 2: fresh adversarial checks of the four round-1 fixes
(D357 better-verdict rule, D356 per-family coverage reject, corrupt-value
refusal, silent exp overflow). Brief §5b."""

from __future__ import annotations

import json
import math
import random
import warnings
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

import numpy as np
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.train.compare import (
    CompareSettings,
    TermResult,
    compare,
    compare_term,
)
from meshforge.controller.ml.train.data import (
    DatasetValueError,
    load_bytes,
    table_from_rows,
)
from meshforge.controller.ml.train.report import canonical_json, report

pytestmark = pytest.mark.unit

FAST = CompareSettings(
    terms=("compute",),
    min_rows=50,
    algorithms=("ridge", "tree", "lightgbm"),
    quantile_alternative=False,
)


def _run(rows: list[dict[str, Any]], settings: CompareSettings = FAST) -> TermResult:
    return compare_term(table_from_rows(rows), "compute", settings)


def _noise(seed: int, families: int, sigma: float = 0.3) -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "node_noise",
        families=families,
        groups_per_family=5,
        runs_per_group=10,
        seed=seed,
    )
    rng = random.Random(seed * 31 + 5)  # noqa: S311 - test data
    for row in rows:
        row["y"] = rng.gauss(0.0, sigma)
        row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    return rows


# ------------------------------------------------------------ T1-D1 / D357


@pytest.mark.parametrize("families", [3, 5, 6, 8])
@pytest.mark.parametrize("seed", range(6))
def test_d357_noise_sweep_is_never_better(families: int, seed: int) -> None:
    result = _run(_noise(seed, families))
    assert result.verdict["verdict"] != "better", (families, seed, result.verdict)
    if not result.final.selected.learned:  # type: ignore[union-attr]
        assert result.verdict["verdict"] == "no_learned_model"
        assert result.status == "no_learned_model"


@pytest.mark.parametrize("sigma", [0.01, 0.1, 0.8])
def test_d357_noise_at_other_scales_is_never_better(sigma: float) -> None:
    for seed in range(3):
        assert _run(_noise(seed, 6, sigma)).verdict["verdict"] != "better"


@pytest.mark.parametrize("families", [5, 6, 8])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_d357_a_strong_learned_signal_is_still_better(families: int, seed: int) -> None:
    """The fix must not make `better` unreachable."""
    result = _run(
        synthetic_rows(
            "signal",
            families=families,
            groups_per_family=5,
            runs_per_group=12,
            noise=0.02,
            seed=seed,
        )
    )
    assert result.verdict["verdict"] == "better", result.verdict
    assert result.final.selected.learned  # type: ignore[union-attr]
    assert result.verdict["high"] < -result.verdict["min_effect"]
    assert result.verdict["learned_folds"] * 2 >= result.verdict["folds"]


@pytest.mark.parametrize("families", [3, 4])
def test_d357_fewer_than_five_families_is_never_better(families: int) -> None:
    result = _run(
        synthetic_rows(
            "signal",
            families=families,
            groups_per_family=5,
            runs_per_group=12,
            noise=0.02,
        )
    )
    assert result.verdict["verdict"] == "not_separable"
    assert result.verdict["reason"] == "too_few_families"
    assert result.verdict["families"] == families


def test_d357_calibration_selected_means_no_learned_model() -> None:
    """Calibration's own truth, no noise structure to learn -> a baseline
    ships; the headline must not be `better`."""
    only_baselines = CompareSettings(
        terms=("compute",), min_rows=50, algorithms=(), quantile_alternative=False
    )
    result = _run(_noise(0, 6), only_baselines)
    assert not result.final.selected.learned  # type: ignore[union-attr]
    assert result.verdict["verdict"] == "no_learned_model"
    assert result.verdict["reason"] == "no_learned_model_ships"
    assert result.verdict["families"] == 6  # the numbers remain


def test_d357_min_effect_is_a_real_gate() -> None:
    """With a huge min_effect even a genuine signal cannot be `better`."""
    strict = CompareSettings(
        terms=("compute",),
        min_rows=50,
        algorithms=("ridge", "tree", "lightgbm"),
        quantile_alternative=False,
        min_effect_log=50.0,
    )
    result = _run(
        synthetic_rows("signal", families=6, groups_per_family=5, runs_per_group=12),
        strict,
    )
    assert result.verdict["verdict"] != "better"


# ------------------------------------------------------------ T1-D2 / D356


def _shifted(n_rows: int, shift: float = 1.0) -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "signal", families=8, groups_per_family=5, runs_per_group=12, noise=0.05
    )
    out: list[dict[str, Any]] = []
    seen = 0
    for row in rows:
        if row["family"] == "fam07":
            seen += 1
            if seen > n_rows:
                continue
            row["y"] += shift
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
        out.append(row)
    return out


def test_d356_a_20_row_uncovered_family_rejects() -> None:
    result = _run(_shifted(20))
    assert result.per_family["fam07"]["rows"] == 20
    assert result.per_family["fam07"]["coverage"] < 0.7
    assert result.would_reject["families_low_coverage"] == ["fam07"]
    assert result.would_reject["any"] is True


def test_d356_a_19_row_uncovered_family_does_not_set_the_family_flag() -> None:
    result = _run(_shifted(19))
    assert result.per_family["fam07"]["rows"] == 19
    assert result.per_family["fam07"]["coverage"] < 0.7
    assert "fam07" not in result.would_reject["families_low_coverage"]


def test_d356_well_covered_families_do_not_trigger() -> None:
    rows = synthetic_rows(
        "signal", families=8, groups_per_family=5, runs_per_group=12, noise=0.05
    )
    result = _run(rows)
    assert result.would_reject["families_low_coverage"] == []


def test_d356_the_floor_and_threshold_are_settings() -> None:
    lenient = CompareSettings(
        terms=("compute",),
        min_rows=50,
        algorithms=("ridge", "tree"),
        quantile_alternative=False,
        family_coverage_min_rows=1000,
    )
    assert _run(_shifted(60), lenient).would_reject["families_low_coverage"] == []


def test_d356_would_reject_lands_in_the_report() -> None:
    table = table_from_rows(_shifted(60))
    results = compare(
        table,
        CompareSettings(terms=("compute",), min_rows=50, algorithms=("ridge", "tree")),
    )
    doc = report(
        table,
        CompareSettings(terms=("compute",), min_rows=50, algorithms=("ridge", "tree")),
        results,
        node_failures=None,
        sidecar="missing",
    )
    text = canonical_json(doc)
    assert "families_low_coverage" in text and "fam07" in text
    json.loads(text)


# ------------------------------------------------------------ T1-D4 corrupt values


def _bad(rows: list[dict[str, Any]], column: str, value: float) -> list[dict[str, Any]]:
    rows[3][column] = value
    return rows


def BASE() -> list[dict[str, Any]]:  # noqa: N802 - a tiny fixture factory
    return synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("y", float("nan")),
        ("y", float("inf")),
        ("y", float("-inf")),
        ("raw_s", float("nan")),
        ("raw_s", float("inf")),
        ("raw_s", -1.0),
        ("raw_s", 0.0),
        ("actual_s", float("nan")),
        ("actual_s", float("inf")),
        ("actual_s", -3.0),
        ("actual_s", 0.0),
        ("load_cpu_percent", float("inf")),
        ("load_cpu_percent", float("-inf")),
    ],
)
def test_corrupt_values_are_refused_with_a_clear_error(
    column: str, value: float
) -> None:
    with pytest.raises(DatasetValueError) as info:
        table_from_rows(_bad(BASE(), column, value))
    assert "never writes" in str(info.value)
    assert "\n" not in str(info.value)


def test_the_parquet_load_path_refuses_too() -> None:
    from meshforge.controller.ml.dataset import parquet_bytes, sort_key

    rows = _bad(BASE(), "y", float("nan"))
    data = parquet_bytes(sorted(rows, key=sort_key))
    with pytest.raises(DatasetValueError):
        load_bytes(data)


def test_a_negative_log_target_is_legitimate() -> None:
    rows = BASE()
    for row in rows:
        row["y"] = -2.5
        row["actual_s"] = row["raw_s"] * math.exp(-2.5)
    table_from_rows(rows)


def test_several_problems_are_all_counted() -> None:
    rows = BASE()
    rows[0]["y"] = float("nan")
    rows[1]["y"] = float("inf")
    rows[2]["raw_s"] = -1.0
    with pytest.raises(
        DatasetValueError, match=r"2 row\(s\) with a non-finite y.*raw_s"
    ):
        table_from_rows(rows)


# ------------------------------------------------------------ T1-D3 exp overflow


def test_ape_overflow_is_silent() -> None:
    from meshforge.controller.ml.train.metrics import HeldOut, summary

    n = 6
    held = HeldOut(
        family=("a",) * n,
        y=np.zeros(n),
        raw_s=np.ones(n),
        actual_s=np.ones(n),
        model=np.full(n, 1e4),  # exp(1e4) overflows
        calibration=np.zeros(n),
        q=np.full(n, 0.1),
        ood=np.zeros(n, dtype=bool),
        learned=np.ones(n, dtype=bool),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = summary(held, max_interval_log=1.0)
    assert out["rows"] == n  # the report layer nulls the inf; no warning leaked


@pytest.mark.parametrize("scale", [1e-30, 1e-12, 1e12, 1e30])
def test_extreme_target_scales_are_finite_and_silent(scale: float) -> None:
    rows = synthetic_rows("signal", families=4, groups_per_family=4, runs_per_group=6)
    for row in rows:
        row["raw_s"] *= scale
        row["actual_s"] *= scale
    settings = CompareSettings(
        terms=("compute",), min_rows=50, algorithms=("ridge", "tree")
    )
    table = table_from_rows(rows)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        results = compare(table, settings)
        text = canonical_json(
            report(table, settings, results, node_failures=None, sidecar="missing")
        )
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)


def test_extreme_extrapolation_to_a_far_family_is_silent() -> None:
    rows = synthetic_rows(
        "signal", families=5, groups_per_family=4, runs_per_group=8, far_family=True
    )
    for row in rows:
        if row["family"] == "far":
            row["load_cpu_percent"] = 1e9
            row["raw_s"] *= 1e6
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    settings = CompareSettings(terms=("compute",), min_rows=50, algorithms=("ridge",))
    table = table_from_rows(rows)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        results = compare(table, settings)
        text = canonical_json(
            report(table, settings, results, node_failures=None, sidecar="missing")
        )
    assert "NaN" not in text and "Infinity" not in text


# ------------------------------------------------------------ determinism


def test_the_verdict_and_report_are_deterministic() -> None:
    rows = synthetic_rows("signal", families=6, groups_per_family=5, runs_per_group=10)
    settings = CompareSettings(
        terms=("compute",), min_rows=50, algorithms=("ridge", "tree")
    )
    outs = []
    for _ in range(2):
        table = table_from_rows(list(reversed(rows)))
        results = compare(table, settings)
        outs.append(
            canonical_json(
                report(table, settings, results, node_failures=None, sidecar="missing")
            )
        )
    assert outs[0] == outs[1]
