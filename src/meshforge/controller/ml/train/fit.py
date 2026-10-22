"""Train the shipped model set (V0.15 G3, D347 shipped-candidate path, D339,
D358).

:func:`train_set` = per term, on the given rows (all of them for the set that
ships; the gate's fit slice for the gate, :mod:`.gate`):

1. the D348 floor (``min_rows``, ``min_families``, >= 2 workload groups) -
   else the term is a member with ``algorithm = "none"``,
   ``insufficient_data``;
2. :func:`~.select.select` (inner grouped k-fold + one-standard-error rule) -
   **the G2 code, unchanged**; a baseline pick (``raw`` / ``calibration``)
   -> member ``none``, ``no_learned_model``;
3. the selected configuration refit on the rows, the split-conformal ``q``
   from the selection's out-of-fold residuals, the D338 OOD test fitted on
   the rows (:func:`~.ood.fit_ood`) - no new statistics;
4. the artefact (:mod:`..artefact`): the fitted model as data (ridge
   coefficients, kNN matrix, tree node arrays, LightGBM text, XGBoost JSON),
   its preprocessing, ``q`` and OOD arrays; ``version`` = its digest.

``set_version`` (:func:`set_version`, D358) = the digest of the sorted
``(term, member version)`` pairs and the feature version. Everything is
deterministic (D352: fixed seeds, one thread, rows in canonical order), so
the same dataset content gives byte-identical artefacts and the same set
version (criterion 3).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

import numpy as np
from threadpoolctl import threadpool_limits

from meshforge.controller.ml.artefact import (
    ARTEFACT_VERSION,
    array_block,
    conformal_block,
    digest,
    encode,
    ood_block,
    prep_block,
)
from meshforge.controller.ml.prep import transform
from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES, TERM_NAMES
from meshforge.controller.ml.train.algorithms import (
    Config,
    FitContext,
    Observer,
    configs_for,
    fit_one,
)
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.conformal import conformal_q
from meshforge.controller.ml.train.data import IntArray, Table
from meshforge.controller.ml.train.ood import fit_ood
from meshforge.controller.ml.train.select import Selection, select

__all__ = [
    "Member",
    "TrainResult",
    "artefact_bytes",
    "none_version",
    "set_version",
    "train_set",
    "train_term",
]

_DISTS = {
    "ridge": ("scikit-learn",),
    "knn": ("scikit-learn",),
    "tree": ("scikit-learn",),
    "forest": ("scikit-learn",),
    "lightgbm": ("lightgbm",),
    "xgboost": ("xgboost", "xgboost-cpu"),
}


@dataclass(frozen=True, slots=True)
class Member:
    """One term of a set: a learned model (``artefact`` bytes) or ``none``."""

    term: str
    algorithm: str
    """A learned algorithm, or ``none``."""
    version: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    reason: str | None = None
    """Why ``none`` (``insufficient_data: ...`` / ``no_learned_model: ...``)."""
    artefact: bytes | None = None
    config: Config | None = None
    """The selected configuration (learned members; not stored)."""

    @property
    def learned(self) -> bool:
        return self.algorithm != "none"


@dataclass(frozen=True, slots=True)
class TrainResult:
    set_version: str
    dataset_digest: str
    feature_version: int
    members: tuple[Member, ...]
    """In :data:`~..schema.TERM_NAMES` order."""
    rows: int
    settings: dict[str, Any] = field(default_factory=dict)

    def member(self, term: str) -> Member | None:
        return next((m for m in self.members if m.term == term), None)

    @property
    def learned(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.learned)


def _library(algorithm: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {"numpy": np.__version__}
    for dist in _DISTS[algorithm]:
        try:
            out[dist.replace("-cpu", "")] = metadata.version(dist)
            break
        except metadata.PackageNotFoundError:
            continue
    return out


def none_version(term: str, reason: str, dataset_digest: str) -> str:
    """The version of a ``none`` member: its descriptor's digest."""
    return digest(
        encode(
            {
                "algorithm": "none",
                "dataset_digest": dataset_digest,
                "feature_version": FEATURE_VERSION,
                "reason": reason,
                "term": term,
            }
        )
    )


def set_version(members: Sequence[Member], feature_version: int) -> str:
    """D358: the digest of the sorted ``(term, version)`` pairs + the
    feature version."""
    return digest(
        encode(
            {
                "feature_version": feature_version,
                "members": sorted([m.term, m.version] for m in members),
            }
        )
    )


def _tree_block(tree: Any) -> dict[str, Any]:
    """scikit-learn's ``tree_`` node arrays (leaf value = ``value[:, 0, 0]``)."""
    return {
        "left": array_block(tree.children_left, "<i8"),
        "right": array_block(tree.children_right, "<i8"),
        "feature": array_block(tree.feature, "<i8"),
        "threshold": array_block(tree.threshold),
        "value": array_block(np.asarray(tree.value)[:, 0, 0]),
    }


def _model_block(
    config: Config, model: Any, X: Any, y: Any
) -> tuple[dict[str, Any], bool]:
    """``(model block, standardise)`` of a fitted learned model."""
    algorithm = config.algorithm
    if algorithm == "lightgbm":
        return {
            "text": model.booster.model_to_string(num_iteration=model.rounds),
            "rounds": int(model.rounds),
        }, False
    if algorithm == "xgboost":
        raw = bytes(model.booster[: model.rounds].save_raw("json"))
        return {"json": raw.decode("utf-8")}, False
    est = model.estimator
    if algorithm == "ridge":
        return {
            "coef": array_block(est.coef_),
            "intercept": float(est.intercept_),
        }, True
    if algorithm == "knn":
        return {
            "reference": array_block(transform(model.prep, X, standardise=True)),
            "y": array_block(y),
            "k": int(est.n_neighbors),
        }, True
    if algorithm == "tree":
        return {"trees": [_tree_block(est.tree_)]}, False
    return {"trees": [_tree_block(e.tree_) for e in est.estimators_]}, False


def artefact_bytes(
    table: Table,
    term: str,
    config: Config,
    model: Any,
    rows: IntArray,
    q: float,
    settings: CompareSettings,
) -> bytes:
    """The canonical artefact of a fitted model (module docstring)."""
    X = table.X[rows]
    block, standardise = _model_block(config, model, X, table.y[rows])
    ood = fit_ood(
        X,
        margin=settings.ood_margin,
        k=settings.ood_k,
        percentile=settings.ood_percentile,
    )
    reference = (
        None if ood.neighbours is None else transform(ood.prep, X, standardise=True)
    )
    doc = {
        "artefact_version": ARTEFACT_VERSION,
        "term": term,
        "algorithm": config.algorithm,
        "params": dict(config.params),
        "feature_version": FEATURE_VERSION,
        "features": list(NUMERIC_FEATURES),
        "dataset_digest": table.digest,
        "libraries": _library(config.algorithm),
        "prep": prep_block(model.prep),
        "standardise": standardise,
        "conformal": conformal_block(q, settings.alpha),
        "ood": ood_block(
            columns=ood.columns,
            low=ood.low,
            high=ood.high,
            prep=ood.prep,
            reference=reference,
            threshold=ood.threshold,
            k=ood.k,
        ),
        "model": block,
    }
    return encode(doc)


def _scores(sel: Selection) -> dict[str, Any]:
    chosen = next(s for s in sel.scores if s.config == sel.selected)
    return {
        "inner_k": sel.k,
        "selected": sel.selected.name,
        "best": sel.best.name,
        "inner_male": chosen.mean if math.isfinite(chosen.mean) else None,
        "inner_se": chosen.se if math.isfinite(chosen.se) else None,
    }


def train_term(
    table: Table,
    term: str,
    rows: IntArray,
    settings: CompareSettings,
    observer: Observer | None = None,
    *,
    config: Config | None = None,
) -> Member:
    """One term's member on ``rows`` (module docstring). ``config`` = refit
    this configuration instead of selecting one (the activation gate judges
    the shipped configuration, :mod:`.gate`): its interval still comes from
    grouped out-of-fold residuals; only the >= 2 groups floor applies."""
    family_of = table.families_of(rows)
    families = len(set(family_of.values()))
    sizes = {"rows": len(rows), "families": families, "groups": len(family_of)}
    if len(family_of) < 2 or (
        config is None
        and (len(rows) < settings.min_rows or families < settings.min_families)
    ):
        reason = (
            f"insufficient_data: {len(rows)} rows, {families} families, "
            f"{len(family_of)} groups (needs >= {settings.min_rows} rows, >= "
            f"{settings.min_families} families, >= 2 groups)"
        )
        return Member(
            term,
            "none",
            none_version(term, reason, table.digest),
            {},
            sizes,
            reason=reason,
        )
    ctx = FitContext(
        table=table,
        with_family=False,
        calibration=settings.calibration,
        seed=settings.seed,
        fold="final",
        observer=observer,
    )
    configs = [config] if config is not None else configs_for(settings.algorithms)
    sel = select(ctx, rows, configs, inner_k=settings.inner_k, seed=settings.seed)
    q = conformal_q(sel.oof_residuals, settings.alpha)
    metrics = {
        **sizes,
        **_scores(sel),
        "conformal_q": q if math.isfinite(q) else None,
        "interval_width": 2.0 * q if math.isfinite(q) else None,
    }
    if not sel.selected.learned:
        reason = (
            f"no_learned_model: the selection kept the {sel.selected.name} "
            "baseline (no learned model within one standard error of the best)"
        )
        return Member(
            term,
            "none",
            none_version(term, reason, table.digest),
            {},
            metrics,
            reason=reason,
        )
    model = fit_one(sel.selected, ctx, rows)
    data = artefact_bytes(table, term, sel.selected, model, rows, q, settings)
    return Member(
        term,
        sel.selected.algorithm,
        digest(data),
        dict(sel.selected.params),
        {**metrics, "artefact_bytes": len(data)},
        artefact=data,
        config=sel.selected,
    )


def train_set(
    table: Table,
    settings: CompareSettings,
    *,
    allowed: np.ndarray | None = None,
    observer: Observer | None = None,
    fixed: Mapping[str, Config] | None = None,
) -> TrainResult:
    """Every term of ``settings.terms`` (in :data:`TERM_NAMES` order) on the
    table's rows - or only the rows where ``allowed`` (a boolean mask) is
    true; BLAS capped at one thread (D352). ``fixed`` (the gate): refit only
    these terms' configurations; every other term is a ``none`` member
    (``not_shipped``)."""
    unknown = sorted(set(settings.terms) - set(TERM_NAMES))
    if unknown:
        raise ValueError(f"unknown term(s): {', '.join(unknown)}")
    members: list[Member] = []
    with threadpool_limits(limits=1):
        for term in TERM_NAMES:
            if term not in settings.terms:
                continue
            rows = table.rows_of(term)
            if allowed is not None:
                rows = rows[allowed[rows]]
            if fixed is not None and term not in fixed:
                reason = "not_shipped: the shipped set has no learned model"
                members.append(
                    Member(
                        term,
                        "none",
                        none_version(term, reason, table.digest),
                        {},
                        {"rows": len(rows)},
                        reason=reason,
                    )
                )
                continue
            members.append(
                train_term(
                    table,
                    term,
                    rows,
                    settings,
                    observer,
                    config=None if fixed is None else fixed[term],
                )
            )
    return TrainResult(
        set_version=set_version(members, FEATURE_VERSION),
        dataset_digest=table.digest,
        feature_version=FEATURE_VERSION,
        members=tuple(members),
        rows=table.n if allowed is None else int(np.count_nonzero(allowed)),
    )
