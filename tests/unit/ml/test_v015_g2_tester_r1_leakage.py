"""V0.15 G2 tester round 1: leakage, by black-box perturbation of the
held-out family (brief §5b "Leakage", D347).

Independent of the implementer's observer hook: for each outer family F the
fold that holds F out must not depend on F's rows at all *for anything it
fits* - selection, conformal ``q``, prep, OOD fit, calibration baseline. So
rewriting F's ``y`` (shifted by a constant) must leave that fold's selected
configuration, inner scores, ``q`` and **mean predictions** (model and
calibration baseline) byte-for-byte the same, and wrecking F's *features*
must leave the selection and ``q`` unchanged.

The mean prediction is recovered exactly from MALE: with ``y' = y + c`` and
``c`` far above any residual, ``MALE' = c + mean(y) - mean(y_hat)``.
"""

from __future__ import annotations

import math
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
    compare_term,
)
from meshforge.controller.ml.train.data import table_from_rows

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(
    terms=("compute",),
    min_rows=50,
    algorithms=("ridge", "tree", "lightgbm"),
    quantile_alternative=False,
)
SHIFT = 1000.0


def _rows(seed: int = 5) -> list[dict[str, Any]]:
    return synthetic_rows(
        "signal", families=4, groups_per_family=4, runs_per_group=8, seed=seed
    )


def _run(rows: list[dict[str, Any]]) -> TermResult:
    return compare_term(table_from_rows(rows), "compute", SETTINGS)


@pytest.fixture(scope="module")
def base() -> tuple[list[dict[str, Any]], TermResult]:
    rows = _rows()
    return rows, _run(rows)


@pytest.mark.parametrize("family", ["fam00", "fam01", "fam02", "fam03"])
def test_the_held_out_familys_y_never_reaches_any_fitted_part(
    base: tuple[list[dict[str, Any]], TermResult], family: str
) -> None:
    rows, result = base
    shifted = [dict(r) for r in rows]
    for r in shifted:
        if r["family"] == family:
            r["y"] = r["y"] + SHIFT
            r["actual_s"] = r["raw_s"] * math.exp(min(r["y"], 50.0))
    changed = _run(shifted)
    a = next(f for f in result.folds if f.family == family)
    b = next(f for f in changed.folds if f.family == family)
    assert (a.selected, a.best, a.q, a.inner_k, a.train_rows) == (
        b.selected,
        b.best,
        b.q,
        b.inner_k,
        b.train_rows,
    )
    # the predictions of the fold (model and the calibration baseline)
    ys = [r["y"] for r in rows if r["family"] == family]
    mean_y = sum(ys) / len(ys)
    new = changed.per_family[family]
    pred_model = SHIFT + mean_y - new["male_model"]
    pred_cal = SHIFT + mean_y - new["male_calibration"]
    # reference: a *second* shifted run is deterministic, so compare against
    # an independent shift size - a leak would make the two predictions differ
    shifted2 = [dict(r) for r in rows]
    for r in shifted2:
        if r["family"] == family:
            r["y"] = r["y"] + 2 * SHIFT
            r["actual_s"] = r["raw_s"] * math.exp(min(r["y"], 50.0))
    again = _run(shifted2)
    pred_model2 = 2 * SHIFT + mean_y - again.per_family[family]["male_model"]
    pred_cal2 = 2 * SHIFT + mean_y - again.per_family[family]["male_calibration"]
    assert pred_model == pytest.approx(pred_model2, abs=1e-8)
    assert pred_cal == pytest.approx(pred_cal2, abs=1e-8)


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
@pytest.mark.parametrize("family", ["fam00", "fam02"])
def test_the_held_out_familys_features_never_reach_selection_or_q(
    base: tuple[list[dict[str, Any]], TermResult], family: str
) -> None:
    rows, result = base
    wrecked = [dict(r) for r in rows]
    for r in wrecked:
        if r["family"] == family:
            r["load_cpu_percent"] = 1e6
            r["hw_cpu_gflops"] = 5e5
            r["job_input_bytes"] = 1e15
            r["an_raw_s"] = 1e9
    changed = _run(wrecked)
    a = next(f for f in result.folds if f.family == family)
    b = next(f for f in changed.folds if f.family == family)
    assert (a.selected, a.best, a.q, a.inner_k, a.train_rows) == (
        b.selected,
        b.best,
        b.q,
        b.inner_k,
        b.train_rows,
    )
    # wrecked features are out of distribution however the fit was done
    assert changed.per_family[family]["ood_rate"] == 1.0


def test_the_family_label_itself_is_not_a_feature_by_default() -> None:
    """Renaming every family (a permutation of labels that keeps the
    grouping) must not change any selection: the label is not a feature."""
    rows = _rows(seed=9)
    renamed = [dict(r) for r in rows]
    for r in renamed:
        r["family"] = "z_" + r["family"][::-1]
    a, b = _run(rows), _run(renamed)
    assert [(f.selected, f.q) for f in a.folds] == [(f.selected, f.q) for f in b.folds]
    assert a.pooled["male_model"] == pytest.approx(b.pooled["male_model"], abs=1e-12)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1-D3 (minor): metrics._ape does np.exp(y_hat) on a model's "
    "extrapolation to an out-of-distribution family -> RuntimeWarning overflow "
    "leaks to the CLI's stderr (metrics.py:76); the value is later nulled, so "
    "only the noise is wrong",
)
def test_an_extreme_out_of_distribution_family_does_not_warn() -> None:
    import warnings

    rows = _rows()
    for r in rows:
        if r["family"] == "fam00":
            r["load_cpu_percent"] = 1e6
            r["hw_cpu_gflops"] = 5e5
            r["job_input_bytes"] = 1e15
            r["an_raw_s"] = 1e9
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _run(rows)
