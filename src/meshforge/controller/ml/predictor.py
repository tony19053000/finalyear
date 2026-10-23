"""What the dispatcher hands the planner to use an ML model (V0.15 G4, D336,
D362, D363).

The planner is pure and never imports ``controller.ml``
(``planner/predictor.py``); this module builds its
:class:`~meshforge.controller.planner.predictor.MlInput` once per model
version:

* the **feature builder** is :func:`~.features.term_features` - the very
  :func:`~.features.features` the dataset rows are built with (D334/D362: no
  train/serve skew; the parity test holds them equal bit for bit);
* the **predictor** is wrapped in :class:`BudgetedPredictor`: a ``predict``
  call slower than ``[controller.planner] ml_predict_budget_ms`` (20 ms) is
  turned into an error, so the planner discards its answer and the decision
  falls back to calibration (D363). A synchronous call cannot be pre-empted:
  the budget bounds what a slow model can *change*, and the error (logged once
  a minute by the dispatcher) says the model is too slow for live planning;
* the model is **warmed up** when it is built (G4 fix r1, G4-R1-2): one
  representative batch per modelled term goes through the real predictor
  *outside* the budget, so the first live decision after an activation is not
  a cold call (measured ~28 ms on a fresh LightGBM + kNN model, over the 20 ms
  budget) that would always fall back. A model whose warm-up fails is treated
  as **unavailable** (logged, ``ml_input`` returns None -> no model), never a
  crash;
* :class:`MlInputSource` is what the dispatcher reads once per tick (like the
  calibration's ``CalibrationSource``). G4 ships :class:`StaticModelSource`
  (a fixed model, or none); the registry's serving set is
  :class:`~.source.RegistryMlInputSource` (``server.ml_source``, G3 + G4
  merge). Named ``MlInputSource`` - not ``ModelSource``, which is the
  registry-side loader of model sets (:class:`~.source.ModelSource`).

No ML library is imported here (the predictor is injected).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from meshforge.common.logging import get_logger
from meshforge.controller.ml.features import term_features
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.planner.predictor import (
    DEFAULT_MAX_INTERVAL_LOG,
    ML_TERMS,
    MlInput,
    PredictorFailed,
    TermPrediction,
    TermPredictor,
    TermQuery,
)

__all__ = [
    "BUDGET_EXCEEDED",
    "DEFAULT_PREDICT_BUDGET_MS",
    "BudgetedPredictor",
    "MlInputSource",
    "StaticModelSource",
    "ml_input",
    "warm_up",
]

log = get_logger(__name__)

WARM_UP_ROWS: Final = 4
"""Rows per modelled term in the warm-up batch (a decision asks a few per
term: one per candidate node)."""

DEFAULT_PREDICT_BUDGET_MS: Final = 20.0
"""D363 ``ml_predict_budget_ms``."""
BUDGET_EXCEEDED: Final = "budget_exceeded"
"""The error a too-slow ``predict`` call becomes (``Decision.ml.error``)."""


class BudgetedPredictor:
    """``inner`` with a time budget per ``predict`` call (module docstring)."""

    def __init__(
        self,
        inner: TermPredictor,
        budget_ms: float = DEFAULT_PREDICT_BUDGET_MS,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not (math.isfinite(budget_ms) and budget_ms > 0.0):
            raise ValueError("the predict budget must be a positive number of ms")
        self.inner = inner
        self.budget_ms = budget_ms
        self._clock = clock
        self.last_ms: float | None = None
        """How long the last ``predict`` call took (ms)."""

    @property
    def predictor_version(self) -> str:
        return self.inner.predictor_version

    def has_model(self, term: str) -> bool:
        return self.inner.has_model(term)

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        started = self._clock()
        answers = self.inner.predict(rows)
        took = (self._clock() - started) * 1000.0
        self.last_ms = took
        if took > self.budget_ms:
            raise PredictorFailed(BUDGET_EXCEEDED)
        return answers


def _warm_rows() -> list[tuple[float, ...]]:
    """Representative rows: all features missing, all zero, and two in
    between - every branch a tree / the kNN imputation can take on NaN and on
    values."""
    width = len(NUMERIC_FEATURES)
    nan = math.nan
    rows = [(nan,) * width, (0.0,) * width]
    rows += [
        tuple(float(10**k) if j % 2 else nan for j in range(width)) for k in (1, 6)
    ]
    return rows[:WARM_UP_ROWS]


def warm_up(predictor: TermPredictor) -> bool:
    """One representative ``predict`` batch for every term the predictor has
    a model for, outside any budget (module docstring). False (and one log
    line) when anything of it fails - the model is then not used."""
    version = "unknown"
    error: str | None = None
    try:
        version = str(predictor.predictor_version)
        queries = [
            TermQuery(term, "warm-up", features)
            for term in ML_TERMS
            if predictor.has_model(term)
            for features in _warm_rows()
        ]
        if queries and len(list(predictor.predict(queries))) != len(queries):
            error = "malformed_prediction"
    except Exception as exc:  # noqa: BLE001 - a broken model is unavailable, not a crash
        error = type(exc).__name__
    if error is None:
        return True
    log.warning(
        "controller.ml.model_warm_up_failed",
        ml_model_version=version,
        error_type=error,
    )
    return False


def ml_input(
    predictor: TermPredictor,
    *,
    max_interval_log: float = DEFAULT_MAX_INTERVAL_LOG,
    budget_ms: float | None = DEFAULT_PREDICT_BUDGET_MS,
    clock: Callable[[], float] = time.perf_counter,
) -> MlInput | None:
    """The planner's input for ``predictor`` (module docstring), warmed up;
    None = the warm-up failed (the model is unavailable: plan without it).
    ``budget_ms`` None = no budget (offline replays and evaluations, where
    time does not decide anything). ``ValueError`` for nonsense limits."""
    if not (math.isfinite(max_interval_log) and max_interval_log >= 0.0):
        raise ValueError("ml_max_interval_log must be a non-negative number")
    if budget_ms is not None and not (math.isfinite(budget_ms) and budget_ms > 0.0):
        raise ValueError("the predict budget must be a positive number of ms")
    if not warm_up(predictor):
        return None
    wrapped: TermPredictor = (
        predictor
        if budget_ms is None
        else BudgetedPredictor(predictor, budget_ms, clock=clock)
    )
    return MlInput(
        predictor=wrapped,
        features=term_features,
        max_interval_log=max_interval_log,
    )


class MlInputSource(Protocol):
    """Where the dispatcher gets the tick's model (module docstring)."""

    async def current(self) -> MlInput | None:
        """The model to plan this tick with; None = no model. Must not
        raise for a missing model (the dispatcher keeps planning)."""
        ...


class StaticModelSource:
    """A fixed model, or none (G4; tests and the in-memory predictor)."""

    def __init__(self, ml: MlInput | None) -> None:
        self._ml = ml

    async def current(self) -> MlInput | None:
        return self._ml
