"""V0.15 G2 tester round 1: the verdict, floors, degenerate targets and the
interval's honesty under distribution shift (brief §5b, D338, D348, D351).

The central question: can the report ever say a learned model is ``better``
than the V0.14 calibration when it is not?
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.train.compare import (
    CompareSettings,
    TermResult,
    compare,
    compare_term,
)
from meshforge.controller.ml.train.data import table_from_rows
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


def _set_y(rows: list[dict[str, Any]], values: list[float]) -> None:
    for row, y in zip(rows, values, strict=True):
        row["y"] = y
        row["actual_s"] = row["raw_s"] * math.exp(max(-50.0, min(50.0, y)))


def _node_truth(seed: int, families: int, shape: str) -> list[dict[str, Any]]:
    """Calibration's truth (per-node constant) plus ``shape`` noise."""
    rows = synthetic_rows(
        "node_noise",
        families=families,
        groups_per_family=5,
        runs_per_group=10,
        seed=seed,
        noise=0.0,
    )
    rng = random.Random(seed * 7919 + 1)  # noqa: S311 - test data
    ys = []
    for row in rows:
        if shape == "gauss":
            e = rng.gauss(0.0, 0.3)
        elif shape == "heavy":  # Cauchy-like, clipped to keep exp() sane
            e = max(-3.0, min(3.0, 0.1 * math.tan(math.pi * (rng.random() - 0.5))))
        elif shape == "exact":
            e = 0.0
        else:
            raise ValueError(shape)
        ys.append(row["y"] + e)
    _set_y(rows, ys)
    return rows


# ---------------------------------------------------------------- never better


@pytest.mark.parametrize("shape", ["gauss", "heavy"])
@pytest.mark.parametrize("families", [3, 5])
def test_calibration_is_the_truth_so_the_model_is_never_better(
    shape: str, families: int
) -> None:
    for seed in range(4):
        result = _run(_node_truth(seed, families, shape))
        assert result.verdict["verdict"] != "better", (shape, families, seed)


def test_noise_free_node_constants_are_learnable_so_better_is_reachable() -> None:
    """Sensitivity: with no noise a tree/LightGBM recovers the per-node
    constants exactly while calibration's shrunk factors stay off - the
    verdict must then be `better` (the gate is not just always-no)."""
    result = _run(_node_truth(0, 5, "exact"))
    assert result.verdict["verdict"] == "better"
    assert all(f.selected.learned for f in result.folds)


def test_a_single_dominant_family_never_makes_a_model_better() -> None:
    for seed in range(4):
        rows = _node_truth(seed, 4, "gauss")
        rng = random.Random(seed)  # noqa: S311 - test data
        keep = [r for r in rows if r["family"] == "fam00" or rng.random() < 0.1]
        result = _run(keep)
        assert result.verdict["verdict"] != "better", seed


def _zero_mean_noise(seed: int, families: int) -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "node_noise",
        families=families,
        groups_per_family=5,
        runs_per_group=10,
        seed=seed,
    )
    rng = random.Random(seed + 1000)  # noqa: S311 - test data
    _set_y(rows, [rng.gauss(0.0, 0.3) for _ in rows])
    return rows


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1-D1 (major): on pure zero-mean noise (y independent of "
    "every feature) every fold selects `raw` - a BASELINE, not a learned "
    "model - yet vs_calibration.verdict is `better` (compare.py:324-338, "
    "metrics.paired_verdict): `model` = the selected config even when it is "
    "raw/calibration, and there is no minimum effect size, so a 0.2-0.8 % MALE "
    "gain that is just calibration over-fitting its per-node factors is "
    "declared `better` (15 of 48 pure-noise runs). The report's headline "
    "verdict must not read `better` when no learned model was selected.",
)
@pytest.mark.parametrize("seed", [10, 2, 7])
def test_pure_noise_is_never_better_when_no_learned_model_was_selected(
    seed: int,
) -> None:
    result = _run(
        _zero_mean_noise(seed, 4),
        CompareSettings(
            terms=("compute",),
            min_rows=50,
            algorithms=("ridge", "tree"),
            quantile_alternative=False,
        ),
    )
    assert not any(f.selected.learned for f in result.folds)
    assert result.verdict["verdict"] != "better"


@pytest.mark.parametrize("seed", range(3))
def test_pure_noise_never_selects_a_learned_model_that_separates(seed: int) -> None:
    """The learned-model half of the same invariant: if a learned model *is*
    picked on pure noise it must not be `better`."""
    result = _run(_zero_mean_noise(seed, 4))
    if any(f.selected.learned for f in result.folds):
        assert result.verdict["verdict"] != "better"


# ---------------------------------------------------------------- shift honesty


def test_conformal_coverage_is_reported_honestly_under_distribution_shift() -> None:
    """One held-out family's residuals sit +1.0 (log) away from every other
    family's: the interval fitted on the others must NOT cover it - and the
    report must say so (coverage low, would-reject), never hide it in a pooled
    average that still looks fine."""
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=12, noise=0.05
    )
    for row in rows:
        if row["family"] == "fam05":
            row["y"] += 1.0
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    result = _run(rows)
    shifted = result.per_family["fam05"]
    assert shifted["coverage"] < 0.2
    assert any(
        part["coverage"] > 0.85
        for name, part in result.per_family.items()
        if name != "fam05"
    )


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1-D2 (minor, = progress-note Q1): would_reject only looks "
    "at POOLED coverage (compare.py:340-346), so one family whose interval "
    "covers 0 % of its rows does not raise would_reject while the other five "
    "cover ~95 % (pooled ~80 %+). A per-family coverage floor is needed for "
    "'honest intervals on families never seen'.",
)
def test_one_uncovered_family_raises_would_reject() -> None:
    rows = synthetic_rows(
        "signal", families=8, groups_per_family=5, runs_per_group=12, noise=0.05
    )
    for row in rows:
        if row["family"] == "fam07":
            row["y"] += 1.0
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    result = _run(rows)
    assert result.per_family["fam07"]["coverage"] < 0.2
    assert result.would_reject["any"]


# ---------------------------------------------------------------- floors


def _trim(rows: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    assert len(rows) >= total
    return rows[:total]


@pytest.mark.parametrize(("total", "status_ok"), [(199, False), (200, True)])
def test_the_row_floor_is_exactly_200(total: int, status_ok: bool) -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=4, runs_per_group=17)
    assert len(rows) == 204
    # drop from the last rows only: every family keeps >= 1 group
    result = compare_term(
        table_from_rows(_trim(rows, total)),
        "compute",
        CompareSettings(algorithms=("ridge",), quantile_alternative=False),
    )
    assert (result.status != "insufficient_data") is status_ok
    assert result.rows == total and result.families == 3


@pytest.mark.parametrize(("families", "status_ok"), [(2, False), (3, True)])
def test_the_family_floor_is_exactly_3(families: int, status_ok: bool) -> None:
    rows = synthetic_rows(
        "signal", families=families, groups_per_family=6, runs_per_group=20
    )
    assert len(rows) >= 200
    result = compare_term(
        table_from_rows(rows),
        "compute",
        CompareSettings(algorithms=("ridge",), quantile_alternative=False),
    )
    assert (result.status != "insufficient_data") is status_ok


def test_a_family_without_a_name_counts_once_and_never_crashes() -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=4, runs_per_group=6)
    for row in rows:
        if row["family"] == "fam02":
            row["family"] = None
    result = _run(rows)
    assert result.families == 3
    assert result.status in ("trained", "no_learned_model")


# ---------------------------------------------------------------- degenerate y


@pytest.mark.parametrize("scale", [1e-12, 1e6, 1e12])
def test_huge_and_tiny_targets_give_a_finite_canonical_report(scale: float) -> None:
    rows = synthetic_rows("signal", families=4, groups_per_family=4, runs_per_group=6)
    for row in rows:
        row["y"] = row["y"] * scale
        row["actual_s"] = row["raw_s"] * math.exp(max(-50.0, min(50.0, row["y"])))
    table = table_from_rows(rows)
    settings = CompareSettings(
        terms=("compute",), min_rows=50, algorithms=("ridge", "tree")
    )
    with pytest.warns(RuntimeWarning) if scale > 1 else _no_warning_check():
        results = compare(table, settings)
    doc = report(table, settings, results, node_failures=None, sidecar="missing")
    text = canonical_json(doc)
    assert "NaN" not in text and "Infinity" not in text
    json.loads(text)
    term = next(t for t in results if t.term == "compute")
    assert term.status in ("trained", "no_learned_model")


class _no_warning_check:  # noqa: N801 - a tiny context manager
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None
