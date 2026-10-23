"""What the planner's ML predictions come from (V0.15 G3, D339, D358) - the
model-set counterpart of the calibration's
:class:`~meshforge.controller.calibration_store.CalibrationSource`.

``[controller.planner] ml``:

* ``off`` - no model; :meth:`ModelSource.refresh` answers None (the planner
  plans exactly as V0.14);
* ``auto`` - the set :meth:`~.registry.ModelRegistry.serving_version` names
  (pinned, else active), re-read by version on every :meth:`refresh` (one
  cheap query) and loaded only when it changed;
* ``sha256:<hex>`` - that set, pinned in configuration. :meth:`check` (at
  startup) refuses a set that does not exist, was ``rejected`` by the gate,
  or cannot be loaded - :class:`~meshforge.common.errors.ConfigError`, never
  a silent fallback (as D306/D301 for the calibration).

A failed read keeps the set it had (logged once per failure streak): a
database hiccup never changes what the planner uses. A set that cannot be
loaded (artefact gone, its library not installed) is remembered, logged
once, and the source answers None for it (the planner falls back to the
calibration) - it is tried again only every ``retry_s`` (60 s), so a
restored blob serves again without a restart.

A set trained on another **feature version** than this code's
(:data:`~.schema.FEATURE_VERSION`; D366 moved it to 2) is never loaded: one
``controller.planner.ml_set_stale_features`` info line, the source answers
None (the planner keeps calibration) until a set of this feature version
serves - the next retrain trains one.

Loading runs in a worker thread (:meth:`~.registry.ModelRegistry.load`).

**What the planner reads (G3 + G4 merge).** :class:`SetPredictor` is the
planner's :class:`~meshforge.controller.planner.predictor.TermPredictor`
over a :class:`~.serve.LoadedSet`, and :class:`RegistryMlInputSource` is the
dispatcher's :class:`~.predictor.MlInputSource` (``server.ml_source``): per
tick it calls :meth:`ModelSource.refresh` (one cheap query) and, when the
loaded set changed, builds the planner's
:class:`~meshforge.controller.planner.predictor.MlInput` with
:func:`~.predictor.ml_input` (warm-up outside the budget; ``ml_max_interval_log``,
``ml_predict_budget_ms``). A set whose warm-up fails is not used (None).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from typing import Final, Literal

import numpy as np

from meshforge.common.errors import ConfigError
from meshforge.common.logging import get_logger
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.predictor import DEFAULT_PREDICT_BUDGET_MS, ml_input
from meshforge.controller.ml.registry import MlRegistryError, ModelRegistry
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.serve import LoadedModel, LoadedSet
from meshforge.controller.planner.predictor import (
    DEFAULT_MAX_INTERVAL_LOG,
    MlInput,
    PredictorFailed,
    TermPrediction,
    TermQuery,
)

__all__ = [
    "ML_OFF",
    "STALE_FEATURES",
    "MlMode",
    "ModelSource",
    "RegistryMlInputSource",
    "SetPredictor",
    "parse_ml_mode",
]

log = get_logger(__name__)

ML_OFF: Final = "off"
UNREADABLE_RETRY_S: Final = 60.0
"""A set that could not be loaded is tried again after this long."""
STALE_FEATURES: Final = "stale_feature_version"
""":attr:`ModelSource.unavailable` for a set of another feature version."""
type MlMode = Literal["auto", "off", "pinned"]


def parse_ml_mode(value: str) -> tuple[MlMode, str | None]:
    """``[controller.planner] ml`` -> (mode, pinned set version)."""
    if value in ("auto", "off"):
        return value, None  # type: ignore[return-value]
    return "pinned", value


class ModelSource:
    """See module docstring."""

    def __init__(
        self,
        registry: ModelRegistry,
        setting: str,
        *,
        retry_s: float = UNREADABLE_RETRY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._retry_s = retry_s
        self._clock = clock
        self.mode, self.pinned = parse_ml_mode(setting)
        self.current: LoadedSet | None = None
        self.kind: str | None = None
        """``pinned`` | ``active`` | ``config`` (what :attr:`current` is)."""
        self.failures = 0
        self.unavailable: str | None = None
        """Why the serving set is not loaded (``artefact_missing``,
        ``library_missing``, ...); None when loaded or nothing serves."""
        self._failing = False
        self._unreadable: str | None = None
        self._unreadable_at = 0.0
        """When :attr:`_unreadable` was last tried (fix r1, D2: it is tried
        again after ``retry_s`` - a restored blob serves without a restart)."""
        self._stale: str | None = None
        """The serving set of another feature version (never loaded; logged
        once)."""

    async def check(self) -> None:
        """Startup (module docstring): a configured pin must exist, not be
        ``rejected`` and load."""
        if self.mode != "pinned" or self.pinned is None:
            return
        row = await self._registry.get(self.pinned)
        where = f"[controller.planner] ml = {self.pinned}"
        if row is None:
            raise ConfigError(
                f"{where}: no such model set (see compute planner ml models)"
            )
        if row.status == "rejected":
            raise ConfigError(
                f"{where}: that set was rejected by the activation gate "
                f"({row.reason}) and is never used (pin an active or superseded set)"
            )
        try:
            self.current = await self._registry.load(row)
        except ArtefactError as exc:
            raise ConfigError(
                f"{where}: that set cannot be loaded ({exc.details.get('reason')})"
            ) from None
        self.kind = "config"

    async def refresh(self) -> LoadedSet | None:
        """The set to plan with now (module docstring)."""
        if self.mode == "off":
            return None
        if self.mode == "pinned":
            return self.current
        try:
            kind, version = await self._registry.serving_version()
            if version is None:
                self.current, self.kind, self.unavailable = None, None, None
            elif version == self._stale:
                self.current, self.kind = None, kind  # quietly: logged once
            elif (
                version == self._unreadable
                and self._clock() - self._unreadable_at < self._retry_s
            ):
                self.current = None  # known bad: logged once, retried later
            elif self.current is None or self.current.set_version != version:
                await self._load(kind, version)
            else:
                self.kind = kind
        except MlRegistryError as exc:
            self.failures += 1
            if not self._failing:
                log.warning(
                    "controller.planner.ml_read_failed",
                    cause=exc.details.get("cause"),
                    kept_set=None if self.current is None else self.current.set_version,
                )
            self._failing = True
            return self.current
        except Exception as exc:  # a bug here must never stop the dispatch
            self.failures += 1
            if not self._failing:
                log.exception(
                    "controller.planner.ml_read_crashed",
                    error_type=type(exc).__name__,
                )
            self._failing = True
            return self.current
        self._failing = False
        return self.current

    async def _load(self, kind: str | None, version: str) -> None:
        row = await self._registry.get(version)
        if row is None:  # changed between the two reads; next refresh
            return
        if row.feature_version != FEATURE_VERSION:
            # trained on other features (D366): never loaded, logged once;
            # the planner keeps calibration until a new set is trained
            log.info(
                "controller.planner.ml_set_stale_features",
                ml_set_version=version,
                set_feature_version=row.feature_version,
                feature_version=FEATURE_VERSION,
                hint="the next retrain trains a set for this feature version",
            )
            self._stale = version
            self.current, self.kind, self.unavailable = None, kind, STALE_FEATURES
            return
        try:
            loaded = await self._registry.load(row)
        except ArtefactError as exc:
            reason = str(exc.details.get("reason", "unreadable"))
            if self._unreadable != version or self.unavailable != reason:
                log.error(  # noqa: TRY400 - a typed reason, no traceback needed
                    "controller.planner.ml_set_unloadable",
                    ml_set_version=version,
                    reason=reason,
                )
            self._unreadable = version
            self._unreadable_at = self._clock()
            self.current, self.kind, self.unavailable = None, kind, reason
            return
        self._unreadable = None
        log.info(
            "controller.planner.ml_set_loaded",
            ml_set_version=version,
            kind=kind,
            terms=list(loaded.terms),
        )
        self.current, self.kind, self.unavailable = loaded, kind, None


# ------------------------------------------------- the planner's view (G3+G4)


def _usable(model: LoadedModel) -> bool:
    """An infinite conformal ``q`` (too few rows for an interval) can never
    pass the D336 width test: such a member is "no model" to the planner
    (asking it would only produce non-finite intervals)."""
    return math.isfinite(model.q)


class SetPredictor:
    """The planner's ``TermPredictor`` over one loaded model set (module
    docstring). ``predictor_version`` = the set version (what decisions and
    replays record); each answer's ``model_version`` = the member that
    answered. A member ``none`` (or with no usable interval) -> ``has_model``
    False, so the planner never asks for it (``no_model``). Any failure of a
    member while predicting (an :class:`~.artefact.ArtefactError` from a
    native library, a wrong row width) is a
    :class:`~meshforge.controller.planner.predictor.PredictorFailed` - the
    decision falls back to calibration (D363)."""

    def __init__(self, loaded: LoadedSet) -> None:
        self.loaded = loaded
        self._models = {
            term: model for term, model in loaded.models.items() if _usable(model)
        }

    @property
    def predictor_version(self) -> str:
        return self.loaded.set_version

    def has_model(self, term: str) -> bool:
        return term in self._models

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        out: list[TermPrediction | None] = [None] * len(rows)
        by_term: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            by_term.setdefault(row.term, []).append(index)
        for term, indices in by_term.items():
            model = self._models.get(term)
            if model is None:
                raise PredictorFailed(f"no_model:{term}")
            try:
                answer = model.predict(
                    np.asarray([rows[i].features for i in indices], dtype=np.float64)
                )
            except ArtefactError as exc:
                raise PredictorFailed(
                    f"artefact_error:{exc.details.get('reason', 'bad_model')}"
                ) from exc
            except ValueError as exc:  # wrong feature width / non-numeric row
                raise PredictorFailed("malformed_features") from exc
            for k, i in enumerate(indices):
                out[i] = TermPrediction(
                    y_hat=float(answer.y_hat[k]),
                    lo=float(answer.lo[k]),
                    hi=float(answer.hi[k]),
                    ood=bool(answer.ood[k]),
                    reason=answer.reasons[k],
                    model_version=answer.model_version,
                )
        return [p for p in out if p is not None]


class RegistryMlInputSource:
    """``server.ml_source`` (module docstring): the dispatcher's
    :class:`~.predictor.MlInputSource` over a :class:`ModelSource`."""

    def __init__(
        self,
        models: ModelSource,
        *,
        max_interval_log: float = DEFAULT_MAX_INTERVAL_LOG,
        budget_ms: float = DEFAULT_PREDICT_BUDGET_MS,
    ) -> None:
        self.models = models
        """The registry-side source (API, retrain task, tests)."""
        self.max_interval_log = max_interval_log
        self.budget_ms = budget_ms
        self._loaded: LoadedSet | None = None
        self._input: MlInput | None = None

    async def current(self) -> MlInput | None:
        """The model to plan this tick with (None = no model / not usable).
        Never raises (a failed read keeps the set, :meth:`ModelSource.
        refresh`)."""
        loaded = await self.models.refresh()
        if loaded is None:
            self._loaded = self._input = None
            return None
        if loaded is not self._loaded:
            self._loaded = loaded
            # warm-up = one batch per modelled term: off the event loop
            self._input = await asyncio.to_thread(
                ml_input,
                SetPredictor(loaded),
                max_interval_log=self.max_interval_log,
                budget_ms=self.budget_ms,
            )
            log.info(
                "controller.planner.ml_input_ready"
                if self._input is not None
                else "controller.planner.ml_input_unavailable",
                ml_set_version=loaded.set_version,
            )
        return self._input
