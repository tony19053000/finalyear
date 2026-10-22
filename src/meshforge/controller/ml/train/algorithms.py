"""The compared algorithms and baselines behind one protocol (V0.15 G2, D337,
D349, D350, D352).

Every algorithm has a **fixed grid** of at most four configurations
(:data:`GRID`, simplest first - no unbounded search); :func:`fit_many` fits
several configurations on the same training rows and returns one
:class:`Model` per configuration (``predict(X, rows) -> y_hat``, the
residual in log space).

**Simplicity order** (ties and the one-standard-error rule pick the
simplest, :data:`SIMPLICITY`): ``raw`` < ``calibration`` < ``ridge`` <
``knn`` < ``tree`` < ``forest`` < ``lightgbm`` < ``xgboost``; inside one
algorithm, the grid order.

**Baselines** (D350): ``raw`` predicts 0 (the uncalibrated analytical
model); ``calibration`` refits the V0.14 factors **on the training rows
only** with the planner's own pure ``fit`` and key ladder (node + family ->
node -> global) and :class:`~meshforge.controller.planner.calibration.
CalibrationConfig`, and predicts ``ln(factor)`` exactly as the live planner
would apply it (:func:`~meshforge.controller.planner.calibration.
applied_factor`; a ``measured`` term or one the planner never scales -
``chunk_rate``, ``combine`` - gets factor 1). Observations are built with
the calibration's own :func:`~meshforge.controller.planner.calibration.
term_observation` - shared, never copied. Per ML term the factors are fitted
on that term's rows only (``lane_stage`` rows fit the ``transfer`` table the
planner applies to a lane's stage): a baseline at least as specific as the
live one.

**Determinism** (D352): fixed seeds, one thread everywhere (``n_jobs=1``,
``num_threads=1``, ``nthread=1``), LightGBM ``deterministic=True,
force_row_wise=True``, XGBoost ``tree_method="hist"``. The compare loop also
caps BLAS threads (:mod:`.compare`).

**Shared boosting prefix**: the LightGBM / XGBoost configurations that differ
only in the number of rounds are fitted **once** with the most rounds and
predict with the first ``n_estimators`` rounds - gradient boosting without
row/column sampling builds the same first trees either way (a unit test
holds the predictions equal), at half the cost.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import lightgbm as lgb
import numpy as np
import xgboost as xgb
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.neighbors import KNeighborsRegressor
from sklearn.tree import DecisionTreeRegressor

from meshforge.controller.ml.schema import term as schema_term
from meshforge.controller.ml.train.data import FloatArray, IntArray, Table
from meshforge.controller.ml.train.prep import Prep, fit_prep, native, transform
from meshforge.controller.planner.calibration import (
    Calibration,
    CalibrationConfig,
    MatchedRun,
    applied_factor,
    fit,
    term_observation,
)

__all__ = [
    "BASELINES",
    "GRID",
    "LEARNED",
    "SIMPLICITY",
    "Config",
    "FitContext",
    "Model",
    "Observer",
    "configs_for",
    "fit_many",
    "fit_one",
]

SIMPLICITY: Final = (
    "raw",
    "calibration",
    "ridge",
    "knn",
    "tree",
    "forest",
    "lightgbm",
    "xgboost",
)
BASELINES: Final = ("raw", "calibration")
LEARNED: Final = SIMPLICITY[2:]

_LR: Final = 0.05
_MIN_CHILD: Final = 20

GRID: Final[Mapping[str, tuple[Mapping[str, Any], ...]]] = {
    "raw": ({},),
    "calibration": ({},),
    # most regularised first
    "ridge": ({"alpha": 10.0}, {"alpha": 1.0}, {"alpha": 0.1}),
    "knn": ({"n_neighbors": 25}, {"n_neighbors": 10}),
    "tree": ({"max_depth": 3}, {"max_depth": 6}),
    "forest": ({"max_depth": 6}, {"max_depth": 12}),
    "lightgbm": (
        {"num_leaves": 7, "n_estimators": 200},
        {"num_leaves": 7, "n_estimators": 400},
        {"num_leaves": 15, "n_estimators": 200},
        {"num_leaves": 15, "n_estimators": 400},
    ),
    "xgboost": (
        {"max_depth": 3, "n_estimators": 200},
        {"max_depth": 3, "n_estimators": 400},
        {"max_depth": 6, "n_estimators": 200},
        {"max_depth": 6, "n_estimators": 400},
    ),
}
"""The fixed grid (module docstring). Fixed settings: tree
``min_samples_leaf=20``; forest 100 trees, ``min_samples_leaf=5``,
``max_features=0.5``; LightGBM ``learning_rate=0.05``,
``min_child_samples=20``; XGBoost ``eta=0.05``."""

type Observer = Callable[[str, str, IntArray], None]
"""``(kind, outer fold, row indices)``: told which rows each fitted part saw
(the leakage test instruments the whole loop with it)."""


@dataclass(frozen=True, slots=True)
class Config:
    algorithm: str
    params: tuple[tuple[str, Any], ...] = ()
    index: int = 0
    """Position in :data:`GRID` (the simplicity order inside the algorithm)."""

    @property
    def name(self) -> str:
        if not self.params:
            return self.algorithm
        inner = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.algorithm}({inner})"

    @property
    def rank(self) -> tuple[int, int]:
        return SIMPLICITY.index(self.algorithm), self.index

    @property
    def learned(self) -> bool:
        return self.algorithm in LEARNED

    def param(self, key: str) -> Any:
        return dict(self.params)[key]


def configs_for(algorithms: Sequence[str]) -> list[Config]:
    """Both baselines + every grid configuration of ``algorithms`` (learned
    names only; unknown names raise ``ValueError``), in simplicity order."""
    unknown = sorted(set(algorithms) - set(LEARNED))
    if unknown:
        raise ValueError(f"unknown algorithm(s): {', '.join(unknown)}")
    wanted = [*BASELINES, *(a for a in LEARNED if a in set(algorithms))]
    return [
        Config(name, tuple(sorted(params.items())), index)
        for name in wanted
        for index, params in enumerate(GRID[name])
    ]


@dataclass(frozen=True, slots=True)
class FitContext:
    table: Table
    with_family: bool = False
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    seed: int = 0
    fold: str = ""
    """The outer fold being evaluated ("final" for the all-row selection)."""
    observer: Observer | None = None

    def observe(self, kind: str, rows: IntArray) -> None:
        if self.observer is not None:
            self.observer(kind, self.fold, rows)

    def families(self, rows: IntArray) -> list[str]:
        return [self.table.family[i] for i in rows]


class Model(Protocol):
    def predict(self, X: FloatArray, rows: IntArray) -> FloatArray:
        """``y_hat`` for feature rows ``X`` (``rows`` = their table indices,
        for the ids a baseline or the family columns need)."""
        ...


# ------------------------------------------------------------------ baselines


class _Raw:
    def predict(self, X: FloatArray, rows: IntArray) -> FloatArray:
        return np.zeros(len(rows), dtype=np.float64)


def _observation(table: Table, i: int, actual_s: float) -> Any:
    return term_observation(
        schema_term(table.term[i]).planner_term,
        float(table.raw_s[i]),
        actual_s,
        table.source[i],
    )


class _Calibration:
    def __init__(self, ctx: FitContext, rows: IntArray) -> None:
        table = ctx.table
        ctx.observe("calibration", rows)
        runs = [
            MatchedRun(
                decision_id=table.decision_id[i],
                attempt_id=table.attempt_id[i],
                node_id=table.node_id[i],
                finished_at=table.finished_at[i],
                terms=(_observation(table, int(i), float(table.actual_s[i])),),
                family=table.family_raw[i],
                fingerprint=table.group[i],
            )
            for i in rows
        ]
        self.table = table
        self.calibration: Calibration = fit(runs, ctx.calibration)

    def predict(self, X: FloatArray, rows: IntArray) -> FloatArray:
        out = np.empty(len(rows), dtype=np.float64)
        cal = self.calibration
        for k, i in enumerate(rows):
            # the factor never depends on the actual: NaN proves it
            obs = _observation(self.table, int(i), math.nan)
            factor = applied_factor(
                cal, obs, self.table.node_id[i], cal.family_of(self.table.group[i])
            )
            out[k] = math.log(factor)
        return out


# ------------------------------------------------------------------ scikit-learn


class _Sklearn:
    def __init__(
        self, ctx: FitContext, rows: IntArray, estimator: Any, *, standardise: bool
    ) -> None:
        table = ctx.table
        ctx.observe("prep", rows)
        families = ctx.families(rows) if ctx.with_family else None
        self.prep: Prep = fit_prep(table.X[rows], families, with_family=ctx.with_family)
        self.ctx = ctx
        self.standardise = standardise
        ctx.observe("model", rows)
        self.estimator = estimator.fit(
            transform(self.prep, table.X[rows], families, standardise=standardise),
            table.y[rows],
        )

    def predict(self, X: FloatArray, rows: IntArray) -> FloatArray:
        families = self.ctx.families(rows) if self.ctx.with_family else None
        Z = transform(self.prep, X, families, standardise=self.standardise)
        return np.asarray(self.estimator.predict(Z), dtype=np.float64)


def _sklearn(config: Config, ctx: FitContext, rows: IntArray) -> _Sklearn:
    seed = ctx.seed
    if config.algorithm == "ridge":
        est: Any = Ridge(alpha=config.param("alpha"), solver="cholesky")
        return _Sklearn(ctx, rows, est, standardise=True)
    if config.algorithm == "knn":
        k = max(1, min(int(config.param("n_neighbors")), len(rows)))
        est = KNeighborsRegressor(n_neighbors=k, algorithm="brute", n_jobs=1)
        return _Sklearn(ctx, rows, est, standardise=True)
    if config.algorithm == "tree":
        est = DecisionTreeRegressor(
            max_depth=config.param("max_depth"),
            min_samples_leaf=_MIN_CHILD,
            random_state=seed,
        )
        return _Sklearn(ctx, rows, est, standardise=False)
    est = RandomForestRegressor(
        n_estimators=100,
        max_depth=config.param("max_depth"),
        min_samples_leaf=5,
        max_features=0.5,
        random_state=seed,
        n_jobs=1,
    )
    return _Sklearn(ctx, rows, est, standardise=False)


# ------------------------------------------------------------------ boosting


@dataclass(slots=True)
class _Booster:
    """A fitted boosting model shared by the configurations of one grid
    point; ``rounds`` = the rounds this configuration predicts with."""

    ctx: FitContext
    prep: Prep
    booster: Any
    kind: str
    rounds: int

    def predict(self, X: FloatArray, rows: IntArray) -> FloatArray:
        families = self.ctx.families(rows) if self.ctx.with_family else None
        Z = native(self.prep, X, families)
        if self.kind == "lightgbm":
            out = self.booster.predict(Z, num_iteration=self.rounds)
        else:
            out = self.booster.predict(
                xgb.DMatrix(Z, missing=np.nan, nthread=1),
                iteration_range=(0, self.rounds),
            )
        return np.asarray(out, dtype=np.float64)


def lightgbm_params(num_leaves: int, seed: int, **extra: Any) -> dict[str, Any]:
    """The LightGBM parameters (D352 determinism) for one grid point."""
    return {
        "objective": "regression",
        "num_leaves": num_leaves,
        "learning_rate": _LR,
        "min_child_samples": _MIN_CHILD,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "seed": seed,
        "bagging_fraction": 1.0,
        "feature_fraction": 1.0,
        "verbose": -1,
        **extra,
    }


def fit_lightgbm(
    ctx: FitContext, rows: IntArray, params: Mapping[str, Any], rounds: int
) -> tuple[Prep, Any]:
    """A LightGBM booster on ``rows`` (also the quantile alternative)."""
    families = ctx.families(rows) if ctx.with_family else None
    prep = fit_prep(ctx.table.X[rows], families, with_family=ctx.with_family)
    ctx.observe("model", rows)
    data = lgb.Dataset(
        native(prep, ctx.table.X[rows], families),
        label=ctx.table.y[rows],
        params={"verbose": -1},
        free_raw_data=True,
    )
    return prep, lgb.train(dict(params), data, num_boost_round=rounds)


def _xgboost(
    ctx: FitContext, rows: IntArray, depth: int, rounds: int
) -> tuple[Prep, Any]:
    families = ctx.families(rows) if ctx.with_family else None
    prep = fit_prep(ctx.table.X[rows], families, with_family=ctx.with_family)
    ctx.observe("model", rows)
    data = xgb.DMatrix(
        native(prep, ctx.table.X[rows], families),
        label=ctx.table.y[rows],
        missing=np.nan,
        nthread=1,
    )
    params = {
        "objective": "reg:squarederror",
        "tree_method": "hist",
        "max_depth": depth,
        "eta": _LR,
        "nthread": 1,
        "seed": ctx.seed,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "verbosity": 0,
    }
    return prep, xgb.train(params, data, num_boost_round=rounds)


def _boosted(
    configs: Sequence[Config], ctx: FitContext, rows: IntArray
) -> dict[Config, Model]:
    out: dict[Config, Model] = {}
    key = "num_leaves" if configs[0].algorithm == "lightgbm" else "max_depth"
    by_shape: dict[int, list[Config]] = {}
    for config in configs:
        by_shape.setdefault(int(config.param(key)), []).append(config)
    for shape, group in by_shape.items():
        most = max(int(c.param("n_estimators")) for c in group)
        if configs[0].algorithm == "lightgbm":
            prep, booster = fit_lightgbm(
                ctx, rows, lightgbm_params(shape, ctx.seed), most
            )
        else:
            prep, booster = _xgboost(ctx, rows, shape, most)
        for config in group:
            out[config] = _Booster(
                ctx, prep, booster, config.algorithm, int(config.param("n_estimators"))
            )
    return out


# ------------------------------------------------------------------ fit


def fit_one(config: Config, ctx: FitContext, rows: IntArray) -> Model:
    """``config`` fitted on table rows ``rows``."""
    return fit_many([config], ctx, rows)[config]


def fit_many(
    configs: Sequence[Config], ctx: FitContext, rows: IntArray
) -> dict[Config, Model]:
    """Every configuration fitted on the same training ``rows`` (module
    docstring: boosting shares its prefix)."""
    out: dict[Config, Model] = {}
    by_algorithm: dict[str, list[Config]] = {}
    for config in configs:
        by_algorithm.setdefault(config.algorithm, []).append(config)
    for algorithm, group in by_algorithm.items():
        if algorithm == "raw":
            out.update({c: _Raw() for c in group})
        elif algorithm == "calibration":
            model = _Calibration(ctx, rows)
            out.update(dict.fromkeys(group, model))
        elif algorithm in ("lightgbm", "xgboost"):
            out.update(_boosted(group, ctx, rows))
        else:
            out.update({c: _sklearn(c, ctx, rows) for c in group})
    return out
