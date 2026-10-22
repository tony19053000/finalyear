"""D319: the regret check of a new calibration version (D301 completed).

G2's activation gate (D306) compares prediction errors. D319 adds, on the same
held-out slice (the newest :data:`~meshforge.controller.planner.calibration.
HOLDOUT_FRAC` of each fingerprint's matched runs, D329), the question that matters: **does the new
version make the planner pick worse configurations?**

* The slice's labelled groups are the ``best`` / ``tie`` groups of the
  campaigns that held the slice's jobs, restricted to the groups those jobs ran
  in (:func:`~.loader.load_for_jobs`).
* Calibrated ``cost_model`` (``min_time``) is evaluated twice on them with the
  evaluator core - once with the new version, once with the active one
  (no active version = uncalibrated) - and compared **paired**, on the groups
  counted under both.
* Fewer than :data:`MIN_GROUPS` such groups -> ``skipped`` (``too_few_groups``,
  recorded in the row's ``reason``): the error check alone decides.
* Else the new version passes iff its mean regret <= the active one's x
  (1 + :data:`TOLERANCE`) - the boundary itself passes, like D306.

Pure (the store reads the inputs and runs this in a worker thread).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from meshforge.controller.planner.calibration import Calibration
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.heldout import CalibrationChoice
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    EvalConfig,
    EvalGroup,
    EvalRecord,
)

__all__ = ["MIN_GROUPS", "TOLERANCE", "RegretCheck", "regret_check", "regret_gate"]

MIN_GROUPS: Final = 10
TOLERANCE: Final = 0.05
_CONFIG: Final = EvalConfig(bootstrap_resamples=1)
"""Means only: the bootstrap is not needed for the gate."""

type CheckStatus = Literal["passed", "rejected", "skipped"]


@dataclass(frozen=True, slots=True)
class RegretCheck:
    status: CheckStatus
    groups: int
    """Groups counted under both versions."""
    regret_new: float | None
    regret_active: float | None
    reason: str | None
    """``rejected``: why; ``skipped``: ``regret check skipped (too_few_groups ...)``."""

    def fit_inputs(self) -> dict[str, Any]:
        """The keys stored in ``planner_calibration.fit_inputs`` (D319: no new
        column)."""
        return {
            "regret_check": self.status,
            "regret_groups": self.groups,
            "holdout_regret_new": self.regret_new,
            "holdout_regret_active": self.regret_active,
        }


def regret_gate(
    new_mean: float | None,
    active_mean: float | None,
    groups: int,
    *,
    min_groups: int = MIN_GROUPS,
) -> RegretCheck:
    """The D319 comparison itself (module docstring)."""
    if groups < min_groups or new_mean is None or active_mean is None:
        return RegretCheck(
            "skipped",
            groups,
            new_mean,
            active_mean,
            f"regret check skipped (too_few_groups: {groups} < {min_groups})",
        )
    # Lead fix (tester r2 T4): Decimal raises on NaN; a non-finite mean never
    # passes - it is rejected (unreachable today: T3 keeps non-finite medians
    # out of the regrets).
    if not (math.isfinite(new_mean) and math.isfinite(active_mean)):
        return RegretCheck(
            "rejected",
            groups,
            new_mean,
            active_mean,
            f"holdout_regret not finite (new {new_mean}, active {active_mean})",
        )
    # G4 fix round 1 (T1): compare the decimal values exactly - binary floating
    # point rounds the product below it for some values (1.14 x 1.05 =
    # 1.1969999999999998), which rejected a version exactly x 1.05 the active
    # one. ``str`` of a float is its shortest round-trip decimal.
    limit = Decimal(str(active_mean)) * (1 + Decimal(str(TOLERANCE)))
    if Decimal(str(new_mean)) <= limit:
        return RegretCheck("passed", groups, new_mean, active_mean, None)
    return RegretCheck(
        "rejected",
        groups,
        new_mean,
        active_mean,
        f"holdout_regret {new_mean:.6f} % > {1.0 + TOLERANCE:.2f} x active "
        f"{active_mean:.6f} % on {groups} labelled groups",
    )


def _regrets(report: Mapping[str, Any]) -> dict[tuple[str, str], float]:
    key = CRITERION_POLICY.key
    out: dict[tuple[str, str], float] = {}
    for group in report["groups"]:
        for pick in group["picks"]:
            if pick["key"] == key and pick["status"] == "counted":
                out[(group["campaign_id"], group["group_id"])] = float(
                    pick["regret_pct"]
                )
    return out


def _choices(
    groups: Sequence[EvalGroup], calibration: Calibration | None
) -> dict[str, CalibrationChoice]:
    return {
        family: CalibrationChoice(
            family=family,
            calibration=calibration,
            source="active" if calibration is not None else "none",
            version=None if calibration is None else calibration.version,
            fitted_runs=0,
            excluded_runs=0,
        )
        for family in sorted({g.family for g in groups})
    }


def regret_check(
    groups: Sequence[EvalGroup],
    records: Sequence[EvalRecord],
    new: Calibration,
    active: Calibration | None,
    *,
    min_groups: int = MIN_GROUPS,
) -> RegretCheck:
    """Evaluate ``new`` vs ``active`` on the slice's groups (module
    docstring)."""
    campaigns = sorted({g.campaign_id for g in groups})
    reports = [
        evaluate(
            groups,
            records,
            _choices(groups, calibration),
            (CRITERION_POLICY,),
            _CONFIG,
            campaign_ids=campaigns,
        )
        for calibration in (new, active)
    ]
    new_regret, active_regret = (_regrets(r) for r in reports)
    common = sorted(new_regret.keys() & active_regret.keys())
    if not common:
        return regret_gate(None, None, 0, min_groups=min_groups)
    mean_new = math.fsum(new_regret[k] for k in common) / len(common)
    mean_active = math.fsum(active_regret[k] for k in common) / len(common)
    return regret_gate(
        round(mean_new, 6), round(mean_active, 6), len(common), min_groups=min_groups
    )
