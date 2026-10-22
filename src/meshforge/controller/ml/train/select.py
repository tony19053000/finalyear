"""Model selection: inner grouped k-fold + the one-standard-error rule
(V0.15 G2, D347, D337).

:func:`select` runs on the rows it is given - in the nested loop, an outer
fold's **training** rows only - and never sees anything else:

1. ``k`` folds of whole fingerprints (:func:`~meshforge.controller.ml.
   splits.group_kfold`, seeded; ``k`` = min(``inner_k``, groups));
2. every configuration fitted on each fold's training side, scored on its
   test side by MALE = mean ``|y_hat - y|``;
3. per configuration the mean of its fold scores and their standard error
   (sample standard deviation / sqrt(k));
4. **one-standard-error rule**: the best mean ``m*`` with standard error
   ``se*``; the selected configuration is the **simplest**
   (:attr:`~.algorithms.Config.rank`) whose mean is at most ``m* + se*``.
   A baseline (``raw`` / ``calibration``) can win - "no learned model" is a
   valid outcome.

The selected configuration's **out-of-fold residuals** ``|y - y_hat|`` over
all the rows are returned for the conformal interval (:mod:`.conformal`):
each comes from a model that never saw that row's fingerprint.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from meshforge.controller.ml.splits import group_kfold
from meshforge.controller.ml.train.algorithms import Config, FitContext, fit_many
from meshforge.controller.ml.train.data import FloatArray, IntArray

__all__ = ["ConfigScore", "Selection", "one_se_pick", "select"]


@dataclass(frozen=True, slots=True)
class ConfigScore:
    config: Config
    mean: float
    se: float
    folds: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Selection:
    scores: tuple[ConfigScore, ...]
    """Every configuration, in the input order."""
    best: Config
    """The lowest mean MALE (ties: the simplest)."""
    selected: Config
    """The one-standard-error pick."""
    k: int
    oof_residuals: FloatArray
    """``|y - y_hat|`` of :attr:`selected` per row, out of fold."""
    rows: IntArray


def one_se_pick(scores: Sequence[ConfigScore]) -> tuple[Config, Config]:
    """``(best, selected)`` by the one-standard-error rule (module docstring)."""
    best = min(scores, key=lambda s: (s.mean, s.config.rank))
    limit = best.mean + (best.se if math.isfinite(best.se) else 0.0)
    eligible = [s for s in scores if s.mean <= limit]
    selected = min(eligible, key=lambda s: s.config.rank)
    return best.config, selected.config


def select(
    ctx: FitContext,
    rows: IntArray,
    configs: Sequence[Config],
    *,
    inner_k: int,
    seed: int,
) -> Selection:
    """See module docstring. ``ValueError`` when the rows hold fewer than two
    groups (nothing to cross-validate)."""
    table = ctx.table
    local = table.split_rows(rows)
    groups = len({row["group"] for row in local})
    k = min(inner_k, groups)
    folds = group_kfold(local, k, seed)  # ValueError for < 2 groups
    oof = np.full((len(configs), len(rows)), np.nan, dtype=np.float64)
    fold_scores: list[list[float]] = [[] for _ in configs]
    for fold in folds:
        train = rows[list(fold.train)]
        test_local = np.asarray(fold.test, dtype=np.intp)
        test = rows[test_local]
        ctx.observe("inner_train", train)
        ctx.observe("inner_test", test)
        models = fit_many(configs, ctx, train)
        X = table.X[test]
        y = table.y[test]
        for c, config in enumerate(configs):
            predicted = models[config].predict(X, test)
            oof[c, test_local] = predicted
            fold_scores[c].append(float(np.mean(np.abs(predicted - y))))
    scores = []
    for c, config in enumerate(configs):
        values = fold_scores[c]
        mean = math.fsum(values) / len(values)
        se = (
            float(np.std(values, ddof=1)) / math.sqrt(len(values))
            if len(values) > 1
            else math.inf
        )
        scores.append(ConfigScore(config, mean, se, tuple(values)))
    best, selected = one_se_pick(scores)
    chosen = configs.index(selected)
    return Selection(
        scores=tuple(scores),
        best=best,
        selected=selected,
        k=k,
        oof_residuals=np.abs(table.y[rows] - oof[chosen]),
        rows=rows,
    )
