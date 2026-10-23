"""V0.15 G5a step 6 (G2/G3 tech debt) on the ML side.

* the conformal rank is the exact rational ceiling of ``(n + 1)(1 - alpha)``
  (integer arithmetic) - also where a float slack would be too coarse;
* :class:`~meshforge.controller.ml.source.SetPredictor` refuses an answer
  that does not fill every row (``malformed_prediction``) instead of
  dropping the empty slots silently (which shifted the rows).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import pytest

np = pytest.importorskip("numpy", reason="needs the controller extra")

from meshforge.controller.ml.serve import LoadedSet, TermOutput  # noqa: E402
from meshforge.controller.ml.source import (  # noqa: E402
    MALFORMED_PREDICTION,
    SetPredictor,
)
from meshforge.controller.ml.train.conformal import conformal_rank  # noqa: E402
from meshforge.controller.planner.predictor import (  # noqa: E402
    PredictorFailed,
    TermQuery,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("alpha", [0.05, 0.1, 0.2, 0.25, 0.3, 0.7, 0.9])
def test_conformal_rank_is_exact_for_small_n(alpha: float) -> None:
    exact = Fraction(repr(alpha))
    for n in range(2001):
        assert conformal_rank(n, alpha) == math.ceil((n + 1) * (1 - exact)), (n, alpha)


@pytest.mark.parametrize("n", [10**7 - 1, 10**9 - 1, 10**12 - 1, 3 * 10**11 + 7])
def test_conformal_rank_is_exact_where_a_float_slack_is_too_coarse(n: int) -> None:
    # (n + 1)(1 - 0.1) is an integer for most of these n; a float product
    # (or a 1e-9 slack) can land on the wrong side of it at this size
    assert conformal_rank(n, 0.1) == -(-(n + 1) * 9 // 10)
    assert conformal_rank(n, 0.3) == math.ceil(Fraction(n + 1) * Fraction(7, 10))


def test_the_g3_float_slack_was_wrong_at_this_size() -> None:
    # (100000009 + 1)(1 - 0.7) = 30000003 exactly; the float form with a
    # 1e-9 slack asked for one residual more (30000004)
    n = 100_000_009
    assert math.ceil((n + 1) * (1 - 0.7) - 1e-9) == 30_000_004
    assert conformal_rank(n, 0.7) == 30_000_003


class _Model:
    """A member answering ``drop`` rows fewer than asked."""

    version = "sha256:" + "a" * 64
    q = 0.1

    def __init__(self, drop: int = 0, short_reasons: bool = False) -> None:
        self.drop = drop
        self.short_reasons = short_reasons

    def predict(self, rows: Any) -> TermOutput:
        n = rows.shape[0] - self.drop
        reasons: Sequence[str | None] = [None] * (n - 1 if self.short_reasons else n)
        return TermOutput(
            y_hat=np.zeros(n),
            lo=np.zeros(n) - 0.1,
            hi=np.zeros(n) + 0.1,
            ood=np.zeros(n, dtype=bool),
            reasons=tuple(reasons),
            model_version=self.version,
        )


def _predictor(model: _Model) -> SetPredictor:
    loaded = LoadedSet(
        set_version="sha256:" + "b" * 64,
        feature_version=2,
        models={"compute": model},  # type: ignore[dict-item]
    )
    return SetPredictor(loaded)


def _rows(n: int) -> list[TermQuery]:
    return [TermQuery("compute", f"n{i}", (0.0,) * 41) for i in range(n)]


def test_a_full_answer_passes() -> None:
    out = _predictor(_Model()).predict(_rows(3))
    assert len(out) == 3


@pytest.mark.parametrize(
    "model", [_Model(drop=1), _Model(short_reasons=True)], ids=["rows", "reasons"]
)
def test_a_short_answer_is_a_malformed_prediction(model: _Model) -> None:
    with pytest.raises(PredictorFailed) as caught:
        _predictor(model).predict(_rows(3))
    assert caught.value.cause == MALFORMED_PREDICTION
