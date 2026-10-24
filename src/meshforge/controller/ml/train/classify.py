"""The 0.15.10 classifier experiment - report only (V0.15 G6a, D343, D375,
D376).

"Why not just classify?" A direct classifier maps a job and the cluster it
meets to the winning **configuration class** (``local``, ``mode1``,
``mode3_k``). This module trains such classifiers on the controller's
examples (:mod:`meshforge.controller.planner_eval.classifier_examples`: one
per labelled benchmark group) and scores them with **the evaluator's regret
metric**, next to the planner's cost regression on the same groups:

1. **Floor** (D376): fewer than ``min_groups`` (10) examples or fewer than
   ``min_families`` (3) families -> ``too_few_groups`` with the counts; no
   model is trained and **no accuracy is printed**.
2. **Folds** - leave-one-family-out (the D347 outer loop): the examples of
   family ``F`` are predicted by models trained only on the other families,
   and also without any example sharing a workload fingerprint with ``F``'s
   (the D316 / D368 rule). Each fold records what it trained on.
3. **Models** - ``majority`` (always the training folds' most common class;
   ties -> the alphabetically first), ``logistic`` (multinomial logistic
   regression on the shared imputed + standardised features, D349) and
   ``lightgbm`` (a small multiclass booster on the raw features, NaN
   native). Fixed seeds, one thread (D352). A training fold with one class
   predicts it; so does a document with no features at all (zero width):
   ``logistic`` and ``lightgbm`` then fall back to the training folds'
   majority, like ``majority``. A non-finite feature (``Infinity`` /
   ``NaN`` literals) reads as missing (NaN), as the controller's builder
   writes it; so does one too large to standardise (``|x| > 1e150``, its
   square would overflow the shared D349 prep) - no real feature is near.
4. **Regret** - a predicted class scores the regret of **the planner's
   best-ranked candidate of that class** for the group (precomputed by the
   controller), never the label's own configuration; a class the planner did
   not enumerate, or whose candidate maps to no measured configuration, is
   ``unmeasured`` - counted and reported, never as 0 (the evaluator's D297
   rule). So is an outcome marked counted whose regret is not a finite
   number.
5. **Comparison** - per model: counted / unmeasured groups, mean regret, the
   share within ``regret_threshold_pct``, accuracy (next to regret, never
   instead of it), per family, and the **paired** difference to the cost
   regression on the groups counted for both with a seeded bootstrap CI
   (``classifier_better`` / ``cost_model_better`` / ``not_separable`` /
   ``too_few_groups``).

Deterministic: the same examples document and library versions give the
same section, byte for byte (canonical JSON through :func:`.report.clean`).
A classifier is never registered and never wired into ``plan()`` (an AST
test pins it).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import LogisticRegression
from threadpoolctl import threadpool_limits

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.ml.prep import fit_prep, transform
from meshforge.controller.ml.train.data import FloatArray
from meshforge.controller.ml.train.report import classifier_lines, clean

__all__ = [
    "CLASSIFY_VERSION",
    "EXAMPLES_VERSIONS",
    "MODELS",
    "ClassifySettings",
    "Example",
    "ExamplesError",
    "classifier_lines",
    "classify",
    "outcome_of",
    "parse_examples",
]

CLASSIFY_VERSION: Final = 1
EXAMPLES_VERSIONS: Final = frozenset({1})
"""Examples documents this code reads."""
MODELS: Final = ("majority", "logistic", "lightgbm")
COST_MODEL: Final = "cost_model"
_ROUND: Final = 6
_MAX_FEATURE_ABS: Final = 1e150
"""Larger feature magnitudes read as missing (module docstring, item 3)."""


class ExamplesError(ValueError):
    """An examples document this code cannot read."""


@dataclass(frozen=True, slots=True)
class ClassifySettings:
    min_groups: int = 10
    """D376: fewer examples -> ``too_few_groups``."""
    min_families: int = 3
    regret_threshold_pct: float = 15.0
    """The evaluator's criterion-1 threshold (shown as "within")."""
    seed: int = 0
    bootstrap_resamples: int = 2000
    bootstrap_seed: int = 20_261_022
    ci_level: float = 0.95
    models: tuple[str, ...] = MODELS
    lightgbm_rounds: int = 60
    lightgbm_leaves: int = 7


@dataclass(frozen=True, slots=True)
class Example:
    campaign_id: str
    group_id: str
    family: str
    fingerprint: str | None
    label: str
    features: tuple[float, ...]
    classes: Mapping[str, Mapping[str, Any]]
    cost_model: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (self.campaign_id, self.group_id)


# ------------------------------------------------------------ input


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExamplesError(f"{what} must be a non-empty string")
    return value


def _number(value: Any) -> float:
    if value is None:
        return math.nan
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ExamplesError("a feature must be a number or null")
    number = float(value)
    if not math.isfinite(number) or abs(number) > _MAX_FEATURE_ABS:
        return math.nan
    return number


def parse_examples(doc: Any) -> tuple[list[str], list[Example]]:
    """``(feature names, examples)`` of an examples document (sorted by
    group); :class:`ExamplesError` for anything malformed."""
    if not isinstance(doc, Mapping):
        raise ExamplesError("the examples document must be a JSON object")
    version = doc.get("examples_version")
    if version not in EXAMPLES_VERSIONS:
        raise ExamplesError(f"unknown examples_version {version!r}")
    names = doc.get("feature_names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ExamplesError("feature_names must be a list of strings")
    rows = doc.get("examples")
    if not isinstance(rows, list):
        raise ExamplesError("examples must be a list")
    out: list[Example] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ExamplesError("an example must be an object")
        values = row.get("features")
        if not isinstance(values, list) or len(values) != len(names):
            raise ExamplesError("an example's features do not match feature_names")
        classes = row.get("classes")
        cost = row.get("cost_model")
        if not isinstance(classes, Mapping) or not isinstance(cost, Mapping):
            raise ExamplesError("an example needs classes and cost_model objects")
        fingerprint = row.get("fingerprint")
        out.append(
            Example(
                campaign_id=_text(row.get("campaign_id"), "campaign_id"),
                group_id=_text(row.get("group_id"), "group_id"),
                family=_text(row.get("family"), "family"),
                fingerprint=fingerprint if isinstance(fingerprint, str) else None,
                label=_text(row.get("label"), "label"),
                features=tuple(_number(v) for v in values),
                classes={
                    str(k): dict(v)
                    for k, v in classes.items()
                    if isinstance(v, Mapping)
                },
                cost_model=dict(cost),
            )
        )
    keys = [e.key for e in out]
    if len(set(keys)) != len(keys):
        raise ExamplesError("a group is listed twice")
    return list(names), sorted(out, key=lambda e: e.key)


# ------------------------------------------------------------ models


def _majority(labels: Sequence[str]) -> str:
    counts = Counter(labels)
    top = max(counts.values())
    return sorted(k for k, n in counts.items() if n == top)[0]


def _matrix(examples: Sequence[Example], width: int) -> FloatArray:
    if not examples:
        return np.zeros((0, width), dtype=np.float64)
    return np.asarray([e.features for e in examples], dtype=np.float64)


def _fit_predict(
    model: str,
    train: Sequence[Example],
    test: Sequence[Example],
    width: int,
    settings: ClassifySettings,
) -> list[str]:
    """``model`` trained on ``train``, its class for every ``test`` example."""
    labels = [e.label for e in train]
    majority = _majority(labels)
    classes = sorted(set(labels))
    if model == "majority" or len(classes) == 1 or width == 0:
        return [majority] * len(test)
    X_train, X_test = _matrix(train, width), _matrix(test, width)
    index = {name: i for i, name in enumerate(classes)}
    y = np.asarray([index[label] for label in labels], dtype=np.int64)
    if model == "logistic":
        prep = fit_prep(X_train)
        clf = LogisticRegression(max_iter=2000, C=1.0, random_state=settings.seed)
        clf.fit(transform(prep, X_train, standardise=True), y)
        found = clf.predict(transform(prep, X_test, standardise=True))
        return [classes[int(i)] for i in found]
    if model == "lightgbm":
        params = {
            "objective": "multiclass",
            "num_class": len(classes),
            "num_leaves": settings.lightgbm_leaves,
            "learning_rate": 0.1,
            "min_child_samples": 2,
            "min_data_in_bin": 1,
            "deterministic": True,
            "force_row_wise": True,
            "num_threads": 1,
            "seed": settings.seed,
            "bagging_fraction": 1.0,
            "feature_fraction": 1.0,
            "verbose": -1,
        }
        data = lgb.Dataset(X_train, label=y, params={"verbose": -1})
        booster = lgb.train(params, data, num_boost_round=settings.lightgbm_rounds)
        proba = np.asarray(booster.predict(X_test), dtype=np.float64)
        if proba.ndim == 1:
            proba = proba.reshape(len(test), len(classes))
        return [classes[int(np.argmax(p))] for p in proba]
    raise ValueError(f"unknown classifier model {model!r}")


# ------------------------------------------------------------ scoring


def outcome_of(example: Example, predicted: str) -> dict[str, Any]:
    """What predicting ``predicted`` for ``example`` scores: the planner's
    best candidate of that class (module docstring, item 4)."""
    found = example.classes.get(predicted)
    if found is None:
        return {
            "class": predicted,
            "status": "not_enumerated",
            "config_id": None,
            "regret_pct": None,
        }
    regret = _counted_regret(found)
    return {
        "class": predicted,
        "status": "unmeasured" if regret is None else "counted",
        "config_id": found.get("config_id"),
        "regret_pct": regret,
    }


def _counted_regret(outcome: Mapping[str, Any]) -> float | None:
    """The outcome's regret when it is counted, else None."""
    regret = outcome.get("regret_pct")
    if outcome.get("status") != "counted" or isinstance(regret, bool):
        return None
    if not isinstance(regret, int | float):
        return None
    number = float(regret)
    return number if math.isfinite(number) else None


def _cost_outcome(example: Example) -> dict[str, Any]:
    regret = _counted_regret(example.cost_model)
    return {
        "status": "unmeasured" if regret is None else "counted",
        "config_id": example.cost_model.get("config_id"),
        "regret_pct": regret,
    }


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, _ROUND)


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _summary(
    outcomes: Mapping[tuple[str, str], Mapping[str, Any]],
    settings: ClassifySettings,
) -> dict[str, Any]:
    regrets = [
        float(o["regret_pct"]) for o in outcomes.values() if o["status"] == "counted"
    ]
    return {
        "counted_groups": len(regrets),
        "unmeasured_groups": sum(
            1 for o in outcomes.values() if o["status"] != "counted"
        ),
        "mean_regret_pct": _r(_mean(regrets)),
        "within_threshold_pct": _r(
            sum(1 for v in regrets if v <= settings.regret_threshold_pct)
            / len(regrets)
            * 100.0
        )
        if regrets
        else None,
    }


def _paired(
    mine: Mapping[tuple[str, str], Mapping[str, Any]],
    cost: Mapping[tuple[str, str], Mapping[str, Any]],
    settings: ClassifySettings,
) -> dict[str, Any]:
    """Paired regret difference (model - cost model) on groups counted for
    both."""
    common = sorted(
        k
        for k in mine.keys() & cost.keys()
        if mine[k]["status"] == "counted" and cost[k]["status"] == "counted"
    )
    diffs = [
        round(float(mine[k]["regret_pct"]) - float(cost[k]["regret_pct"]), _ROUND)
        for k in common
    ]
    out: dict[str, Any] = {
        "common_groups": len(common),
        "mean_diff_pct": _r(_mean(diffs)),
        "ci_low": None,
        "ci_high": None,
    }
    if len(common) < settings.min_groups:
        out["verdict"] = "too_few_groups"
        return out
    ci = bootstrap_mean_ci(
        diffs,
        seed=settings.bootstrap_seed,
        resamples=settings.bootstrap_resamples,
        level=settings.ci_level,
    )
    low, high = round(ci.low, _ROUND), round(ci.high, _ROUND)
    out.update(ci_low=low, ci_high=high)
    if high < 0.0:
        out["verdict"] = "classifier_better"
    elif low > 0.0:
        out["verdict"] = "cost_model_better"
    else:
        out["verdict"] = "not_separable"
    return out


# ------------------------------------------------------------ the experiment


def _clean_dict(doc: Mapping[str, Any]) -> dict[str, Any]:
    out = clean(doc)
    assert isinstance(out, dict)  # noqa: S101 - a mapping cleans to a dict
    return out


def _settings_json(settings: ClassifySettings) -> dict[str, Any]:
    return {
        "min_groups": settings.min_groups,
        "min_families": settings.min_families,
        "regret_threshold_pct": settings.regret_threshold_pct,
        "seed": settings.seed,
        "bootstrap_resamples": settings.bootstrap_resamples,
        "bootstrap_seed": settings.bootstrap_seed,
        "ci_level": settings.ci_level,
        "models": list(settings.models),
        "lightgbm": {
            "rounds": settings.lightgbm_rounds,
            "num_leaves": settings.lightgbm_leaves,
        },
    }


def classify(
    doc: Mapping[str, Any], settings: ClassifySettings | None = None
) -> dict[str, Any]:
    """The "Classifier vs cost regression" section (module docstring),
    canonical-ready. :class:`ExamplesError` for an unreadable document."""
    settings = settings or ClassifySettings()
    names, examples = parse_examples(doc)
    families = sorted({e.family for e in examples})
    base: dict[str, Any] = {
        "classify_version": CLASSIFY_VERSION,
        "examples_version": doc.get("examples_version"),
        "campaign_ids": list(doc.get("campaign_ids") or []),
        "policy": doc.get("policy"),
        "settings": _settings_json(settings),
        "groups": len(examples),
        "families": families,
        "feature_count": len(names),
        "label_counts": dict(sorted(Counter(e.label for e in examples).items())),
        "input_counts": dict(doc.get("counts") or {}),
    }
    if len(examples) < settings.min_groups or len(families) < settings.min_families:
        return _clean_dict(
            base
            | {
                "status": "too_few_groups",
                "reason": (
                    f"{len(examples)} labelled group(s) in {len(families)} "
                    f"family(ies); at least {settings.min_groups} groups in "
                    f"{settings.min_families} families needed - no classifier is "
                    "trained or scored on fewer"
                ),
            }
        )
    width = len(names)
    predictions: dict[str, dict[tuple[str, str], str]] = {
        m: {} for m in settings.models
    }
    folds: list[dict[str, Any]] = []
    with threadpool_limits(limits=1):
        for family in families:
            test = [e for e in examples if e.family == family]
            prints = {e.fingerprint for e in test if e.fingerprint is not None}
            train = [
                e
                for e in examples
                if e.family != family
                and (e.fingerprint is None or e.fingerprint not in prints)
            ]
            dropped = sum(
                1 for e in examples if e.family != family and e.fingerprint in prints
            )
            folds.append(
                {
                    "family": family,
                    "test_groups": len(test),
                    "train_groups": len(train),
                    "train_families": sorted({e.family for e in train}),
                    "excluded_same_workload": dropped,
                    "train_classes": sorted({e.label for e in train}),
                }
            )
            if not train:
                continue
            for model in settings.models:
                found = _fit_predict(model, train, test, width, settings)
                for example, name in zip(test, found, strict=True):
                    predictions[model][example.key] = name
    by_key = {e.key: e for e in examples}
    cost = {e.key: _cost_outcome(e) for e in examples}
    rows: list[dict[str, Any]] = []
    for model in settings.models:
        picked = predictions[model]
        outcomes = {k: outcome_of(by_key[k], cls) for k, cls in sorted(picked.items())}
        correct = sum(1 for k, cls in picked.items() if cls == by_key[k].label)
        per_family = []
        for family in families:
            mine = {k: o for k, o in outcomes.items() if by_key[k].family == family}
            per_family.append({"family": family} | _summary(mine, settings))
        rows.append(
            {
                "model": model,
                "predicted_groups": len(picked),
                "accuracy": _r(correct / len(picked)) if picked else None,
                **_summary(outcomes, settings),
                "vs_cost_model": _paired(outcomes, cost, settings),
                "per_family": per_family,
                "predicted_classes": dict(sorted(Counter(picked.values()).items())),
            }
        )
    cost_row = {
        "model": COST_MODEL,
        "predicted_groups": len(cost),
        **_summary(cost, settings),
        "per_family": [
            {"family": family}
            | _summary(
                {k: o for k, o in cost.items() if by_key[k].family == family},
                settings,
            )
            for family in families
        ],
    }
    return _clean_dict(
        base
        | {
            "status": "evaluated",
            "reason": None,
            "folds": folds,
            "models": rows,
            "cost_model": cost_row,
            "groups_detail": [
                {
                    "campaign_id": e.campaign_id,
                    "group_id": e.group_id,
                    "family": e.family,
                    "label": e.label,
                    "cost_model": cost[e.key],
                    "predicted": {
                        m: predictions[m].get(e.key) for m in settings.models
                    },
                }
                for e in examples
            ],
        }
    )
