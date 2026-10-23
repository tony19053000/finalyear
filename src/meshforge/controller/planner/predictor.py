"""The ML term predictor the planner may be given (V0.15 G4, D336, D362-D365;
ADR-0021).

The planner stays the planner: it enumerates, checks feasibility, costs every
candidate with the analytical model and ranks. A **predictor** only corrects
the cost of learnable *terms*: the analytical (pre-calibration) seconds ``raw``
become ``raw x exp(y_hat)``, where ``y_hat`` is the model's predicted residual
``ln(actual / raw)`` (D335). Everything here is pure - no numpy, no clock, no
logging, no ``controller.ml`` import (the planner's AST tests): the model and
the feature builder are **injected** (:class:`MlInput`), built by
``controller/ml`` once per model version.

**Learnable terms** (the dataset's names, :data:`ML_TERMS`): a whole job's
``transfer``, ``env``, ``compute``, ``output``; a split job's lane
``lane_stage`` / ``lane_env`` (the lane's ``transfer`` / ``env``),
``chunk_rate`` (seconds per item) and the set's ``combine``.

**Precedence per term, per candidate (D336):**

1. the term's prediction is **exact by construction** (D345,
   :func:`exact_by_construction`) -> the model is never asked
   (``exact_by_construction``);
2. the decision's predictor failed (D363, below) -> ``predictor_error``;
3. no model for the term -> ``no_model``;
4. the raw term is shorter than the dataset's floor (``MIN_PREDICTED_S``,
   0.05 s; ``chunk_rate``: not positive) - no training row was ever that
   small, so a correction would be pure extrapolation ->
   ``below_min_predicted``;
5. the model says the row is out of distribution -> ``ood``;
6. its interval is wider than ``max_interval_log`` (``hi - lo``) ->
   ``interval_too_wide``;
7. otherwise **ML is used**: seconds = ``raw x exp(y_hat)`` (capped like
   every term), ``source`` unchanged, ``detail.ml = {model_version, y_hat,
   lo, hi, used: true, raw_s}``.

A term ML does not correct falls back to the V0.14 **calibration** factor
(if any), else stays raw - with ``detail.ml = {model_version, used: false,
reason}``. So one OOD node keeps calibration while the other candidates of
the same decision use the model.

**D363 - ML can never fail a plan.** The predictor (or the feature builder)
raising, or answering with the wrong number of rows / a non-finite value, is
a :class:`PredictorFailed`; :func:`~.plan.plan` then plans the **whole
decision** again without asking the model - every learnable term falls back
to calibration with reason ``predictor_error`` - and names the error in
:attr:`~.types.Decision.ml`. A predictor slower than its budget is turned
into an error by its wrapper (``controller/ml/predictor.py``: the planner has
no clock).

**Batching (D336).** A :class:`MlSession` asks the predictor once per
:meth:`MlSession.resolve` call with every query of that stage: Mode 1 = one
call for all feasible candidates x terms; Mode 3 = one call for every lane's
``lane_stage`` / ``lane_env`` / ``chunk_rate``, then one for the ``combine``
of every costed candidate (a set's combine node and lane count are known
only after the corrected lanes are costed).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, cast

from meshforge.controller.planner.calibration import MIN_PREDICTED_S
from meshforge.controller.planner.estimator import bounded
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    EstimateTerm,
    MlUse,
    PlanRequest,
)

__all__ = [
    "DEFAULT_MAX_INTERVAL_LOG",
    "MAX_Y_HAT",
    "ML_TERMS",
    "UNKNOWN_VERSION",
    "FeatureFn",
    "MlInput",
    "MlOutcome",
    "MlReason",
    "MlSession",
    "MlUse",
    "PredictorFailed",
    "TermContext",
    "TermPrediction",
    "TermPredictor",
    "TermQuery",
    "corrected",
    "exact_by_construction",
    "ml_block",
    "ml_term",
    "stored_raw",
]

ML_TERMS: Final = (
    "transfer",
    "env",
    "compute",
    "output",
    "lane_stage",
    "lane_env",
    "chunk_rate",
    "combine",
)
"""The learnable terms, by their dataset names (``controller/ml/schema.py``
``TERM_NAMES`` - a unit test holds the two equal)."""
DEFAULT_MAX_INTERVAL_LOG: Final = math.log(2.0)
"""D338 ``ml_max_interval_log``: the widest usable interval (full width, log
space - the interval spans at most a factor 2)."""
MAX_Y_HAT: Final = 50.0
"""``|y_hat|`` above this is not a prediction (``e^50`` ~ 5e21): treated as a
malformed answer (:class:`PredictorFailed`), never applied."""
UNKNOWN_VERSION: Final = "unknown"
"""The version recorded when the predictor cannot name its own (G4 fix r1,
O-1): the decision then falls back (``predictor_error``)."""
_ROUND: Final = 6

_STAGE_TERMS: Final = frozenset({"transfer", "lane_stage"})
_ENV_TERMS: Final = frozenset({"env", "lane_env"})


def exact_by_construction(term: str, detail: Mapping[str, Any]) -> bool:
    """D345: the term's prediction is **exact by construction** - a
    structural 0 s, not a time derived from a rate or a history - so there is
    no residual to learn (and nothing for a model to correct). The rule is by
    term and ``detail``, never by ``source`` alone (a transfer over a fresh
    probed link is ``measured`` bandwidth but a *predicted* time: learnable):

    * ``transfer`` / ``lane_stage`` with ``detail.missing_bytes == 0`` - the
      node holds every tree, nothing moves;
    * ``env`` / ``lane_env`` with ``detail.env_cached`` true - the built
      environment is in the node's cache.

    ``queue`` is never a learnable term (always 0 s, a feasible node has a
    free slot). Every other term (``compute``, ``output``, ``chunk_rate``,
    ``combine``, a transfer that moves bytes, an env that is built) is
    learnable. One definition for the dataset (``controller/ml/targets.py``
    re-exports it) and the planner (V0.15 G4 moved it here: the planner may
    not import ``controller.ml``)."""
    if term in _STAGE_TERMS:
        missing = detail.get("missing_bytes")
        return (
            isinstance(missing, int) and not isinstance(missing, bool) and missing == 0
        )
    if term in _ENV_TERMS:
        return detail.get("env_cached") is True
    return False


def stored_raw(term: str, seconds: float) -> float:
    """The term's raw prediction **as the stored decision row holds it** -
    what the dataset's ``raw_s`` (and feature ``an_raw_s``) will be read back
    as (D362: train and serve see the same number): the seconds rounded to 6
    decimals (``decision_json``); ``chunk_rate`` (seconds per item) = ``1 /``
    the row's ``rate_items_s`` (items/s rounded to 6 decimals), or the
    seconds as is when that rounds to 0."""
    if term == "chunk_rate":
        if not seconds > 0.0 or not math.isfinite(seconds):
            return seconds
        rate = round(1.0 / seconds, _ROUND)
        return 1.0 / rate if rate > 0.0 else seconds
    return round(seconds, _ROUND)


# ------------------------------------------------------------------ protocol


@dataclass(frozen=True, slots=True)
class TermContext:
    """What the planner knows of one learnable term of one candidate - the
    input of the injected :data:`FeatureFn` (the dataset builds the same
    from the stored row: ``controller/ml/features.Predicted``)."""

    term: str
    """:data:`ML_TERMS` name."""
    node_id: str
    raw_s: float
    """:func:`stored_raw` of the pre-calibration analytical seconds
    (``chunk_rate``: seconds per item)."""
    source: str | None
    """The analytical term's :class:`~.types.TermSource` value."""
    detail: Mapping[str, Any] = field(default_factory=dict)
    lanes: int = 1
    """Active lanes of the costed candidate (1 = a whole job or one lane)."""
    chunk_items: int | None = None
    """Items per chunk of the costed candidate (None = a whole job)."""


type FeatureFn = Callable[
    [PlanRequest, ClusterSnapshot, dt.datetime | None, TermContext],
    tuple[float, ...],
]
"""``(request, snapshot, now, term) -> feature row`` (NaN = missing, D334)."""


@dataclass(frozen=True, slots=True)
class TermQuery:
    term: str
    node_id: str
    features: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class TermPrediction:
    y_hat: float
    """Predicted residual ``ln(actual / raw)``."""
    lo: float
    hi: float
    """The interval of ``y_hat`` (log space)."""
    ood: bool
    reason: str | None
    """Why the row is out of distribution (the model's words), else None."""
    model_version: str
    """The model (artefact) that answered."""


class TermPredictor(Protocol):
    """A batch predictor over feature rows (``controller/ml`` builds one per
    model version)."""

    @property
    def predictor_version(self) -> str:
        """The model set's version (``plan_decision`` / replay ``ml``)."""
        ...

    def has_model(self, term: str) -> bool:
        """A learned model exists for ``term`` (else ``no_model``, never
        asked)."""
        ...

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        """One prediction per row, in order."""
        ...


@dataclass(frozen=True, slots=True)
class MlInput:
    """What :func:`~.plan.plan` gets to use a model (D336)."""

    predictor: TermPredictor
    features: FeatureFn
    max_interval_log: float = DEFAULT_MAX_INTERVAL_LOG
    fallback: str | None = None
    """Set = do not ask the model: every learnable term falls back with
    reason ``predictor_error`` (a replay of a decision whose predictor failed
    re-plans exactly so; the value is the original error)."""

    @property
    def version(self) -> str:
        return self.predictor.predictor_version


class MlReason(StrEnum):
    """Why ML did not correct a term (``detail.ml.reason``)."""

    NO_MODEL = "no_model"
    OOD = "ood"
    INTERVAL_TOO_WIDE = "interval_too_wide"
    EXACT = "exact_by_construction"
    BELOW_MIN = "below_min_predicted"
    PREDICTOR_ERROR = "predictor_error"
    COMBINE_NODE_UNKNOWN = "combine_node_unknown"
    """V0.15 G5a: a ``combine`` term whose detail names no combine node -
    its features cannot be built, so the model is not asked (never a
    silent guess of the node)."""


class PredictorFailed(Exception):  # noqa: N818 - a signal, named for what happened
    """The predictor (or the feature builder) failed: the decision falls back
    (module docstring, D363)."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True, slots=True)
class MlOutcome:
    """One term's result of :meth:`MlSession.resolve`."""

    used: bool
    reason: MlReason | None = None
    prediction: TermPrediction | None = None


# ------------------------------------------------------------------ session


def _finite(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _valid(prediction: Any) -> bool:
    return (
        isinstance(prediction, TermPrediction)
        and _finite(prediction.y_hat)
        and _finite(prediction.lo)
        and _finite(prediction.hi)
        and abs(prediction.y_hat) <= MAX_Y_HAT
        and prediction.lo <= prediction.hi
        and isinstance(prediction.model_version, str)
    )


def _too_small(ctx: TermContext) -> bool:
    """The raw term is below every training row (module docstring, rule 4)."""
    if ctx.term == "chunk_rate":
        return not ctx.raw_s > 0.0
    return not ctx.raw_s >= MIN_PREDICTED_S


class MlSession:
    """One decision's use of the predictor (module docstring)."""

    def __init__(
        self,
        ml: MlInput,
        request: PlanRequest,
        snapshot: ClusterSnapshot,
        now: dt.datetime | None,
        *,
        failed: str | None = None,
    ) -> None:
        self.ml = ml
        # G4 fix r1 (O-1): even the version is the model's code - read it
        # inside the D363 guard; a raising or non-text version = the
        # decision falls back (predictor_error), never a failed plan
        try:
            version = ml.version
        except Exception as exc:  # noqa: BLE001 - D363: any model fault falls back
            version, failed = UNKNOWN_VERSION, failed or type(exc).__name__
        if not isinstance(version, str) or not version:
            version, failed = UNKNOWN_VERSION, failed or "malformed_version"
        self.version: str = version
        self.request = request
        self.snapshot = snapshot
        self.now = now
        self.error = failed if failed is not None else ml.fallback
        """None = the predictor is asked; else the decision falls back."""
        self._features: dict[Hashable, tuple[float, ...]] = {}
        """V0.15 G5a (G3/G4 reviewer debt): feature rows already built in
        this decision, by the full term context (:func:`_context_key`)."""

    def use(self) -> MlUse:
        return MlUse(version=self.version, error=self.error)

    def _row(self, ctx: TermContext) -> tuple[float, ...]:
        """``ctx``'s feature row, built once per decision for one context
        (V0.15 G5a): the feature builder is a pure function of the
        decision's request, snapshot and ``now`` (fixed for the session) and
        the context, so a context seen before gets the same row. A context
        whose detail cannot be keyed is built every time."""
        key = _context_key(ctx)
        if key is not None:
            found = self._features.get(key)
            if found is not None:
                return found
        row = self.ml.features(self.request, self.snapshot, self.now, ctx)
        if key is not None:
            self._features[key] = row
        return row

    def skip(self, term: str, reason: MlReason) -> MlOutcome:
        """The outcome of a ``term`` the caller cannot ask the model for
        (``reason``, e.g. :attr:`MlReason.COMBINE_NODE_UNKNOWN`), with the
        precedence of the module docstring before it: a failed decision is
        ``predictor_error``, a term without a model ``no_model``. Raises
        :class:`PredictorFailed` when asking for the model raises (D363)."""
        if self.error is not None:
            return MlOutcome(False, MlReason.PREDICTOR_ERROR)
        try:
            known = self.ml.predictor.has_model(term)
        except Exception as exc:
            raise PredictorFailed(type(exc).__name__) from exc
        return MlOutcome(False, reason if known else MlReason.NO_MODEL)

    def resolve(self, contexts: Sequence[TermContext]) -> list[MlOutcome]:
        """One :class:`MlOutcome` per context, asking the predictor **once**
        for every context that needs it (the precedence of the module
        docstring). Raises :class:`PredictorFailed` (D363)."""
        out: list[MlOutcome | None] = [None] * len(contexts)
        asked: list[int] = []
        queries: list[TermQuery] = []
        predictor = self.ml.predictor
        try:
            for index, ctx in enumerate(contexts):
                if exact_by_construction(ctx.term, ctx.detail):
                    out[index] = MlOutcome(False, MlReason.EXACT)
                elif self.error is not None:  # nothing of the model is touched
                    out[index] = MlOutcome(False, MlReason.PREDICTOR_ERROR)
                elif not predictor.has_model(ctx.term):
                    out[index] = MlOutcome(False, MlReason.NO_MODEL)
                elif _too_small(ctx):
                    out[index] = MlOutcome(False, MlReason.BELOW_MIN)
                else:
                    queries.append(TermQuery(ctx.term, ctx.node_id, self._row(ctx)))
                    asked.append(index)
            answers = list(predictor.predict(queries)) if queries else []
        except PredictorFailed:
            raise
        except Exception as exc:
            raise PredictorFailed(type(exc).__name__) from exc
        if len(answers) != len(queries) or not all(_valid(a) for a in answers):
            raise PredictorFailed("malformed_prediction")
        limit = self.ml.max_interval_log
        for index, answer in zip(asked, answers, strict=True):
            if answer.ood:
                out[index] = MlOutcome(False, MlReason.OOD, answer)
            elif not answer.hi - answer.lo <= limit:
                out[index] = MlOutcome(False, MlReason.INTERVAL_TOO_WIDE, answer)
            else:
                out[index] = MlOutcome(True, None, answer)
        return [
            o if o is not None else MlOutcome(False, MlReason.NO_MODEL) for o in out
        ]


def _freeze(value: Any) -> Hashable:
    """``value`` as a hashable key (mappings by sorted items, sequences as
    tuples); ``TypeError`` for anything else unhashable."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    hash(value)  # TypeError for an unhashable leaf
    return cast("Hashable", value)


def _context_key(ctx: TermContext) -> Hashable | None:
    """Everything of ``ctx`` a feature row may depend on; None = not
    keyable (the row is then never cached)."""
    try:
        detail = _freeze(ctx.detail)
    except TypeError:
        return None
    return (
        ctx.term,
        ctx.node_id,
        ctx.raw_s,
        ctx.source,
        ctx.lanes,
        ctx.chunk_items,
        detail,
    )


def _r(value: float) -> float:
    return round(value, _ROUND)


def ml_block(outcome: MlOutcome, version: str, raw_s: float) -> dict[str, Any]:
    """``detail.ml`` of a term (module docstring)."""
    answer = outcome.prediction
    block: dict[str, Any] = {
        "model_version": version if answer is None else answer.model_version,
        "used": outcome.used,
    }
    if answer is not None:
        block.update(y_hat=_r(answer.y_hat), lo=_r(answer.lo), hi=_r(answer.hi))
    if outcome.used:
        block["raw_s"] = raw_s
    else:
        block["reason"] = None if outcome.reason is None else outcome.reason.value
        if outcome.reason is MlReason.OOD and answer is not None and answer.reason:
            block["ood_reason"] = answer.reason
    return block


def corrected(seconds: float, y_hat: float) -> float:
    """``seconds x exp(y_hat)``, capped like every term."""
    return bounded(seconds * math.exp(min(y_hat, MAX_Y_HAT)))


def ml_term(
    term: EstimateTerm,
    outcome: MlOutcome,
    *,
    name: str,
    version: str,
    fallback: EstimateTerm,
) -> EstimateTerm:
    """``term`` with the precedence applied: ML used -> ``raw x
    exp(y_hat)``; else ``fallback`` (``term`` calibrated, or ``term`` itself)
    - either way with its ``detail.ml``."""
    raw = stored_raw(name, term.seconds)
    if outcome.used and outcome.prediction is not None:
        return EstimateTerm(
            corrected(term.seconds, outcome.prediction.y_hat),
            term.source,
            {**term.detail, "ml": ml_block(outcome, version, raw)},
        )
    base = fallback
    return EstimateTerm(
        base.seconds,
        base.source,
        {**base.detail, "ml": ml_block(outcome, version, raw)},
    )
