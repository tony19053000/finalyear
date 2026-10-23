"""The nested comparison per term (V0.15 G2, D347, D348, D351).

For each term (one model per term; the term is never a feature of a shared
model, D348):

* **Floor** - fewer than ``min_rows`` (200) rows or ``min_families`` (3)
  families -> ``insufficient_data`` (the planner keeps calibration).
* **Outer loop = leave one family out** (:func:`~meshforge.controller.ml.
  splits.leave_one_family_out` on the term's rows): it only **reports**.
  Per held-out family, on the other families' rows only:

  - :func:`~.select.select` (inner grouped k-fold + one-standard-error rule)
    picks the configuration and gives its out-of-fold residuals;
  - :func:`~.conformal.conformal_q` of those residuals -> the interval;
  - the selected configuration refitted on all the outer training rows, the
    calibration baseline refitted on them (D350), the OOD test fitted on
    them (:mod:`.ood`), and LightGBM quantile models (0.05 / 0.95) as the
    compared interval alternative;
  - all of them predict the held-out family's rows.

  Nothing fitted in a fold ever sees the held-out family (an observer hook,
  :data:`~.algorithms.Observer`, lets the leakage test check every part).
* **Metrics** (:mod:`.metrics`) per family and pooled, the **paired family
  bootstrap** of MALE(model) - MALE(calibration) (``better`` /
  ``not_separable`` / ``worse``), and **would-reject** flags: pooled
  coverage < 80 %, or a family whose MALE is worse than calibration's by
  more than ``family_tolerance_log`` (0.05), or (D356) a family with >= 20
  held-out rows covered < 70 %. **D357**: ``better`` only with >= 5 families,
  the CI's upper end below ``-min_effect_log`` (0.02), and a learned model
  selected in >= half of the outer folds and shipped; no learned model
  shipped -> ``no_learned_model``; the verdict carries its ``reason``.
* **Permutation importance** of each fold's learned model on its held-out
  rows (V0.15 G5a, D372: ``importance_repeats`` = 5 seeded permutations per
  feature, their mean MALE increase; weighted by the fold's rows; folds that
  picked a baseline add 0, and the report says from how many folds the
  numbers came) - the top 10. ``importance_repeats`` 0 skips it (the
  feature-group ablations, :mod:`.ablate`, re-run this evaluation and do not
  need it).
* **The shipped candidate** (G3 stores it): the selection re-run on **all**
  the term's rows, its conformal ``q`` from those all-row out-of-fold
  residuals. ``trained`` when it is a learned model, else
  ``no_learned_model``.

Deterministic (D352): fixed seeds, one thread (BLAS capped with
``threadpoolctl``), rows in the canonical order.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from threadpoolctl import threadpool_limits

from meshforge.controller.ml.schema import NUMERIC_FEATURES, TERM_NAMES
from meshforge.controller.ml.splits import leave_one_family_out
from meshforge.controller.ml.train.algorithms import (
    LEARNED,
    Config,
    FitContext,
    Observer,
    configs_for,
    fit_lightgbm,
    fit_one,
    lightgbm_params,
)
from meshforge.controller.ml.train.conformal import ALPHA, conformal_q
from meshforge.controller.ml.train.data import FloatArray, IntArray, Table
from meshforge.controller.ml.train.metrics import (
    MIN_EFFECT_LOG,
    MIN_VERDICT_FAMILIES,
    HeldOut,
    male,
    paired_verdict,
    summary,
)
from meshforge.controller.ml.train.ood import (
    OOD_K,
    OOD_PERCENTILE,
    RANGE_MARGIN,
    fit_ood,
)
from meshforge.controller.ml.train.prep import native
from meshforge.controller.ml.train.select import Selection, select
from meshforge.controller.planner.calibration import CalibrationConfig

__all__ = [
    "CompareSettings",
    "FoldResult",
    "TermResult",
    "compare",
    "compare_term",
]

_CALIBRATION = Config("calibration")


@dataclass(frozen=True, slots=True)
class CompareSettings:
    terms: tuple[str, ...] = TERM_NAMES
    algorithms: tuple[str, ...] = LEARNED
    with_family: bool = False
    min_rows: int = 200
    """D348 ``ml_min_rows``."""
    min_families: int = 3
    """D348 ``ml_min_families``."""
    inner_k: int = 5
    alpha: float = ALPHA
    max_interval_log: float = math.log(2.0)
    """D338 ``ml_max_interval_log``: the widest usable interval (full width)."""
    family_tolerance_log: float = 0.05
    """D351 ``ml_family_tolerance_log``."""
    coverage_reject: float = 0.80
    family_coverage_reject: float = 0.70
    """D356 (amended): a family with at least ``family_coverage_min_rows``
    held-out rows and a coverage below this also sets ``would_reject``."""
    family_coverage_min_rows: int = 20
    min_effect_log: float = MIN_EFFECT_LOG
    """D357 ``ml_min_effect_log``: ``better`` needs the CI's upper end below
    minus this."""
    min_verdict_families: int = MIN_VERDICT_FAMILIES
    """D357: fewer held-out families -> ``not_separable``."""
    bootstrap_resamples: int = 2000
    bootstrap_seed: int = 20_261_022
    seed: int = 0
    ood_margin: float = RANGE_MARGIN
    ood_k: int = OOD_K
    ood_percentile: float = OOD_PERCENTILE
    quantile_alternative: bool = True
    importance_top: int = 10
    importance_repeats: int = 5
    """V0.15 G5a (D372): seeded permutations per feature and fold (0 = no
    importance)."""
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)


@dataclass(frozen=True, slots=True)
class FoldResult:
    family: str
    train_rows: int
    test_rows: int
    selected: Config
    best: Config
    q: float
    inner_k: int | None
    """None = the training side had fewer than two groups (calibration kept)."""
    quantile_coverage: float | None
    quantile_median_width: float | None


@dataclass(frozen=True, slots=True)
class TermResult:
    term: str
    status: str
    """``trained`` | ``no_learned_model`` | ``insufficient_data``."""
    rows: int
    families: int
    groups: int
    reason: str | None = None
    final: Selection | None = None
    final_q: float | None = None
    folds: tuple[FoldResult, ...] = ()
    per_family: dict[str, dict[str, Any]] = field(default_factory=dict)
    pooled: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    would_reject: dict[str, Any] = field(default_factory=dict)
    quantile: dict[str, Any] | None = None
    importance: tuple[tuple[str, float], ...] = ()
    importance_folds: int = 0
    """V0.15 G3 (G2 tech debt): the outer folds whose learned model the
    importance came from (a fold that picked a baseline adds 0)."""
    importance_repeats: int = 0
    """V0.15 G5a (D372): permutations per feature and fold behind it."""


def _quantile(
    ctx: FitContext, train: IntArray, test: IntArray
) -> tuple[FloatArray, FloatArray]:
    """LightGBM 0.05 / 0.95 quantile predictions for ``test`` (D338's
    compared alternative; one fixed configuration)."""
    ctx.observe("quantile", train)
    bounds: list[FloatArray] = []
    for level in (0.05, 0.95):
        params = lightgbm_params(15, ctx.seed, objective="quantile", alpha=level)
        prep, booster = fit_lightgbm(ctx, train, params, 200)
        families = ctx.families(test) if ctx.with_family else None
        bounds.append(
            np.asarray(
                booster.predict(native(prep, ctx.table.X[test], families)),
                dtype=np.float64,
            )
        )
    return np.minimum(bounds[0], bounds[1]), np.maximum(bounds[0], bounds[1])


def _importance(
    model: Any,
    table: Table,
    test: IntArray,
    seed: int,
    fold_index: int,
    repeats: int,
) -> FloatArray:
    """Mean MALE increase per feature when that column is permuted
    (``repeats`` seeded permutations each) on the held-out rows."""
    X = table.X[test]
    y = table.y[test]
    base = male(y, model.predict(X, test))
    out = np.zeros(len(NUMERIC_FEATURES), dtype=np.float64)
    for j in range(len(NUMERIC_FEATURES)):
        total = 0.0
        for repeat in range(repeats):
            rng = np.random.default_rng([seed, fold_index, j, repeat])
            permuted = X.copy()
            permuted[:, j] = rng.permutation(permuted[:, j])
            total += male(y, model.predict(permuted, test)) - base
        out[j] = total / repeats
    return out


@dataclass(slots=True)
class _Collected:
    family: list[str] = field(default_factory=list)
    rows: list[IntArray] = field(default_factory=list)
    model: list[FloatArray] = field(default_factory=list)
    calibration: list[FloatArray] = field(default_factory=list)
    q: list[FloatArray] = field(default_factory=list)
    ood: list[np.ndarray] = field(default_factory=list)
    reasons: list[str | None] = field(default_factory=list)
    learned: list[np.ndarray] = field(default_factory=list)
    q_low: list[FloatArray] = field(default_factory=list)
    q_high: list[FloatArray] = field(default_factory=list)
    q_y: list[FloatArray] = field(default_factory=list)
    """The held-out ``y`` of the folds that have quantile predictions."""


def _fold(
    ctx: FitContext,
    configs: Sequence[Config],
    settings: CompareSettings,
    train: IntArray,
    test: IntArray,
    family: str,
    fold_index: int,
    out: _Collected,
    importance: FloatArray,
) -> FoldResult:
    table = ctx.table
    ctx.observe("outer_train", train)
    try:
        sel: Selection | None = select(
            ctx, train, configs, inner_k=settings.inner_k, seed=settings.seed
        )
    except ValueError:  # fewer than two groups to cross-validate
        sel = None
    if sel is None:
        selected = best = _CALIBRATION
        q = math.inf
    else:
        selected, best = sel.selected, sel.best
        ctx.observe("conformal", sel.rows)
        q = conformal_q(sel.oof_residuals, settings.alpha)
    model = fit_one(selected, ctx, train)
    calibration = fit_one(_CALIBRATION, ctx, train)
    ctx.observe("ood", train)
    ood_model = fit_ood(
        table.X[train],
        margin=settings.ood_margin,
        k=settings.ood_k,
        percentile=settings.ood_percentile,
    )
    X = table.X[test]
    flags, reasons = ood_model.check(X)
    y_hat = model.predict(X, test)
    out.family.extend(table.family[i] for i in test)
    out.rows.append(test)
    out.model.append(y_hat)
    out.calibration.append(calibration.predict(X, test))
    out.q.append(np.full(len(test), q))
    out.ood.append(flags)
    out.reasons.extend(reasons)
    out.learned.append(np.full(len(test), selected.learned))
    q_cov: float | None = None
    q_width: float | None = None
    if (
        settings.quantile_alternative
        and "lightgbm" in settings.algorithms
        and sel is not None
    ):
        low, high = _quantile(ctx, train, test)
        out.q_low.append(low)
        out.q_high.append(high)
        y = table.y[test]
        out.q_y.append(y)
        q_cov = float(np.mean((y >= low) & (y <= high)))
        q_width = float(np.median(high - low))
    if selected.learned and settings.importance_repeats > 0:
        importance += _importance(
            model, table, test, settings.seed, fold_index, settings.importance_repeats
        ) * len(test)
    return FoldResult(
        family=family,
        train_rows=len(train),
        test_rows=len(test),
        selected=selected,
        best=best,
        q=q,
        inner_k=None if sel is None else sel.k,
        quantile_coverage=q_cov,
        quantile_median_width=q_width,
    )


def _learned_gate(
    verdict: dict[str, Any], folds: Sequence[FoldResult], final: Selection
) -> dict[str, Any]:
    """D357 (a): ``better`` only for a learned model that is the shipped
    all-rows candidate **and** was selected in at least half of the outer
    folds; no learned model shipped -> ``no_learned_model``. The bootstrap
    numbers stay in the verdict either way."""
    learned_folds = sum(1 for f in folds if f.selected.learned)
    out = {**verdict, "learned_folds": learned_folds, "folds": len(folds)}
    if not final.selected.learned:
        out.update(verdict="no_learned_model", reason="no_learned_model_ships")
    elif out["verdict"] == "better" and 2 * learned_folds < len(folds):
        out.update(verdict="not_separable", reason="learned_in_under_half_the_folds")
    return out


def compare_term(
    table: Table,
    term: str,
    settings: CompareSettings,
    observer: Observer | None = None,
) -> TermResult:
    """One term's :class:`TermResult` (module docstring), BLAS capped at one
    thread (D352)."""
    with threadpool_limits(limits=1):
        return _compare_term(table, term, settings, observer)


def _compare_term(
    table: Table,
    term: str,
    settings: CompareSettings,
    observer: Observer | None,
) -> TermResult:
    rows = table.rows_of(term)
    family_of = table.families_of(rows)
    families = sorted(set(family_of.values()))
    sizes = (len(rows), len(families), len(family_of))
    if (
        len(rows) < settings.min_rows
        or len(families) < settings.min_families
        or len(family_of) < 2  # nothing to cross-validate, whatever the floors
    ):
        return TermResult(
            term,
            "insufficient_data",
            *sizes,
            reason=f"needs >= {settings.min_rows} rows, >= "
            f"{settings.min_families} families and >= 2 workload groups",
        )
    configs = configs_for(settings.algorithms)
    ctx = FitContext(
        table=table,
        with_family=settings.with_family,
        calibration=settings.calibration,
        seed=settings.seed,
        observer=observer,
    )
    collected = _Collected()
    importance = np.zeros(len(NUMERIC_FEATURES), dtype=np.float64)
    folds: list[FoldResult] = []
    for index, fold in enumerate(leave_one_family_out(table.split_rows(rows))):
        fold_ctx = replace(ctx, fold=fold.name)
        folds.append(
            _fold(
                fold_ctx,
                configs,
                settings,
                rows[list(fold.train)],
                rows[list(fold.test)],
                fold.name,
                index,
                collected,
                importance,
            )
        )
    order = np.concatenate(collected.rows)
    held = HeldOut(
        family=tuple(collected.family),
        y=table.y[order],
        raw_s=table.raw_s[order],
        actual_s=table.actual_s[order],
        model=np.concatenate(collected.model),
        calibration=np.concatenate(collected.calibration),
        q=np.concatenate(collected.q),
        ood=np.concatenate(collected.ood),
        learned=np.concatenate(collected.learned),
    )
    per_family: dict[str, dict[str, Any]] = {}
    deltas: list[float] = []
    for name in sorted(set(held.family)):
        part = summary(
            held.take(np.asarray([f == name for f in held.family])),
            max_interval_log=settings.max_interval_log,
        )
        per_family[name] = part
        deltas.append(part["male_model"] - part["male_calibration"])
    pooled = summary(held, max_interval_log=settings.max_interval_log)
    reasons = [r.split(":")[0] if r else None for r in collected.reasons]
    pooled["ood_by_reason"] = {
        reason: sum(1 for r in reasons if r == reason)
        for reason in sorted({r for r in reasons if r})
    }
    verdict = paired_verdict(
        deltas,
        seed=settings.bootstrap_seed,
        resamples=settings.bootstrap_resamples,
        min_effect=settings.min_effect_log,
        min_families=settings.min_verdict_families,
    )
    worse = sorted(
        name
        for name, part in per_family.items()
        if part["male_model"] - part["male_calibration"] > settings.family_tolerance_log
    )
    coverage_low = pooled["coverage"] < settings.coverage_reject
    families_low = sorted(
        name
        for name, part in per_family.items()
        if part["rows"] >= settings.family_coverage_min_rows
        and part["coverage"] < settings.family_coverage_reject
    )
    quantile = None
    if collected.q_low:
        low = np.concatenate(collected.q_low)
        high = np.concatenate(collected.q_high)
        q_y = np.concatenate(collected.q_y)
        quantile = {
            "rows": len(q_y),
            "coverage": float(np.mean((q_y >= low) & (q_y <= high))),
            "median_width": float(np.median(high - low)),
        }
    weighted = importance / max(1, len(order))
    ranked = sorted(
        ((NUMERIC_FEATURES[j], float(weighted[j])) for j in range(len(weighted))),
        key=lambda item: (-item[1], item[0]),
    )
    any_learned = any(f.selected.learned for f in folds)
    final_ctx = replace(ctx, fold="final")
    final = select(
        final_ctx, rows, configs, inner_k=settings.inner_k, seed=settings.seed
    )
    final_q = conformal_q(final.oof_residuals, settings.alpha)
    verdict = _learned_gate(verdict, folds, final)
    return TermResult(
        term,
        "trained" if final.selected.learned else "no_learned_model",
        *sizes,
        final=final,
        final_q=final_q,
        folds=tuple(folds),
        per_family=per_family,
        pooled=pooled,
        verdict=verdict,
        would_reject={
            "coverage_below": coverage_low,
            "families_worse": worse,
            "families_low_coverage": families_low,
            "any": coverage_low or bool(worse) or bool(families_low),
        },
        quantile=quantile,
        importance=tuple(ranked[: settings.importance_top])
        if any_learned and settings.importance_repeats > 0
        else (),
        importance_folds=sum(1 for f in folds if f.selected.learned),
        importance_repeats=settings.importance_repeats,
    )


def compare(
    table: Table, settings: CompareSettings, observer: Observer | None = None
) -> list[TermResult]:
    """Every requested term, in :data:`~..schema.TERM_NAMES` order."""
    unknown = sorted(set(settings.terms) - set(TERM_NAMES))
    if unknown:
        raise ValueError(f"unknown term(s): {', '.join(unknown)}")
    return [
        compare_term(table, name, settings, observer)
        for name in TERM_NAMES
        if name in settings.terms
    ]
