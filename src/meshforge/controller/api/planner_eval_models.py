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
  oracle in >= 80 % of counted groups) and criterion 2 (**paired**, D322: for
  every rule policy the bootstrap CI of the per-group regret difference
  ``rule - cost_model`` over the groups counted for both lies entirely above
  0; each comparison is in ``comparisons``); ``too_few_groups`` below
  ``min_groups`` counted (criterion 2: common) groups, never ``PASS``;
  ``leaked`` when the report was made with ``allow_leak`` (``would_be`` then
  says what it would have been).
* ``latency`` (V0.14 G6, D324) - the stored decision times of the
  campaigns' dispatched decisions; criterion 3's verdict (p99 of snapshots
  of >= 4 nodes strictly below the budget; ``too_few_decisions`` below 30).
* ``calibrations`` - per family, the calibration the calibrated rows used: a
  leave-one-family-out refit (D316, ``refit``), or the live active version
  (``active``, only with ``allow_leak``), or ``none``.
* ``groups`` - every group with its label, configurations (median total ms),
  and per policy its pick, status and regret.

* ``ml`` (V0.15 G5a, D368-D370, ``eval_version`` 2; additive - null in a
  version 1 report) - the ``cost_model:ml`` rows' story: criterion 1
  (paired bootstrap CI of ``regret(ML) - regret(calibrated)`` per common
  group) and criterion 2 (per family with >= 3 common groups, ML within
  calibrated + 5 percentage points), the fold models (one per held-out
  family, trained without it, never registered), what ML did in the chosen
  plans (used / why not, per family) and the fold models' held-out
  prediction error per term. ``ml_unavailable`` when the controller has no
  ``ml`` extra or ``ml = off`` (never ``FAIL``).

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
    "EvalMlOut",
    "EvalReportListOut",
    "EvalReportOut",
    "EvalReportRecordOut",
    "EvalReportSummaryOut",
    "EvalRequestIn",
    "EvalSettingsOut",
    "FamilyResultOut",
    "GroupPickOut",
    "GroupResultOut",
    "LatencyBucketOut",
    "LatencyOut",
    "MlEvalSettingsOut",
    "MlFallbackCountsOut",
    "MlFallbackOut",
    "MlFamilyFallbackOut",
    "MlFamilyVerdictOut",
    "MlFoldOut",
    "MlTermMetricsOut",
    "MlVerdictOut",
    "PairedComparisonOut",
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
    "PASS", "FAIL", "not_separable", "too_few_groups", "too_few_decisions", "leaked"
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


class PairedComparisonOut(ApiModel):
    """Criterion 2 (D322): one rule policy vs calibrated ``cost_model`` on the
    groups counted for both."""

    key: str
    """The rule policy."""
    common_groups: int
    mean_diff_pct: float | None
    """Mean of ``regret(rule) - regret(cost_model)`` (% points); > 0 = the
    criterion policy picked better."""
    ci_low: float | None
    ci_high: float | None
    """Bootstrap CI of that mean (None below ``min_groups`` common groups)."""
    unmeasured_groups: int
    """Groups where the rule policy's pick was never measured (not paired)."""


class VerdictOut(ApiModel):
    criterion: Literal[1, 2, 3]
    """V0.14 G6 (D324): criterion 3 = decision latency (``latency``)."""
    status: VerdictStatus
    """``too_few_decisions``: criterion 3 only."""
    would_be: VerdictStatus | None
    """With ``leaked``: the verdict the numbers would give (never a claim)."""
    value: float | None
    """Criterion 1: % within the threshold; criterion 2: the lowest
    difference-CI lower bound over the rule policies (% points); criterion
    3: the p99 decision time (ms) of ``latency.judged``."""
    threshold: float | None
    """Criterion 1: the target %; criterion 2: 0; criterion 3: the budget
    (ms, PASS only strictly below it)."""
    counted_groups: int
    """Criterion 2: the fewest common groups of any comparison; criterion 3:
    the decisions judged (not groups)."""
    detail: str
    comparisons: list[PairedComparisonOut] = Field(default_factory=list)
    """Criterion 2 only (D322); empty for criterion 1."""


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
    """``no_replay`` / ``replay_unreadable`` / ``no_shadow`` /
    ``replay_unplannable`` -> records."""
    non_finite_configs: int = 0
    """Configurations with a non-finite median; their groups are excluded
    (G4 fix round 1, T3)."""


class LatencyBucketOut(ApiModel):
    """Decisions of one snapshot size (V0.14 G6, D324)."""

    decisions: int
    p99_ms: float | None


class LatencyOut(ApiModel):
    """Decision latency of the evaluated campaigns' ``dispatched`` decisions,
    from ``plan_decision.decision_ms`` (V0.14 G6, D324; criterion 3).
    Percentiles are nearest-rank (exact; no interpolation)."""

    decisions: int
    """Decisions with a stored time."""
    without_latency: int
    """Decisions without one (rows written before migration 0033): excluded,
    never counted as 0."""
    p50_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    over_budget: int
    """Decisions slower than ``budget_ms`` (the dispatcher's own rule)."""
    budget_ms: float
    """``[controller.planner] decision_latency_budget_ms``."""
    node_count: int
    """Criterion 3 judges snapshots of at least this many live nodes (4: the
    lab laptops; D332)."""
    min_decisions: int
    """Fewer judged decisions -> ``too_few_decisions``."""
    judged_on: Literal["snapshot_nodes", "live_nodes"] = "snapshot_nodes"
    """What ``judged`` counts: ``live_nodes`` (V0.14 G6F, D332) = snapshot
    nodes online, connected and accepting jobs at decision time;
    ``snapshot_nodes`` = every node row (a report stored before D332, which
    lacks the key)."""
    by_node_count: dict[str, LatencyBucketOut]
    """Snapshot node rows - offline and revoked ones too - (``"unknown"`` =
    unreadable) -> its decisions."""
    by_live_node_count: dict[str, LatencyBucketOut] | None = None
    """D332: live snapshot nodes (``"unknown"`` = a decision without a
    replay) -> its decisions; None in a report stored before D332."""
    judged: LatencyBucketOut
    """The decisions criterion 3 judges (snapshots of >= ``node_count``
    nodes, counted as ``judged_on`` says) and their p99."""


type MlVerdictStatus = Literal[
    "PASS", "FAIL", "not_separable", "too_few_groups", "leaked", "ml_unavailable"
]


class MlFamilyVerdictOut(ApiModel):
    """One family of ML criterion 2 (V0.15 D370)."""

    family: str
    common_groups: int
    mean_regret_ml_pct: float
    mean_regret_calibrated_pct: float
    diff_pct: float
    """ML minus calibrated, in percentage points."""
    status: Literal["pass", "fail", "too_few_groups"]
    """``too_few_groups`` = fewer than ``min_family_groups``: listed, never a
    pass."""


class MlVerdictOut(ApiModel):
    """``ml_criterion_1`` (paired bootstrap of ``regret(ML) -
    regret(calibrated)``) or ``ml_criterion_2`` (per family, absolute
    percentage-point tolerance) - V0.15 D370."""

    name: Literal["ml_criterion_1", "ml_criterion_2"]
    status: MlVerdictStatus
    would_be: str | None
    """``leaked`` reports: the status it would have had."""
    detail: str
    common_groups: int
    mean_diff_pct: float | None
    """Criterion 1: mean of ML minus calibrated regret (% points)."""
    ci_low: float | None
    ci_high: float | None
    threshold: float | None
    """Criterion 1: 0; criterion 2: the tolerance in % points."""
    families: list[MlFamilyVerdictOut]
    """Criterion 2 only."""


class MlEvalSettingsOut(ApiModel):
    criterion_policy: str
    compared_with: str
    family_tolerance_pp: float
    min_family_groups: int
    min_groups: int


class MlFallbackCountsOut(ApiModel):
    """What ML did in the criterion ML row's decisions (V0.15 D369)."""

    decisions: int
    no_fold_model: int
    """Planned without a model (the family's fold has none)."""
    predictor_error: int
    """The model failed; the decision fell back to calibration (D363)."""
    terms: dict[str, int]
    """Learnable terms of the chosen plans: ``used`` or why ML was not used
    (``ood``, ``interval_too_wide``, ``no_model``, ...)."""
    used_rate: float | None


class MlFamilyFallbackOut(MlFallbackCountsOut):
    family: str


class MlFallbackOut(ApiModel):
    total: MlFallbackCountsOut
    families: list[MlFamilyFallbackOut]


class MlFoldOut(ApiModel):
    """The model one held-out family's ML rows planned with (V0.15 D368):
    trained by the production pipeline on every other family, never
    registered (``source`` ``fold``); ``active`` = the serving set
    (``allow_leak`` only); ``none`` = no usable model (``reason``)."""

    family: str
    source: Literal["fold", "active", "none"]
    set_version: str | None
    reason: str | None
    train_rows: int | None
    trained_families: list[str]
    """The families its training rows came from (never ``family``)."""
    excluded_rows: int
    learned_terms: list[str]
    until: str | None
    """Rows that finished after this were not trained on (ISO time)."""


class MlTermMetricsOut(ApiModel):
    """The fold models' prediction error on their held-out family's rows,
    one term (V0.15 D370; log error MALE, MAPE on seconds)."""

    rows: int
    families: int
    male_model: float | None
    male_raw: float | None
    mape_model: float | None
    mape_raw: float | None
    median_ape_model: float | None
    coverage: float | None
    ood_rate: float | None
    usable_rate: float | None


class EvalMlOut(ApiModel):
    """The report's ``ml`` block (V0.15 G5a, ``eval_version`` 2)."""

    status: str
    """``evaluated`` | ``ml_unavailable``."""
    reason: str | None
    settings: MlEvalSettingsOut
    verdicts: list[MlVerdictOut]
    fallback: MlFallbackOut
    folds: list[MlFoldOut]
    terms: dict[str, MlTermMetricsOut]


class EvalReportOut(ApiModel):
    """``planner_eval_report.report`` (``eval_version`` 1 or 2)."""

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
    latency: LatencyOut | None = None
    """V0.14 G6 (D324, additive - ``eval_version`` stays 1): null in a report
    stored before G6."""
    ml: EvalMlOut | None = None
    """V0.15 G5a (D369/D370, ``eval_version`` 2): null in a version 1
    report."""


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
