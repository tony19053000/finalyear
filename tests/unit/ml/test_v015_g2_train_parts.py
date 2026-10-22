"""V0.15 G2: the training library's parts, one by one (D337, D338, D349-D352).

prep (fit on train only, NaN -> median + indicator, all-NaN column, family
columns), conformal (rank arithmetic, too few residuals), the one-SE rule,
OOD (range margin, NaN ignored, unseen feature, kNN), the boosting prefix,
the calibration baseline (= the planner's own fit, training rows only,
measured / unscaled terms -> 0), the data loader (versions, columns, shuffled
file), the paired verdict.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.dataset import DATASET_METADATA_KEY
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.train import algorithms as alg
from meshforge.controller.ml.train.conformal import conformal_q, covered
from meshforge.controller.ml.train.data import (
    DatasetVersionError,
    load,
    load_bytes,
    table_from_rows,
)
from meshforge.controller.ml.train.metrics import paired_verdict
from meshforge.controller.ml.train.ood import fit_ood
from meshforge.controller.ml.train.prep import fit_prep, native, transform
from meshforge.controller.ml.train.select import ConfigScore, one_se_pick
from meshforge.controller.planner.calibration import (
    CalibrationConfig,
    MatchedRun,
    TermObservation,
    fit,
)

pytestmark = pytest.mark.unit

NAN = math.nan
COL = {name: j for j, name in enumerate(NUMERIC_FEATURES)}


# ------------------------------------------------------------------ prep


def test_prep_imputes_with_training_medians_and_marks_missing() -> None:
    X = np.array([[1.0, NAN], [3.0, 4.0], [5.0, 8.0]])
    prep = fit_prep(X)
    assert prep.medians.tolist() == [3.0, 6.0]
    assert prep.indicator_cols == (1,)
    Z = transform(prep, np.array([[NAN, NAN]]), standardise=False)
    # NaN -> the training median, never 0; the indicator says it was missing
    assert Z.tolist() == [[3.0, 6.0, 1.0]]


def test_prep_uses_only_the_rows_it_is_fitted_on() -> None:
    train = np.array([[1.0], [2.0], [3.0]])
    test = np.array([[1000.0]])
    a = fit_prep(train)
    b = fit_prep(np.vstack([train, test]))
    assert a.medians[0] == 2.0 and b.medians[0] == 2.5
    assert a.mean[0] == 2.0


def test_an_all_nan_column_is_constant_and_flagged() -> None:
    X = np.array([[NAN, 1.0], [NAN, 2.0]])
    prep = fit_prep(X)
    Z = transform(prep, X, standardise=True)
    assert np.isfinite(Z).all()
    assert prep.indicator_cols == (0,)
    assert (Z[:, 0] == Z[0, 0]).all()  # constant, so its value carries nothing


def test_standardisation_has_unit_scale_and_a_constant_column_keeps_scale_one() -> None:
    X = np.array([[1.0, 7.0], [3.0, 7.0]])
    prep = fit_prep(X)
    Z = transform(prep, X, standardise=True)
    assert Z[:, 0].tolist() == [-1.0, 1.0]
    assert prep.std[1] == 1.0 and Z[:, 1].tolist() == [0.0, 0.0]


def test_family_columns_only_with_the_flag_and_an_unseen_family_is_zero() -> None:
    X = np.array([[1.0], [2.0]])
    assert fit_prep(X, ["a", "b"]).family_levels == ()
    prep = fit_prep(X, ["a", "b"], with_family=True)
    assert prep.family_levels == ("a", "b")
    Z = transform(prep, np.array([[1.0]]), ["held_out"], standardise=False)
    assert Z.tolist() == [[1.0, 0.0, 0.0]]
    N = native(prep, np.array([[NAN]]), ["b"])
    assert math.isnan(N[0, 0]) and N[0, 1:].tolist() == [0.0, 1.0]


# ------------------------------------------------------------------ conformal


def test_conformal_q_is_the_ceil_rank_order_statistic() -> None:
    residuals = np.arange(1.0, 20.0)  # n = 19 -> rank ceil(20 x 0.9) = 18
    assert conformal_q(residuals, 0.1) == 18.0
    residuals = np.arange(1.0, 10.0)  # n = 9 -> rank 9
    assert conformal_q(residuals, 0.1) == 9.0


def test_conformal_q_is_infinite_with_too_few_residuals() -> None:
    assert conformal_q(np.arange(1.0, 9.0), 0.1) == math.inf  # rank 9 > n = 8
    assert conformal_q(np.array([]), 0.1) == math.inf
    with pytest.raises(ValueError):
        conformal_q(np.array([1.0]), 0.0)


def test_conformal_coverage_on_exchangeable_data() -> None:
    rng = np.random.default_rng(3)
    calib = rng.normal(0.0, 1.0, 2000)
    test = rng.normal(0.0, 1.0, 20000)
    q = conformal_q(np.abs(calib), 0.1)
    coverage = covered(test, np.zeros_like(test), q).mean()
    assert 0.88 <= coverage <= 0.92


# ------------------------------------------------------------------ 1-SE rule


def _score(name: str, index: int, mean: float, se: float) -> ConfigScore:
    return ConfigScore(alg.Config(name, (), index), mean, se, ())


def test_one_se_picks_the_simplest_within_one_standard_error() -> None:
    scores = [
        _score("calibration", 0, 0.30, 0.02),
        _score("ridge", 0, 0.205, 0.01),
        _score("lightgbm", 0, 0.20, 0.01),
    ]
    best, selected = one_se_pick(scores)
    assert best.algorithm == "lightgbm"
    assert selected.algorithm == "ridge"  # argmin would say lightgbm


def test_one_se_keeps_the_best_when_nothing_simpler_is_close() -> None:
    scores = [_score("raw", 0, 0.5, 0.01), _score("forest", 1, 0.1, 0.01)]
    assert one_se_pick(scores)[1].algorithm == "forest"


# ------------------------------------------------------------------ OOD


def _ood_rows(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    X = np.full((n, len(NUMERIC_FEATURES)), NAN)
    X[:, COL["hw_cpu_gflops"]] = rng.uniform(100.0, 200.0, n)
    X[:, COL["job_input_bytes"]] = rng.uniform(1e6, 2e6, n)
    X[:, COL["load_cpu_percent"]] = rng.uniform(0.0, 100.0, n)
    return X


def test_ood_range_margin_is_ten_percent_of_the_span() -> None:
    X = _ood_rows(200)
    model = fit_ood(X)
    lo = X[:, COL["hw_cpu_gflops"]].min()
    hi = X[:, COL["hw_cpu_gflops"]].max()
    span = hi - lo
    probe = np.repeat(X[:1], 3, axis=0)
    probe[:, COL["hw_cpu_gflops"]] = [
        hi + 0.05 * span,
        hi + 0.15 * span,
        lo - 0.15 * span,
    ]
    flags, reasons = model.check(probe)
    assert flags[1] and flags[2]
    assert reasons[1] == "range:hw_cpu_gflops"
    assert not (reasons[0] or "").startswith("range")  # inside the margin


def test_ood_ignores_nan_and_flags_an_unseen_feature() -> None:
    X = _ood_rows(100)
    model = fit_ood(X)
    probe = X[:2].copy()
    probe[0, COL["hw_cpu_gflops"]] = NAN  # unknown is not "outside"
    probe[1, COL["hw_gpu_tflops"]] = 5.0  # never had a value in training
    flags, reasons = model.check(probe)
    assert not str(reasons[0]).startswith("range")
    assert flags[1] and reasons[1] == "range:hw_gpu_tflops:unseen"


def test_ood_knn_flags_a_combination_never_seen() -> None:
    X = _ood_rows(300)
    # a load feature (not range-checked) far away: only the kNN part can see it
    probe = X[:1].copy()
    probe[0, COL["load_cpu_percent"]] = 10_000.0
    flags, reasons = fit_ood(X).check(probe)
    assert flags[0] and reasons[0] == "knn"
    inside, _ = fit_ood(X).check(X[:50])
    assert inside.mean() < 0.1  # ~1 % by construction (99th percentile)


def test_ood_knn_is_off_with_too_few_rows() -> None:
    X = _ood_rows(5)
    model = fit_ood(X)
    assert model.neighbours is None


# ------------------------------------------------------------------ algorithms


def _table(**kw: object):  # type: ignore[no-untyped-def]
    return table_from_rows(synthetic_rows("signal", **kw))  # type: ignore[arg-type]


def test_boosting_prefix_equals_a_separately_trained_model() -> None:
    table = _table(families=3, groups_per_family=3, runs_per_group=10)
    rows = np.arange(table.n)
    ctx = alg.FitContext(table=table)
    for name in ("lightgbm", "xgboost"):
        configs = [c for c in alg.configs_for([name]) if c.algorithm == name]
        shared = alg.fit_many(configs, ctx, rows)
        for config in configs:
            alone = alg.fit_many([config], ctx, rows)[config]
            np.testing.assert_array_equal(
                shared[config].predict(table.X, rows), alone.predict(table.X, rows)
            )


def test_every_algorithm_is_deterministic() -> None:
    table = _table(families=3, groups_per_family=3, runs_per_group=10)
    rows = np.arange(table.n)
    ctx = alg.FitContext(table=table)
    configs = alg.configs_for(alg.LEARNED)
    a = alg.fit_many(configs, ctx, rows)
    b = alg.fit_many(configs, ctx, rows)
    for config in configs:
        np.testing.assert_array_equal(
            a[config].predict(table.X, rows), b[config].predict(table.X, rows)
        )


def test_grid_is_small_and_ordered_by_simplicity() -> None:
    assert all(len(grid) <= 4 for grid in alg.GRID.values())
    configs = alg.configs_for(alg.LEARNED)
    assert configs == sorted(configs, key=lambda c: c.rank)
    assert [c.algorithm for c in configs[:2]] == ["raw", "calibration"]
    with pytest.raises(ValueError):
        alg.configs_for(["catboost"])


def test_calibration_baseline_is_the_planners_fit_on_training_rows_only() -> None:
    table = _table(families=3, groups_per_family=2, runs_per_group=10)
    train = np.arange(0, table.n, 2)
    test = np.arange(1, table.n, 2)
    seen: list[tuple[str, np.ndarray]] = []
    ctx = alg.FitContext(
        table=table, observer=lambda kind, fold, rows: seen.append((kind, rows))
    )
    model = alg.fit_one(alg.Config("calibration"), ctx, train)
    assert [kind for kind, _ in seen] == ["calibration"]
    np.testing.assert_array_equal(seen[0][1], train)
    # by hand: the planner's fit on the same training rows, node-level lookup
    runs = [
        MatchedRun(
            decision_id=table.decision_id[i],
            attempt_id=table.attempt_id[i],
            node_id=table.node_id[i],
            finished_at=table.finished_at[i],
            terms=(
                TermObservation(
                    "compute", float(table.raw_s[i]), float(table.actual_s[i]), True
                ),
            ),
            family=table.family_raw[i],
            fingerprint=table.group[i],
        )
        for i in train
    ]
    cal = fit(runs, CalibrationConfig())
    got = model.predict(table.X[test], test)
    for k, i in enumerate(test):
        found = cal.lookup("compute", table.node_id[i], cal.family_of(table.group[i]))
        assert found is not None
        assert got[k] == pytest.approx(math.log(found[0].factor), abs=0)


def test_calibration_baseline_never_scales_measured_or_unscaled_terms() -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=6)
    for row in rows[: len(rows) // 2]:
        row["source"] = "measured"
    rows += synthetic_rows(
        "signal", families=3, groups_per_family=2, runs_per_group=6, terms=("combine",)
    )
    table = table_from_rows(rows)
    all_rows = np.arange(table.n)
    model = alg.fit_one(
        alg.Config("calibration"), alg.FitContext(table=table), all_rows
    )
    pred = model.predict(table.X, all_rows)
    for k, i in enumerate(all_rows):
        if table.source[i] == "measured" or table.term[i] == "combine":
            assert pred[k] == 0.0  # factor 1: the live planner leaves it raw
    assert (pred != 0.0).any()


# ------------------------------------------------------------------ data


def test_a_shuffled_file_loads_the_same_rows_and_digest(tmp_path: Path) -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=5)
    a = load(parquet_file(rows, tmp_path / "a.parquet"))
    b = load(parquet_file(rows, tmp_path / "b.parquet", shuffle_seed=7))
    assert (tmp_path / "a.parquet").read_bytes() != (
        tmp_path / "b.parquet"
    ).read_bytes()
    assert a.digest == b.digest
    np.testing.assert_array_equal(a.y, b.y)
    assert a.attempt_id == b.attempt_id


def test_the_digest_of_a_canonical_file_is_its_g1_digest(tmp_path: Path) -> None:
    import hashlib

    rows = synthetic_rows("signal", families=2, groups_per_family=2, runs_per_group=3)
    path = parquet_file(rows, tmp_path / "d.parquet")
    assert (
        load(path).digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_an_unknown_dataset_version_is_refused(tmp_path: Path) -> None:
    rows = synthetic_rows("signal", families=2, groups_per_family=2, runs_per_group=3)
    path = parquet_file(rows, tmp_path / "d.parquet")
    table = pq.read_table(path)
    meta = dict(table.schema.metadata)
    meta[DATASET_METADATA_KEY] = b"99"
    sink = pa.BufferOutputStream()
    pq.write_table(table.replace_schema_metadata(meta), sink)
    with pytest.raises(DatasetVersionError, match="99"):
        load_bytes(sink.getvalue().to_pybytes())
    sink = pa.BufferOutputStream()
    pq.write_table(table.drop_columns(["y"]), sink)
    with pytest.raises(DatasetVersionError, match="columns"):
        load_bytes(sink.getvalue().to_pybytes())


def test_nan_features_stay_nan_in_the_table() -> None:
    rows = synthetic_rows("signal", families=2, groups_per_family=1, runs_per_group=2)
    table = table_from_rows(rows)
    assert np.isnan(table.X[:, COL["hw_gpu_tflops"]]).all()
    assert not np.isnan(table.X[:, COL["hw_cpu_gflops"]]).any()


# ------------------------------------------------------------------ verdict


def test_paired_verdict() -> None:
    kw = {"seed": 1, "resamples": 2000}
    assert paired_verdict([-0.1, -0.12, -0.09, -0.11], **kw)["verdict"] == "better"
    assert paired_verdict([0.1, 0.12, 0.09, 0.11], **kw)["verdict"] == "worse"
    assert paired_verdict([-0.1, 0.1, -0.05, 0.08], **kw)["verdict"] == "not_separable"
    assert paired_verdict([-0.5], **kw)["verdict"] == "not_separable"  # one family
    assert paired_verdict([], **kw)["verdict"] == "not_separable"
    a = paired_verdict([-0.1, 0.02, -0.3], **kw)
    assert a == paired_verdict([-0.3, -0.1, 0.02], **kw)  # order-free, seeded


def test_settings_are_frozen() -> None:
    from meshforge.controller.ml.train.compare import CompareSettings

    with pytest.raises(dataclasses.FrozenInstanceError):
        CompareSettings().min_rows = 1  # type: ignore[misc]


# ------------------------------------------------------------------ selection


def test_conformal_residuals_are_out_of_fold_never_in_sample() -> None:
    from meshforge.controller.ml.splits import group_kfold
    from meshforge.controller.ml.train.select import select

    table = _table(families=3, groups_per_family=4, runs_per_group=8, noise=0.3)
    rows = np.arange(table.n)
    deep = alg.Config("tree", (("max_depth", 6),), 1)
    ctx = alg.FitContext(table=table)
    sel = select(ctx, rows, [deep], inner_k=5, seed=0)
    # by hand: each row predicted by a tree that never saw its fingerprint
    expected = np.full(table.n, np.nan)
    for fold in group_kfold(table.split_rows(rows), 5, 0):
        train = rows[list(fold.train)]
        test = rows[list(fold.test)]
        model = alg.fit_one(deep, ctx, train)
        expected[test] = np.abs(table.y[test] - model.predict(table.X[test], test))
    np.testing.assert_array_equal(sel.oof_residuals, expected)
    in_sample = np.abs(table.y - alg.fit_one(deep, ctx, rows).predict(table.X, rows))
    assert in_sample.mean() < sel.oof_residuals.mean()  # the two really differ
