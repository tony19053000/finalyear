"""Response / request models of ``/v1/planner/eval-reports`` (V0.14 G4, D297/D313-D318).

**The contract the dashboard's Planner page builds on (D318).** G4 commits
these models first so G5 can build against them in parallel; a change after
that is a contract change (bump ``eval_version`` and say so in the design
change log).

An **evaluation report** (``eval_version`` 1, canonical JSON, stored in
``planner_eval_report.report``) answers, for one or more benchmark campaigns:
*for each labelled group, which configuration would each policy have picked,
and how much slower is that than the best measured configuration?*

* ``policies`` - one row per evaluated policy (``key``): every rule policy,
  ``cost_model`` x {uncalibrated, calibrated} x each objective, and the
  evaluation-only ``oracle`` (D298, regret 0 by construction). Mean relative
  regret (%) with a seeded bootstrap 95 % CI over groups, mean absolute regret
  (ms), and the share of counted groups within ``regret_threshold_pct``.
* ``families`` - the same per workload family (no CI).
* ``verdicts`` - criterion 1 (calibrated ``cost_model`` within 15 % of the
  oracle in >= 80 % of counted groups) and criterion 2 (its CI entirely below
  every rule policy's); ``too_few_groups`` below ``min_groups`` counted
  groups, never ``PASS``; ``leaked`` when the report was made with
  ``allow_leak`` (``would_be`` then says what it would have been).
* ``calibrations`` - per family, the calibration the calibrated rows used: a
  leave-one-family-out refit (D316, ``refit``), or the live active version
  (``active``, only with ``allow_leak``), or ``none``.
* ``groups`` - every group with its label, configurations (median total ms),
  and per policy its pick, status and regret.

Regret of a group (D315) = ``(median(picked) - median(best)) / median(best)``
in %, 0 when the pick is the best or in the label's tie set. A pick no
configuration of the campaign measured is ``unmeasured`` - reported, never
counted as 0 (D297). Groups labelled ``insufficient`` / ``pending`` and groups
without a usable replay (``no_replay``) are reported, not counted.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from meshforge.controller.api.models import ApiModel, Utc

__all__ = [
    "MAX_EVAL_CAMPAIGNS",
    "MAX_FAMILY_CHARS",
    "BootstrapCIOut",
    "CalibrationUsedOut",
    "EvalConfigOut",
    "EvalCountsOut",
    "EvalReportListOut",
    "EvalReportOut",
    "EvalReportRecordOut",
    "EvalReportSummaryOut",
    "EvalRequestIn",
    "EvalSettingsOut",
    "FamilyResultOut",
    "GroupPickOut",
    "GroupResultOut",
    "PolicyResultOut",
    "VerdictOut",
]

MAX_EVAL_CAMPAIGNS: Final = 20
"""Most campaigns one report may evaluate."""
MAX_FAMILY_CHARS: Final = 64
"""A workload family name's length bound (``bench_run.family``)."""
_ID_MAX: Final = 64

type PolicyKind = Literal["rule", "cost_model", "oracle"]
type VerdictStatus = Literal[
    "PASS", "FAIL", "not_separable", "too_few_groups", "leaked"
]
type PickStatus = Literal["counted", "unmeasured", "no_replay"]
type GroupStatus = Literal["evaluated", "excluded", "no_replay"]


class EvalSettingsOut(ApiModel):
    """The fixed numbers a report was computed with (all in the report, so
    it can be recomputed byte for byte)."""

    regret_threshold_pct: float
    """Criterion 1's "within X % of the oracle" (15)."""
    within_target_pct: float
    """Criterion 1 passes at this share of counted groups (80)."""
    min_groups: int
    """Fewer counted groups -> ``too_few_groups`` (10)."""
    bootstrap_resamples: int
    bootstrap_seed: int
    ci_level: float
    criterion_policy: str
    """The policy row the criteria judge (``cost_model:calibrated:min_time``)."""


class BootstrapCIOut(ApiModel):
    """Percentile bootstrap CI of a mean (None = no counted group)."""

    low: float | None
    high: float | None


class PolicyResultOut(ApiModel):
    key: str
    """``random`` ... ``cache_affinity``, ``cost_model:<uncalibrated|calibrated>:
    <objective>``, ``oracle``."""
    policy: str
    kind: PolicyKind
    calibrated: bool | None
    """``cost_model`` rows only."""
    objective: str | None
    """``cost_model`` rows only (rule policies ignore objectives, D295)."""
    counted_groups: int
    unmeasured_groups: int
    no_replay_groups: int
    mean_regret_pct: float | None
    ci_pct: BootstrapCIOut
    mean_regret_ms: float | None
    within_threshold_pct: float | None
    """% of counted groups with regret <= ``regret_threshold_pct``."""
    zero_regret_groups: int


class FamilyResultOut(ApiModel):
    family: str
    key: str
    counted_groups: int
    unmeasured_groups: int
    no_replay_groups: int
    mean_regret_pct: float | None
    mean_regret_ms: float | None
    within_threshold_pct: float | None


class VerdictOut(ApiModel):
    criterion: Literal[1, 2]
    status: VerdictStatus
    would_be: VerdictStatus | None
    """With ``leaked``: the verdict the numbers would give (never a claim)."""
    value: float | None
    """Criterion 1: % within the threshold; criterion 2: the criterion
    policy's CI upper bound (%)."""
    threshold: float | None
    """Criterion 1: the target %; criterion 2: the lowest rule-policy CI
    lower bound (%)."""
    counted_groups: int
    detail: str


class CalibrationUsedOut(ApiModel):
    family: str
    source: Literal["refit", "active", "none"]
    version: int | None
    """``active``: the live version; ``refit``: None (fitted, not stored)."""
    fitted_runs: int
    """Matched runs the refit used."""
    excluded_runs: int
    """Matched runs the held-out filter removed (this family / its workloads)."""
    factors: int
    digest: str | None
    """sha256 of the factors' canonical JSON (None = no factor)."""


class EvalConfigOut(ApiModel):
    """One measured (or unsupported) configuration of a group."""

    config_id: str
    strategy: str
    node_id: str
    origin_node_id: str
    node_set: list[str]
    """``mode3`` only (sorted); empty otherwise."""
    state: Literal["measured", "unsupported"]
    median_ms: float | None
    repetitions: int


class GroupPickOut(ApiModel):
    key: str
    status: PickStatus
    config_id: str | None
    """The group's pick (D313: the most frequent over its records' replays)."""
    regret_pct: float | None
    regret_ms: float | None
    votes: dict[str, int]
    """config id (or ``unmeasured``) -> records whose replay picked it."""


class GroupResultOut(ApiModel):
    campaign_id: str
    group_id: str
    family: str
    condition: str
    label: Literal["best", "tie", "insufficient", "pending"]
    best_config_id: str | None
    tied_config_ids: list[str]
    best_median_ms: float | None
    status: GroupStatus
    excluded_reason: str | None
    records: int
    records_used: int
    """Records whose replay could be planned (D313)."""
    configs: list[EvalConfigOut]
    picks: list[GroupPickOut]
    """Empty unless ``status`` is ``evaluated``."""


class EvalCountsOut(ApiModel):
    groups: int
    evaluated_groups: int
    """``best`` / ``tie`` groups with at least one usable replay."""
    labels: dict[str, int]
    """``best`` / ``tie`` / ``insufficient`` / ``pending`` -> groups."""
    no_replay_groups: int
    records: int
    records_used: int
    records_skipped: dict[str, int]
    """``no_replay`` / ``replay_unreadable`` / ``no_shadow`` -> records."""


class EvalReportOut(ApiModel):
    """``planner_eval_report.report`` (``eval_version`` 1)."""

    eval_version: int
    campaign_ids: list[str]
    holdout: str | None
    """Only this family was evaluated (``--holdout``); None = every family,
    each with its own leave-one-family-out calibration."""
    leaked: bool
    """True = made with ``allow_leak``: the calibrated rows used the live
    calibration, which may have seen these runs. Every verdict is
    ``leaked``."""
    settings: EvalSettingsOut
    counts: EvalCountsOut
    verdicts: list[VerdictOut]
    policies: list[PolicyResultOut]
    families: list[FamilyResultOut]
    calibrations: list[CalibrationUsedOut]
    groups: list[GroupResultOut]


class EvalReportSummaryOut(ApiModel):
    """One stored report in the list (no groups)."""

    id: int
    created_at: Utc
    campaign_ids: list[str]
    holdout: str | None
    leaked: bool
    eval_version: int
    evaluated_groups: int
    verdicts: list[VerdictOut]


class EvalReportListOut(ApiModel):
    reports: list[EvalReportSummaryOut]
    """Newest first."""


class EvalReportRecordOut(ApiModel):
    """One stored report (``GET /v1/planner/eval-reports/{id}``, and what
    ``POST`` answers)."""

    id: int
    created_at: Utc
    campaign_ids: list[str]
    holdout: str | None
    leaked: bool
    report: EvalReportOut


class EvalRequestIn(ApiModel):
    """``POST /v1/planner/eval-reports`` (admin): evaluate and store."""

    campaign_ids: list[Annotated[str, Field(min_length=1, max_length=_ID_MAX)]] = Field(
        min_length=1,
        max_length=MAX_EVAL_CAMPAIGNS,
        description="The benchmark campaigns to evaluate (their labelled groups).",
    )
    holdout: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_FAMILY_CHARS,
        description="Evaluate only this workload family (held out of the "
        "calibration refit).",
    )
    allow_leak: bool = Field(
        default=False,
        description="Debugging only: use the live calibration instead of "
        "held-out refits; every verdict is stamped 'leaked'.",
    )
