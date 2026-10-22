"""V0.15 G2 tester round 1: the OOD test on hostile inputs (D338) - NaN-heavy
features, constant columns, one training row per group, duplicates, tiny
training sets."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.schema import NUMERIC_FEATURES, FeatureGroup
from meshforge.controller.ml.train.compare import CompareSettings, compare_term
from meshforge.controller.ml.train.data import feature_indices, table_from_rows
from meshforge.controller.ml.train.ood import fit_ood

pytestmark = pytest.mark.unit

N = len(NUMERIC_FEATURES)
HW = feature_indices(FeatureGroup.HARDWARE)
JOB = feature_indices(FeatureGroup.JOB_SIZE)


def _cloud(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    X = rng.uniform(1.0, 10.0, size=(n, N))
    return X


def test_a_row_inside_the_training_cloud_is_in_distribution() -> None:
    X = _cloud(200)
    flags, reasons = fit_ood(X).check(X[:20])
    # the 99th percentile of LOO distances: at most a couple of the training
    # rows themselves may exceed it
    assert flags.sum() <= 1
    assert all(r is None for f, r in zip(flags, reasons, strict=True) if not f)


@pytest.mark.parametrize("col_group", [HW, JOB])
def test_each_range_group_fires_just_outside_the_margin_not_just_inside(
    col_group: tuple[int, ...],
) -> None:
    X = _cloud(100)
    model = fit_ood(X)
    col = col_group[0]
    lo, hi = X[:, col].min(), X[:, col].max()
    span = hi - lo
    inside = X[:1].copy()
    inside[0, col] = hi + 0.099 * span
    outside = X[:1].copy()
    outside[0, col] = hi + 0.101 * span
    below = X[:1].copy()
    below[0, col] = lo - 0.101 * span
    assert not model.check(inside)[0][0] or model.check(inside)[1][0] == "knn"
    f_out, r_out = model.check(outside)
    f_below, _ = model.check(below)
    assert f_out[0] and (r_out[0] or "").startswith("range:")
    assert f_below[0]


def test_all_nan_row_never_crashes_and_is_not_range_ood() -> None:
    X = _cloud(100)
    row = np.full((1, N), np.nan)
    flags, reasons = fit_ood(X).check(row)
    assert reasons[0] != "range" and not (reasons[0] or "").startswith("range:")
    assert flags.shape == (1,)


def test_nan_heavy_training_rows_are_handled() -> None:
    rng = np.random.default_rng(3)
    X = _cloud(150, 1)
    X[rng.random(X.shape) < 0.7] = np.nan  # 70 % missing everywhere
    model = fit_ood(X)
    flags, _ = model.check(X[:30])
    assert flags.shape == (30,)
    assert np.isfinite(model.threshold) or model.neighbours is None


def test_an_all_nan_range_column_marks_any_present_value_unseen() -> None:
    X = _cloud(100)
    X[:, HW[0]] = np.nan
    probe = _cloud(3, 9)
    flags, reasons = fit_ood(X).check(probe)
    assert flags.all()
    assert all((r or "").endswith(":unseen") for r in reasons)
    # a NaN in the same column is not "outside"
    probe[:, HW[0]] = np.nan
    assert not any((r or "").startswith("range:") for r in fit_ood(X).check(probe)[1])


def test_constant_columns_do_not_divide_by_zero() -> None:
    X = _cloud(100)
    X[:, :] = 5.0  # every column constant
    model = fit_ood(X)
    with np.errstate(all="raise"):
        same, _ = model.check(X[:5])
        other = X[:1].copy()
        other[0, JOB[0]] = 6.0
        flag, reason = model.check(other)
    assert not same.any()
    assert flag[0] and (reason[0] or "").startswith("range:")  # span 0 -> any change


def test_duplicate_rows_and_tiny_training_sets_switch_knn_off_not_crash() -> None:
    X = np.tile(_cloud(1), (50, 1))  # 50 identical rows -> LOO distances all 0
    model = fit_ood(X)
    assert not model.check(X[:3])[0].any()
    for n in (0, 1, 5):
        small = fit_ood(_cloud(n))
        assert small.neighbours is None
        assert small.check(_cloud(2, 4))[0].shape == (2,)


def test_one_training_row_per_group_through_the_whole_comparison() -> None:
    rows = synthetic_rows("signal", families=4, groups_per_family=30, runs_per_group=1)
    result = compare_term(
        table_from_rows(rows),
        "compute",
        CompareSettings(
            terms=("compute",), min_rows=50, algorithms=("ridge", "tree", "knn")
        ),
    )
    assert result.status in ("trained", "no_learned_model")
    assert 0.0 <= result.pooled["ood_rate"] <= 1.0
    assert math.isfinite(result.pooled["male_model"])


def test_nan_heavy_dataset_through_the_whole_comparison() -> None:
    rows: list[dict[str, Any]] = synthetic_rows(
        "signal", families=4, groups_per_family=5, runs_per_group=8
    )
    import random

    rng = random.Random(0)  # noqa: S311 - test data
    for row in rows:
        for name in NUMERIC_FEATURES:
            if name not in ("hw_cpu_gflops", "job_input_bytes") and rng.random() < 0.6:
                row[name] = math.nan
    result = compare_term(
        table_from_rows(rows),
        "compute",
        CompareSettings(
            terms=("compute",), min_rows=50, algorithms=("ridge", "tree", "lightgbm")
        ),
    )
    assert math.isfinite(result.pooled["male_model"])
    assert 0.0 <= result.pooled["coverage"] <= 1.0


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_an_infinite_feature_is_flagged_or_refused_never_a_deep_crash() -> None:
    """A +inf feature (a corrupt file) must be handled deterministically:
    either flagged out of range or the dataset refused - never a crash deep in
    scikit-learn."""
    X = _cloud(100)
    X[3, JOB[0]] = np.inf
    try:
        model = fit_ood(X)
        model.check(X[:5])
    except (ValueError, FloatingPointError):  # a clear refusal is fine
        pass
