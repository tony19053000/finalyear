"""V0.15 G3 step 0 - the G2 tech-debt carry items (STATUS, reviewer non-blocking):

* the conformal rank in integer-safe form: equal to the exact rational
  ``ceil((n + 1)(1 - alpha))`` for alpha in {0.05, 0.1, 0.2} (the brief's set)
  and 0.7 (where the bare float form is off by one) and every n in [1, 2000];
* the report says how many outer folds the importance came from, and the
  Markdown states that a baseline fold counts as delta = 0;
* the watermark's JSON form names every field (the retrain task stores it).
"""

from __future__ import annotations

import datetime as dt
import math
from fractions import Fraction

import pytest

pytest.importorskip("numpy", reason="needs the controller extra")

import numpy as np

from meshforge.controller.ml.loader import Watermark
from meshforge.controller.ml.train.conformal import conformal_q, conformal_rank

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("alpha", [0.05, 0.1, 0.2, 0.7])
def test_conformal_rank_is_the_exact_rational_ceiling(alpha: float) -> None:
    exact_alpha = Fraction(str(alpha))
    for n in range(1, 2001):
        expected = math.ceil((n + 1) * (1 - exact_alpha))
        assert conformal_rank(n, alpha) == expected, (alpha, n)


def test_the_float_form_was_wrong_where_the_rank_is_an_integer() -> None:
    # (9 + 1)(1 - 0.7) = 3 exactly; in floats 3.0000000000000004 -> ceil 4
    assert math.ceil((9 + 1) * (1.0 - 0.7)) == 4
    assert conformal_rank(9, 0.7) == 3
    residuals = np.arange(1.0, 10.0)  # 1..9
    assert conformal_q(residuals, 0.7) == 3.0


def test_watermark_json_names_every_field() -> None:
    at = dt.datetime(2026, 10, 22, 1, 2, 3, tzinfo=dt.UTC)
    mark = Watermark(
        matched_runs=3,
        newest_matched=(at, 9),
        ended_attempts=5,
        newest_end=at,
        feature_version=1,
        max_runs=100,
        run_records=4,
        newest_record=at,
    )
    doc = mark.as_json()
    assert doc == {
        "matched_runs": 3,
        "newest_matched": [at.isoformat(), 9],
        "ended_attempts": 5,
        "newest_end": at.isoformat(),
        "feature_version": 1,
        "max_runs": 100,
        "run_records": 4,
        "newest_record": at.isoformat(),
    }
    # a later run record alone changes the key
    assert mark != Watermark(
        matched_runs=3,
        newest_matched=(at, 9),
        ended_attempts=5,
        newest_end=at,
        feature_version=1,
        max_runs=100,
        run_records=5,
        newest_record=at + dt.timedelta(seconds=1),
    )


def test_report_names_importance_folds_and_the_baseline_delta() -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    from mf_ml_synthetic import synthetic_rows

    from meshforge.controller.ml.train.compare import CompareSettings, compare
    from meshforge.controller.ml.train.data import table_from_rows
    from meshforge.controller.ml.train.report import markdown, report

    table = table_from_rows(
        synthetic_rows("signal", families=5, groups_per_family=4, runs_per_group=8)
    )
    settings = CompareSettings(
        terms=("compute",),
        algorithms=("ridge",),
        min_rows=50,
        bootstrap_resamples=200,
        quantile_alternative=False,
    )
    results = compare(table, settings)
    doc = report(table, settings, results, node_failures=None, sidecar="missing")
    (term,) = doc["terms"]
    folds = term["importance_folds"]
    assert folds["total"] == len(term["outer"])
    assert folds["learned"] == sum(
        1 for f in term["outer"] if not f["selected"].startswith(("raw", "calib"))
    )
    text = markdown(doc)
    assert "calibration: delta = 0" in text
    assert f"(from {folds['learned']} of {folds['total']} folds)" in text
