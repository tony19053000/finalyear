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
* :class:`ModelSource` is what the dispatcher reads once per tick (like the
  calibration's ``CalibrationSource``). G4 ships :class:`StaticModelSource`
  (a fixed model, or none); G3's registry loader implements the same
  protocol.

No ML library is imported here (the predictor is injected).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from meshforge.controller.ml.features import term_features
from meshforge.controller.planner.predictor import (
    DEFAULT_MAX_INTERVAL_LOG,
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
    "ModelSource",
    "StaticModelSource",
    "ml_input",
]

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


def ml_input(
    predictor: TermPredictor,
    *,
    max_interval_log: float = DEFAULT_MAX_INTERVAL_LOG,
    budget_ms: float | None = DEFAULT_PREDICT_BUDGET_MS,
    clock: Callable[[], float] = time.perf_counter,
) -> MlInput:
    """The planner's input for ``predictor`` (module docstring);
    ``budget_ms`` None = no budget (offline replays and evaluations, where
    time does not decide anything)."""
    if not (math.isfinite(max_interval_log) and max_interval_log >= 0.0):
        raise ValueError("ml_max_interval_log must be a non-negative number")
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


class ModelSource(Protocol):
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
