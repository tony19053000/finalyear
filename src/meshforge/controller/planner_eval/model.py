"""The evaluator's inputs, settings and policy rows (V0.14 G4, D297/D313-D317).

Plain frozen dataclasses: the DB loader (:mod:`.loader`) builds them from the
campaign's labels, run records and ``plan_decision`` replays; the pure core
(:mod:`.core`) reads nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from meshforge.bench.stats import BOOTSTRAP_LEVEL, BOOTSTRAP_RESAMPLES
from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.planner_eval.latency import LatencyConfig

__all__ = [
    "COST_MODEL",
    "CRITERION_POLICY",
    "EVAL_VERSION",
    "ORACLE",
    "RULE_POLICIES",
    "UNMEASURED",
    "ConfigRef",
    "EvalConfig",
    "EvalGroup",
    "EvalRecord",
    "PolicySpec",
    "default_policies",
]

EVAL_VERSION: Final = 1
"""Version of the report's shape (``planner_eval_report.report``); bump on
any change of it (D318)."""
ORACLE: Final = "oracle"
"""D298: evaluation only - never in :data:`~meshforge.common.manifest.
POLICIES`, so the API, the manifest and the planner all refuse it."""
COST_MODEL: Final = "cost_model"
RULE_POLICIES: Final = tuple(p for p in POLICIES if p != COST_MODEL)
"""Criterion 2's comparison set: every placement policy but ``cost_model``."""
UNMEASURED: Final = "unmeasured"
"""The vote of a record whose pick matches no measured configuration."""

type PolicyKind = Literal["rule", "cost_model", "oracle"]


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """One evaluated policy row (D315)."""

    policy: str
    calibrated: bool = False
    """``cost_model`` only: plan with the held-out calibration (D316)."""
    objective: str | None = None
    """``cost_model`` only: the objective it ranks by (D295)."""

    @property
    def kind(self) -> PolicyKind:
        if self.policy == ORACLE:
            return "oracle"
        return "cost_model" if self.policy == COST_MODEL else "rule"

    @property
    def key(self) -> str:
        if self.policy != COST_MODEL:
            return self.policy
        state = "calibrated" if self.calibrated else "uncalibrated"
        return f"{COST_MODEL}:{state}:{self.objective or 'min_time'}"


CRITERION_POLICY: Final = PolicySpec(COST_MODEL, calibrated=True, objective="min_time")
"""The row criteria 1 and 2 judge."""


def default_policies() -> tuple[PolicySpec, ...]:
    """Every rule policy, ``cost_model`` x {uncalibrated, calibrated} x each
    objective, and the oracle (D315), in report order."""
    rules = tuple(PolicySpec(p) for p in RULE_POLICIES)
    costs = tuple(
        PolicySpec(COST_MODEL, calibrated=calibrated, objective=objective)
        for calibrated in (False, True)
        for objective in OBJECTIVES
    )
    return (*rules, *costs, PolicySpec(ORACLE))


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
