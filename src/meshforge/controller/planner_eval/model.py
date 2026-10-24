"""The evaluator's inputs, settings and policy rows (V0.14 G4, D297/D313-D317).

Plain frozen dataclasses: the DB loader (:mod:`.loader`) builds them from the
campaign's labels, run records and ``plan_decision`` replays; the pure core
(:mod:`.core`) reads nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from meshforge.bench.stats import BOOTSTRAP_LEVEL, BOOTSTRAP_RESAMPLES
from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.planner_eval.latency import LatencyConfig

__all__ = [
    "COST_MODEL",
    "CRITERION_POLICY",
    "EVAL_VERSION",
    "ML_CRITERION_POLICY",
    "ML_EVALUATED",
    "ML_UNAVAILABLE",
    "ORACLE",
    "RULE_POLICIES",
    "UNMEASURED",
    "ConfigRef",
    "EvalConfig",
    "EvalGroup",
    "EvalMl",
    "EvalRecord",
    "PolicySpec",
    "default_policies",
]

EVAL_VERSION: Final = 2
"""Version of the report's shape (``planner_eval_report.report``); bump on
any change of it (D318). 2 (V0.15 G5a, D369): ``cost_model:ml`` policy rows
and the ``ml`` block (criteria, fallback counts, fold models, per-term
metrics); a version 1 report has neither and still renders."""
ORACLE: Final = "oracle"
"""D298: evaluation only - never in :data:`~meshforge.common.manifest.
POLICIES`, so the API, the manifest and the planner all refuse it."""
COST_MODEL: Final = "cost_model"
RULE_POLICIES: Final = tuple(p for p in POLICIES if p != COST_MODEL)
"""Criterion 2's comparison set: every placement policy but ``cost_model``."""
UNMEASURED: Final = "unmeasured"
"""The vote of a record whose pick matches no measured configuration."""

type PolicyKind = Literal["rule", "cost_model", "oracle"]


class EvalCancelledError(Exception):
    """The evaluation was stopped (the controller stops): raised in the
    replay thread at the next group / family boundary (G6b reviewer N1)."""


class CancelFlag(Protocol):
    """What a stop request looks like to the pure replay (a
    ``threading.Event`` in the service; no thread import here)."""

    def is_set(self) -> bool: ...


def check_cancel(cancel: CancelFlag | None) -> None:
    """Raise :class:`EvalCancelledError` when ``cancel`` is set."""
    if cancel is not None and cancel.is_set():
        raise EvalCancelledError("the evaluation was stopped")


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """One evaluated policy row (D315)."""

    policy: str
    calibrated: bool = False
    """``cost_model`` only: plan with the held-out calibration (D316)."""
    objective: str | None = None
    """``cost_model`` only: the objective it ranks by (D295)."""
    ml: bool = False
    """V0.15 G5a (D369): ``cost_model`` only - plan with the held-out
    calibration **and** the family's fold model (D336 precedence: ML ->
    calibration -> raw, per term). Always calibrated."""

    def __post_init__(self) -> None:
        if self.ml and (self.policy != COST_MODEL or not self.calibrated):
            raise ValueError("an ML row is a calibrated cost_model row")

    @property
    def kind(self) -> PolicyKind:
        if self.policy == ORACLE:
            return "oracle"
        return "cost_model" if self.policy == COST_MODEL else "rule"

    @property
    def key(self) -> str:
        if self.policy != COST_MODEL:
            return self.policy
        if self.ml:
            return f"{COST_MODEL}:ml:{self.objective or 'min_time'}"
        state = "calibrated" if self.calibrated else "uncalibrated"
        return f"{COST_MODEL}:{state}:{self.objective or 'min_time'}"


CRITERION_POLICY: Final = PolicySpec(COST_MODEL, calibrated=True, objective="min_time")
"""The row criteria 1 and 2 judge."""
ML_CRITERION_POLICY: Final = PolicySpec(
    COST_MODEL, calibrated=True, objective="min_time", ml=True
)
"""V0.15 G5a (D370): the row the ML criteria judge against
:data:`CRITERION_POLICY` (calibrated analytical v2)."""
ML_EVALUATED: Final = "evaluated"
ML_UNAVAILABLE: Final = "ml_unavailable"


def default_policies(*, ml: bool = False) -> tuple[PolicySpec, ...]:
    """Every rule policy, ``cost_model`` x {uncalibrated, calibrated} x each
    objective, (``ml``: ``cost_model:ml`` x each objective, V0.15 G5a) and
    the oracle (D315), in report order."""
    rules = tuple(PolicySpec(p) for p in RULE_POLICIES)
    costs = tuple(
        PolicySpec(COST_MODEL, calibrated=calibrated, objective=objective)
        for calibrated in (False, True)
        for objective in OBJECTIVES
    )
    learned = (
        tuple(
            PolicySpec(COST_MODEL, calibrated=True, objective=objective, ml=True)
            for objective in OBJECTIVES
        )
        if ml
        else ()
    )
    return (*rules, *costs, *learned, PolicySpec(ORACLE))


@dataclass(frozen=True, slots=True)
class EvalConfig:
    """The fixed numbers of an evaluation (all written into the report)."""

    regret_threshold_pct: float = 15.0
    within_target_pct: float = 80.0
    min_groups: int = 10
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES
    bootstrap_seed: int = 20_261_022
    ci_level: float = BOOTSTRAP_LEVEL
    latency: LatencyConfig = field(default_factory=LatencyConfig)
    """V0.14 G6 (D324): criterion 3's budget, fabric size and minimum count
    (written into the report's ``latency`` block)."""
    ml_family_tolerance_pp: float = 5.0
    """V0.15 D370 (criterion 2 of V0.15): per family, ML mean regret may be
    at most this many **percentage points** above calibrated analytical."""
    ml_min_family_groups: int = 3
    """V0.15 D370: a family with fewer common groups is listed
    ``too_few_groups``, never counted as a pass."""


@dataclass(frozen=True, slots=True)
class EvalMl:
    """V0.15 G5a (D368/D369): what the ML rows plan with, made by the caller
    (:mod:`.service`; the core never trains or loads a model).

    ``status`` :data:`ML_EVALUATED` = the ``cost_model:ml`` rows are
    planned, each family with ``inputs[family]`` (its fold model, D368;
    None = the fold has no usable model: the row plans with calibration
    only and the decision counts as ``no_fold_model``). Anything else (e.g.
    :data:`ML_UNAVAILABLE`: no ``ml`` extra, ``ml = off``) = no ML rows,
    every ML verdict that status (never ``FAIL``), ``reason`` says why."""

    status: str
    reason: str | None = None
    inputs: Mapping[str, MlInput | None] = field(default_factory=dict)
    folds: Sequence[Mapping[str, Any]] = ()
    """One JSON-ready entry per evaluated family (its fold set version, why
    it has no model, the rows it trained on, ...), written as is."""
    terms: Mapping[str, Any] = field(default_factory=dict)
    """Per-term held-out prediction metrics of the fold models (MALE, MAPE,
    coverage), JSON-ready, written as is."""
    until: str | None = None
    """The fold models' training cut-off (ISO time; rows that finished
    later were not trained on); None = no fold was trained."""

    @property
    def evaluated(self) -> bool:
        return self.status == ML_EVALUATED


@dataclass(frozen=True, slots=True)
class ConfigRef:
    """One configuration of a group (``bench_run`` + its label costs)."""

    config_id: str
    strategy: str
    """``local`` | ``mode1`` | ``mode3``."""
    node_id: str
    origin_node_id: str
    node_set: tuple[str, ...] = ()
    """``mode3`` only, sorted (a campaign's sets are stored sorted)."""
    state: Literal["measured", "unsupported"] = "measured"
    median_ms: float | None = None
    repetitions: int = 0


@dataclass(frozen=True, slots=True)
class EvalGroup:
    """One labelled group (:class:`~meshforge.controller.telemetry.labels.
    GroupLabel` + the configurations' node sets)."""

    campaign_id: str
    group_id: str
    family: str
    condition: str
    label: Literal["best", "tie", "insufficient", "pending"]
    best_config_id: str | None
    tied_config_ids: tuple[str, ...]
    configs: tuple[ConfigRef, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class EvalRecord:
    """One kept run record of a group (a label row) and its decision's
    replay (``plan_decision.replay``; None = none stored)."""

    campaign_id: str
    group_id: str
    config_id: str
    repetition: int
    attempt_id: str
    job_id: str
    fingerprint: str | None = None
    replay: Mapping[str, Any] | None = field(default=None, compare=False)
    decision_id: int | None = None
