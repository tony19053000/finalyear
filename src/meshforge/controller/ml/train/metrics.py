"""Held-out prediction metrics and the paired family bootstrap (V0.15 G2,
D351). Pure numpy.

Per set of held-out rows (one family, or all of them pooled):

* **MALE** = mean ``|y_hat - y|`` (log error; primary - matches the
  residual target and is roughly symmetric MAPE);
* **MAPE** = mean ``|raw x e^y_hat - actual| / actual`` on seconds, and the
  median APE;
* **coverage** of the conformal interval ``y_hat +- q`` and its median full
  width ``2 q``;
* **OOD rate**; **usable rate** = in distribution **and** ``2 q <=
  max_interval_log`` **and** a learned model (a baseline is never "ML").

**Paired family bootstrap** (:func:`paired_verdict`): per held-out family
``d_f`` = MALE(model) - MALE(calibration) on that family's rows; the
percentile bootstrap CI of the mean of the ``d_f`` (families resampled,
:func:`meshforge.bench.stats.bootstrap_mean_ci` - the planner evaluator's
helper, seeded). D357: ``better`` iff at least 5 families **and** the CI's
upper end < ``-ml_min_effect_log`` (0.02), ``worse`` iff its lower end > 0,
else ``not_separable`` (fewer than 5 families: reason ``too_few_families``);
:func:`~.compare.compare_term` adds the learned-model conditions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.ml.train.data import FloatArray

__all__ = [
    "MIN_EFFECT_LOG",
    "MIN_VERDICT_FAMILIES",
    "HeldOut",
    "male",
    "paired_verdict",
    "summary",
]

MIN_EFFECT_LOG: Final = 0.02
"""D357 ``ml_min_effect_log``: ``better`` needs the CI's upper end below
minus this (about 2 % in log error)."""
MIN_VERDICT_FAMILIES: Final = 5
"""D357: fewer held-out families -> ``not_separable`` (``too_few_families``)."""


@dataclass(frozen=True, slots=True)
class HeldOut:
    """The held-out predictions of one term (all outer folds together)."""

    family: tuple[str, ...]
    y: FloatArray
    raw_s: FloatArray
    actual_s: FloatArray
    model: FloatArray
    """The selected configuration's ``y_hat`` of the row's fold."""
    calibration: FloatArray
    q: FloatArray
    """The fold's conformal half-width per row."""
    ood: np.ndarray
    learned: np.ndarray
    """The fold's selected configuration is a learned model."""

    def take(self, mask: np.ndarray) -> HeldOut:
        idx = np.flatnonzero(mask)
        return HeldOut(
            family=tuple(self.family[i] for i in idx),
            y=self.y[idx],
            raw_s=self.raw_s[idx],
            actual_s=self.actual_s[idx],
            model=self.model[idx],
            calibration=self.calibration[idx],
            q=self.q[idx],
            ood=self.ood[idx],
            learned=self.learned[idx],
        )


def male(y: FloatArray, y_hat: FloatArray) -> float:
    return float(np.mean(np.abs(y_hat - y))) if y.size else math.nan


def _ape(held: HeldOut, y_hat: FloatArray) -> FloatArray:
    # a model extrapolating far out of distribution can overflow exp(): the
    # value becomes inf (written as null) - silently, never a RuntimeWarning
    with np.errstate(over="ignore", invalid="ignore"):
        predicted = held.raw_s * np.exp(y_hat)
        return np.asarray(
            np.abs(predicted - held.actual_s) / held.actual_s, dtype=np.float64
        )


def summary(held: HeldOut, *, max_interval_log: float) -> dict[str, Any]:
    """The D351 metrics of ``held`` (module docstring)."""
    n = int(held.y.size)
    if n == 0:
        return {"rows": 0}
    width = 2.0 * held.q
    inside = np.abs(held.y - held.model) <= held.q
    usable = held.learned & ~held.ood & (width <= max_interval_log)
    ape_model = _ape(held, held.model)
    return {
        "rows": n,
        "male_model": male(held.y, held.model),
        "male_calibration": male(held.y, held.calibration),
        "male_raw": male(held.y, np.zeros_like(held.y)),
        "male_effective": male(held.y, np.where(usable, held.model, held.calibration)),
        "mape_model": float(np.mean(ape_model)),
        "mape_calibration": float(np.mean(_ape(held, held.calibration))),
        "median_ape_model": float(np.median(ape_model)),
        "coverage": float(np.mean(inside)),
        "median_width": float(np.median(width)),
        "ood_rate": float(np.mean(held.ood)),
        "usable_rate": float(np.mean(usable)),
    }


def paired_verdict(
    deltas: Sequence[float],
    *,
    seed: int,
    resamples: int,
    min_effect: float = MIN_EFFECT_LOG,
    min_families: int = MIN_VERDICT_FAMILIES,
    level: float = 0.95,
) -> dict[str, Any]:
    """The paired family bootstrap of ``deltas`` (one per family, module
    docstring; D357): ``better`` iff at least ``min_families`` families and
    the CI's upper end is below ``-min_effect``; ``worse`` iff (with enough
    families) its lower end is above 0; else ``not_separable``. ``reason``
    says why. The learned-model conditions are the caller's
    (:func:`~.compare.compare_term`)."""
    values = [d for d in deltas if math.isfinite(d)]
    if not values:
        return {
            "families": 0,
            "mean": None,
            "low": None,
            "high": None,
            "level": level,
            "min_effect": min_effect,
            "verdict": "not_separable",
            "reason": "too_few_families",
        }
    ci = bootstrap_mean_ci(values, seed=seed, resamples=resamples, level=level)
    if len(values) < min_families:
        verdict, reason = "not_separable", "too_few_families"
    elif ci.high < -min_effect:
        verdict, reason = "better", "ci_below_min_effect"
    elif ci.low > 0.0:
        verdict, reason = "worse", "ci_above_zero"
    else:
        verdict, reason = "not_separable", "ci_not_beyond_min_effect"
    return {
        "families": len(values),
        "mean": ci.mean,
        "low": ci.low,
        "high": ci.high,
        "level": level,
        "min_effect": min_effect,
        "verdict": verdict,
        "reason": reason,
    }
