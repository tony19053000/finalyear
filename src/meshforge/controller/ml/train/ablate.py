"""Feature-group ablations (V0.15 G5a, D372) - "which inputs matter", with
numbers for the report.

For each compared term and each feature group (:data:`ABLATION_GROUPS`:
hardware scores, live load, cache, links, job size, analytical raw + source,
family one-hot) the **whole nested evaluation** of :mod:`.compare` is run
again without that group, and its held-out error is compared with the full
run's, per held-out family:

* a numeric group is dropped by making its columns **missing** (NaN) in every
  row - the models' own missing-value handling (median imputation + missing
  indicator, or LightGBM / XGBoost's native NaN) then carries no information
  from it, and the feature list, the OOD test and the artefact format stay as
  they are;
* the family group exists only when the full run used it (``--with-family``);
  otherwise its row says ``not_in_full_model``.

Per family ``d_f = MALE(without group) - MALE(full)`` (log error; > 0 = the
group helped); the seeded percentile bootstrap CI of the mean ``d_f``
(families resampled, :func:`meshforge.bench.stats.bootstrap_mean_ci`, the
G2 paired family bootstrap) -> ``matters`` (CI low > 0), ``not_needed`` (CI
high < 0: dropping it helped), else ``not_separable``; fewer than
``min_verdict_families`` families -> ``not_separable`` (``too_few_families``).
The pooled MALE difference is reported next to it.

Cost: one nested evaluation per (term, group) - about 7x a comparison; the
regret of each ablation is deliberately not computed (D372). Deterministic
(D352): the same seeds, one thread, no importance or quantile models in the
re-runs (they do not change MALE).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Final

import numpy as np

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.ml.schema import FEATURES, FeatureGroup
from meshforge.controller.ml.train.algorithms import Observer
from meshforge.controller.ml.train.compare import (
    CompareSettings,
    TermResult,
    compare_term,
)
from meshforge.controller.ml.train.data import Table, feature_indices

__all__ = ["ABLATION_GROUPS", "ablate", "ablate_term", "without_group"]

ABLATION_GROUPS: Final = (
    FeatureGroup.HARDWARE,
    FeatureGroup.LOAD,
    FeatureGroup.CACHE,
    FeatureGroup.NETWORK,
    FeatureGroup.JOB_SIZE,
    FeatureGroup.ANALYTICAL,
    FeatureGroup.FAMILY,
)


def without_group(table: Table, group: FeatureGroup) -> Table:
    """``table`` with every column of ``group`` missing (NaN)."""
    X = table.X.copy()
    X[:, list(feature_indices(group))] = np.nan
    return replace(table, X=X)


def _features(group: FeatureGroup) -> list[str]:
    return [f.name for f in FEATURES if f.group == group]


def _row(
    group: FeatureGroup,
    status: str,
    *,
    reason: str | None = None,
    **numbers: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "group": group.value,
        "features": _features(group),
        "status": status,
        "reason": reason,
        "families": 0,
        "delta_male_pooled": None,
        "mean": None,
        "low": None,
        "high": None,
        "verdict": None,
        "per_family": {},
    }
    base.update(numbers)
    return base


def _verdict(
    deltas: Sequence[float], settings: CompareSettings
) -> tuple[str, str, float | None, float | None, float | None]:
    values = [d for d in deltas if math.isfinite(d)]
    if not values:
        return "not_separable", "too_few_families", None, None, None
    ci = bootstrap_mean_ci(
        values, seed=settings.bootstrap_seed, resamples=settings.bootstrap_resamples
    )
    if len(values) < settings.min_verdict_families:
        return "not_separable", "too_few_families", ci.mean, ci.low, ci.high
    if ci.low > 0.0:
        return "matters", "ci_above_zero", ci.mean, ci.low, ci.high
    if ci.high < 0.0:
        return "not_needed", "ci_below_zero", ci.mean, ci.low, ci.high
    return "not_separable", "ci_contains_zero", ci.mean, ci.low, ci.high


def ablate_term(
    table: Table,
    full: TermResult,
    settings: CompareSettings,
    observer: Observer | None = None,
) -> list[dict[str, Any]]:
    """Every group of :data:`ABLATION_GROUPS` for one term (module
    docstring), in that order."""
    rerun = replace(settings, importance_repeats=0, quantile_alternative=False)
    out: list[dict[str, Any]] = []
    for group in ABLATION_GROUPS:
        if full.status == "insufficient_data":
            out.append(_row(group, "skipped", reason="insufficient_data"))
            continue
        if group == FeatureGroup.FAMILY:
            if not settings.with_family:
                out.append(
                    _row(
                        group,
                        "skipped",
                        reason="not_in_full_model (the full run has no family "
                        "feature; --with-family adds it)",
                    )
                )
                continue
            result = compare_term(
                table, full.term, replace(rerun, with_family=False), observer
            )
        else:
            result = compare_term(
                without_group(table, group), full.term, rerun, observer
            )
        per_family: dict[str, float] = {}
        for name, part in sorted(full.per_family.items()):
            other = result.per_family.get(name)
            if other is None:
                continue
            delta = float(other["male_model"]) - float(part["male_model"])
            if math.isfinite(delta):
                per_family[name] = delta
        verdict, reason, mean, low, high = _verdict(list(per_family.values()), settings)
        pooled = float(result.pooled.get("male_model", math.nan)) - float(
            full.pooled.get("male_model", math.nan)
        )
        out.append(
            _row(
                group,
                "evaluated",
                reason=reason,
                families=len(per_family),
                delta_male_pooled=pooled if math.isfinite(pooled) else None,
                mean=mean,
                low=low,
                high=high,
                verdict=verdict,
                per_family=per_family,
            )
        )
    return out


def ablate(
    table: Table,
    results: Sequence[TermResult],
    settings: CompareSettings,
    observer: Observer | None = None,
) -> dict[str, Any]:
    """The report's ``ablations`` block: per compared term, every group."""
    return {
        "groups": [g.value for g in ABLATION_GROUPS],
        "how": "the group's columns set missing (family: the one-hot dropped); "
        "the nested evaluation re-run; delta = MALE(without) - MALE(full) per "
        "held-out family, paired bootstrap of the mean",
        "terms": {r.term: ablate_term(table, r, settings, observer) for r in results},
    }
