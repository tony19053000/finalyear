"""A :class:`~meshforge.controller.planner.predictor.TermPredictor` over G2's
in-memory models (V0.15 G4, D336).

G4 plugs the planner into a model **before** G3's registry exists: this
predictor holds, per term, the D347 shipped candidate trained in this process
- the configuration :func:`~.select.select` picks on **all** the term's rows,
refitted on them, its split-conformal half-width ``q`` from the all-row
out-of-fold residuals (:func:`~.conformal.conformal_q`) and the D338 OOD test
fitted on the same rows (:func:`~.ood.fit_ood`). Exactly the statistics G2's
report names for the shipped candidate; G3 serialises the same parts
(``ml/serve.py`` replaces this class at serving time).

A term whose selection is a baseline (``raw`` / ``calibration``) or that is
under the D348 floor has **no model** here (the planner keeps calibration,
``no_model``).

``predict`` groups the rows by term and calls each model once (batching),
returns ``y_hat +- q`` and the OOD flag with its reason. It lives in the
train package because it calls scikit-learn / LightGBM / XGBoost models (the
``ml-libraries-only-in-ml-train`` contract); the planner never imports it.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from threadpoolctl import threadpool_limits

from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.train.algorithms import (
    LEARNED,
    FitContext,
    Model,
    configs_for,
    fit_one,
)
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.conformal import conformal_q
from meshforge.controller.ml.train.data import FloatArray, Table
from meshforge.controller.ml.train.ood import OodModel, fit_ood
from meshforge.controller.ml.train.select import select
from meshforge.controller.planner.predictor import TermPrediction, TermQuery

__all__ = ["InMemoryPredictor", "TermModel", "train_in_memory"]


@dataclass(frozen=True, slots=True)
class TermModel:
    """One term's shipped candidate (module docstring)."""

    term: str
    model: Model
    q: float
    """Conformal half-width (log space); ``inf`` = never usable."""
    ood: OodModel
    config: str
    """The configuration's name (``lightgbm(n_estimators=400,...)``)."""


@dataclass(slots=True)
class InMemoryPredictor:
    """Module docstring. ``version`` names the model set (a digest of the
    dataset and the shipped configurations, :func:`train_in_memory`)."""

    version: str
    models: Mapping[str, TermModel] = field(default_factory=dict)
    calls: int = 0
    """How many ``predict`` calls (the batching tests count them)."""

    @property
    def predictor_version(self) -> str:
        return self.version

    def has_model(self, term: str) -> bool:
        return term in self.models

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        self.calls += 1
        out: list[TermPrediction | None] = [None] * len(rows)
        by_term: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            if len(row.features) != len(NUMERIC_FEATURES):
                raise ValueError("a feature row has the wrong length")
            by_term.setdefault(row.term, []).append(index)
        with threadpool_limits(limits=1):
            for term, indices in by_term.items():
                found = self.models.get(term)
                if found is None:
                    raise KeyError(f"no model for term {term!r}")
                X: FloatArray = np.asarray(
                    [rows[i].features for i in indices], dtype=np.float64
                )
                y_hat = found.model.predict(X, np.arange(len(indices), dtype=np.intp))
                flags, reasons = found.ood.check(X)
                for k, i in enumerate(indices):
                    value = float(y_hat[k])
                    out[i] = TermPrediction(
                        y_hat=value,
                        lo=value - found.q,
                        hi=value + found.q,
                        ood=bool(flags[k]),
                        reason=reasons[k],
                        model_version=self.version,
                    )
        return [p for p in out if p is not None]


def train_in_memory(
    table: Table,
    settings: CompareSettings | None = None,
    *,
    terms: Sequence[str] | None = None,
) -> InMemoryPredictor:
    """Per term of ``terms`` (default: ``settings.terms``) the shipped
    candidate on all its rows (module docstring). A term under the D348 floor,
    or whose pick is a baseline, gets no model. Deterministic (D352)."""
    settings = settings or CompareSettings()
    wanted = tuple(settings.terms if terms is None else terms)
    configs = configs_for(settings.algorithms)
    ctx = FitContext(
        table=table,
        with_family=False,
        calibration=settings.calibration,
        seed=settings.seed,
        fold="final",
    )
    models: dict[str, TermModel] = {}
    with threadpool_limits(limits=1):
        for term in wanted:
            rows = table.rows_of(term)
            families = set(table.families_of(rows).values())
            if (
                len(rows) < settings.min_rows
                or len(families) < settings.min_families
                or len(table.families_of(rows)) < 2
            ):
                continue
            selection = select(
                ctx, rows, configs, inner_k=settings.inner_k, seed=settings.seed
            )
            if selection.selected.algorithm not in LEARNED:
                continue
            models[term] = TermModel(
                term=term,
                model=fit_one(selection.selected, ctx, rows),
                q=conformal_q(selection.oof_residuals, settings.alpha),
                ood=fit_ood(
                    table.X[rows],
                    margin=settings.ood_margin,
                    k=settings.ood_k,
                    percentile=settings.ood_percentile,
                ),
                config=selection.selected.name,
            )
    payload = json.dumps(
        {
            "dataset": table.digest,
            "models": {
                t: [m.config, None if math.isinf(m.q) else round(m.q, 12)]
                for t, m in sorted(models.items())
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    version = "mem-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return InMemoryPredictor(version=version, models=models)
