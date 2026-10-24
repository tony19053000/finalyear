"""The offline evaluator's pure core (V0.14 G4, D297/D313-D317; ADR-0020 decision 6).

``evaluate(groups, records, calibrations, policies, config, ...)`` -> the
report (canonical JSON shape of
:class:`~meshforge.controller.api.planner_eval_models.EvalReportOut`). No I/O,
no clock, no logging (an AST test pins the imports): the same inputs give the
same report, byte for byte (:func:`.report.report_text`).

**Per group** (a campaign's ``group_id``: one workload x parameters x
condition, measured under several configurations):

1. Only ``best`` and ``tie`` groups are evaluated; ``insufficient`` and
   ``pending`` ones are reported with their reason and not counted (D297).
2. Every kept record of the group (a label row) has its decision's replay
   (D292). For a campaign's forced run that is the **shadow** request - the
   same job unpinned and unforced, on the idle snapshot (D215/D235) - which is
   exactly "what would the planner have chosen here". A record without a
   replay, with an unreadable one, or forced without a shadow, is skipped and
   counted (``records_skipped``); a group with no usable record is
   ``no_replay`` - reported, not counted (D313).
3. For each policy, every usable record's replay is planned
   (:func:`~meshforge.controller.planner.plan.plan`, pure) and its pick mapped
   to a configuration (:func:`.mapping.config_for`, D314). The group's pick is
   the most frequent; ties go to the smallest ``config_id`` (the labels' own
   rule), **except** that ``unmeasured`` wins a tie it is in (a policy that
   picks something unmeasured as often as a measured configuration is not
   credited with the measured one). ``random`` keeps its stored seed
   (``job_id`` + attempt number in the request), ``round_robin`` its stored
   cursor (in the snapshot).
4. ``oracle`` (D298) picks the best configuration (``best``) or the lowest
   median of the tie (``tie``, ties by ``config_id``): regret 0 by
   construction - the reference row.
5. **Regret** (D315) = ``(median(pick) - median(best)) / median(best)`` x 100,
   and ``median(pick) - median(best)`` ms; **0** when the pick is the best or
   in the tie set. ``median(best)`` = the lowest median of the group's
   measured configurations. An ``unmeasured`` pick is never counted. Regrets
   are rounded to 6 decimals (the reported precision) before any comparison
   with the threshold (G4 fix round 1, T2). A group with a non-finite median
   (never produced by the labels; a guard) is excluded with the reason and
   counted in ``counts.non_finite_configs`` (T3) - it never reaches the JSON.

**Per policy**: mean relative regret over its counted groups with a seeded
bootstrap CI (:func:`meshforge.bench.stats.bootstrap_mean_ci`, D317), mean
absolute regret, the share within ``regret_threshold_pct``; per family the
same without CI.

**Verdicts** (D317), on :data:`.model.CRITERION_POLICY` (calibrated
``cost_model``, ``min_time``):

* criterion 1 - ``PASS`` iff at least ``within_target_pct`` % of its counted
  groups are within ``regret_threshold_pct`` %; else ``FAIL``;
* criterion 2 - **paired** (D322): for each rule policy, the per-group
  regret difference ``rule - calibrated cost_model`` over the groups counted
  for **both**, with a seeded bootstrap CI of its mean. ``PASS`` iff every
  difference CI lies entirely above 0; ``FAIL`` when some difference CI lies
  entirely below 0 (that rule policy is better); otherwise
  ``not_separable``. Pairing removes selection bias: a policy whose picks are
  ``unmeasured`` on the hard groups would otherwise look better on the easy
  ones it kept. The verdict lists every comparison with the rule policy's
  ``unmeasured`` count; the unpaired per-policy means stay for display;
* fewer than ``min_groups`` counted groups (criterion 2: common groups in any
  comparison) -> ``too_few_groups``, never ``PASS``;
* a ``leaked`` report (``allow_leak``) -> every verdict ``leaked``, with
  ``would_be`` the verdict above (D299: never a claim on training data).
* criterion 3 (V0.14 G6, D324) - decision latency p99 at the lab's fabric
  size from the stored ``decision_ms`` of the campaigns' dispatched decisions
  (:mod:`.latency`); ``too_few_decisions`` below its minimum, never ``PASS``.

**ML rows and verdicts** (V0.15 G5a, D368-D370; ``eval_version`` 2). With an
evaluated :class:`~.model.EvalMl`, the ``cost_model:ml`` rows (one per
objective) re-plan every record with the family's held-out calibration
**and** its fold model (``plan(..., calibration=, ml=)``, the D336
precedence); a family whose fold has no usable model plans with calibration
only. Per family the criterion ML row's decisions are counted: those without
a fold model (``no_fold_model``), those whose predictor failed
(``predictor_error``, D363), and per learnable term of the **chosen**
candidate whether ML was used or why not (``detail.ml.reason``). The ``ml``
block's verdicts, on :data:`~.model.ML_CRITERION_POLICY` vs
:data:`~.model.CRITERION_POLICY`:

* ``ml_criterion_1`` - per group counted under both, ``d = regret(ML) -
  regret(calibrated)``; seeded bootstrap CI of the mean ``d``: ``PASS`` iff
  its upper end < 0, ``FAIL`` iff its lower end > 0, else
  ``not_separable``; fewer than ``min_groups`` common groups ->
  ``too_few_groups``;
* ``ml_criterion_2`` - per family with at least ``ml_min_family_groups``
  common groups: ML mean regret <= calibrated mean regret +
  ``ml_family_tolerance_pp`` **percentage points** (an absolute margin, not
  a ratio). ``PASS`` only if every such family passes; smaller families are
  listed ``too_few_groups`` and never counted as passes; no family large
  enough -> ``too_few_groups``;
* a ``leaked`` report marks both ``leaked`` (``would_be`` as above); without
  an evaluated ML input both are its status (``ml_unavailable``), never
  ``FAIL``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from meshforge.bench.stats import bootstrap_mean_ci
from meshforge.controller.planner.calibration import Calibration
from meshforge.controller.planner.plan import plan
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.planner.replay import (
    ReplayError,
    from_replay,
    shadow_from_replay,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Decision,
    Objective,
    PlanRequest,
)
from meshforge.controller.planner_eval.heldout import (
    CalibrationChoice,
    calibration_digest,
)
from meshforge.controller.planner_eval.latency import (
    LatencySample,
    latency_block,
    latency_verdict,
)
from meshforge.controller.planner_eval.mapping import config_for, pick_of
from meshforge.controller.planner_eval.model import (
    COST_MODEL,
    CRITERION_POLICY,
    EVAL_VERSION,
    ML_CRITERION_POLICY,
    ML_UNAVAILABLE,
    ORACLE,
    RULE_POLICIES,
    UNMEASURED,
    ConfigRef,
    EvalConfig,
    EvalGroup,
    EvalMl,
    EvalRecord,
    PolicySpec,
)

__all__ = [
    "ML_VERDICTS",
    "SKIP_REASONS",
    "GroupPick",
    "PlannedInputs",
    "best_median",
    "evaluate",
    "group_pick",
    "group_regret",
    "ml_fallback",
    "oracle_pick",
    "planned_inputs",
    "vote",
]

SKIP_REASONS: Final = (
    "no_replay",
    "replay_unreadable",
    "no_shadow",
    "replay_unplannable",
)
"""Why a record's replay could not be used (``counts.records_skipped``)."""
EVALUATED_LABELS: Final = frozenset({"best", "tie"})
_ROUND: Final = 6
ML_VERDICTS: Final = ("ml_criterion_1", "ml_criterion_2")
NO_FOLD_MODEL: Final = "no_fold_model"
PREDICTOR_ERROR: Final = "predictor_error"

type PlannedInputs = tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, _ROUND)


# ------------------------------------------------------------ one record


def planned_inputs(record: EvalRecord) -> PlannedInputs | str:
    """The inputs to plan this record's situation with, or why not
    (:data:`SKIP_REASONS`): the shadow request when the replay has one, else
    the decision's own request - unless that one is pinned or forced (then it
    is not a preference, D215)."""
    if record.replay is None:
        return "no_replay"
    try:
        shadow = shadow_from_replay(record.replay)
        if shadow is not None:
            return shadow
        request, snapshot, now = from_replay(record.replay)
    except ReplayError:
        return "replay_unreadable"
    if request.pinned_node_id is not None or request.node_set:
        return "no_shadow"
    return request, snapshot, now


def _calibration(
    spec: PolicySpec, choice: CalibrationChoice | None
) -> Calibration | None:
    if spec.policy != COST_MODEL or not spec.calibrated or choice is None:
        return None
    return choice.calibration


def ml_fallback(decision: Decision) -> Counter[str]:
    """V0.15 G5a (D369): what ML did in one decision - ``decisions`` 1, plus
    ``no_fold_model`` (planned without a model) or ``predictor_error`` (the
    model failed, D363), plus one count per learnable term of the **chosen**
    candidate: ``used`` or its ``detail.ml.reason`` (``ood``,
    ``interval_too_wide``, ``no_model``, ...). A Mode 3 compute term's
    nested ``ml.rate`` block describes the same correction and is not
    counted again."""
    out: Counter[str] = Counter(decisions=1)
    if decision.ml is None:
        out[NO_FOLD_MODEL] += 1
        return out
    if decision.ml.error is not None:
        out[PREDICTOR_ERROR] += 1
    chosen = decision.chosen
    if chosen is None or chosen.estimate is None:
        return out
    for term in chosen.estimate.terms().values():
        block = term.detail.get("ml") if isinstance(term.detail, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        if block.get("used") is True:
            out["term:used"] += 1
        else:
            out[f"term:{block.get('reason') or 'unknown'}"] += 1
    return out


def _votes_of(
    inputs: PlannedInputs,
    origin: str,
    configs: Sequence[ConfigRef],
    policies: Sequence[PolicySpec],
    choice: CalibrationChoice | None,
    ml: MlInput | None = None,
    ml_counts: Counter[str] | None = None,
) -> dict[str, str]:
    """policy key -> the configuration id (or ``unmeasured``) this record's
    replay picks under each policy (the oracle is not planned). ML rows
    (V0.15 G5a) plan with ``ml`` (the family's fold model) on top of the
    calibration; the criterion ML row's decision is counted into
    ``ml_counts`` (:func:`ml_fallback`)."""
    request, snapshot, now = inputs
    out: dict[str, str] = {}
    for spec in policies:
        if spec.policy == ORACLE:
            continue
        asked = request
        if spec.policy == COST_MODEL:
            asked = dataclasses.replace(
                request, objective=cast("Objective | None", spec.objective)
            )
        decision = plan(
            asked,
            snapshot,
            spec.policy,
            now,
            calibration=_calibration(spec, choice),
            ml=ml if spec.ml else None,
        )
        if spec.ml and ml_counts is not None and spec.key == ML_CRITERION_POLICY.key:
            ml_counts.update(ml_fallback(decision))
        found = config_for(pick_of(decision), origin, configs)
        out[spec.key] = UNMEASURED if found is None else found.config_id
    return out


# ------------------------------------------------------------ one group


def vote(votes: Mapping[str, int]) -> str | None:
    """The group's pick from its records' votes (module docstring, item 3);
    None = no vote."""
    if not votes:
        return None
    top = max(votes.values())
    tied = sorted(k for k, n in votes.items() if n == top)
    if UNMEASURED in tied:
        return UNMEASURED
    return tied[0]


@dataclass(frozen=True, slots=True)
class GroupPick:
    key: str
    status: str
    """``counted`` | ``unmeasured`` | ``no_replay``."""
    config_id: str | None
    regret_pct: float | None
    regret_ms: float | None
    votes: Mapping[str, int]


def _non_finite(group: EvalGroup) -> list[str]:
    return sorted(
        c.config_id
        for c in group.configs
        if c.median_ms is not None and not math.isfinite(c.median_ms)
    )


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def best_median(configs: Sequence[ConfigRef]) -> float | None:
    """The lowest median of the measured configurations (regret's
    reference, D315); None = none measured."""
    medians = [
        c.median_ms
        for c in configs
        if c.state == "measured" and c.median_ms is not None
    ]
    return min(medians) if medians else None


def oracle_pick(group: EvalGroup) -> str | None:
    """The oracle's configuration (D298): the labelled best, or the tie's
    lowest median (ties by ``config_id``)."""
    if group.label == "best":
        return group.best_config_id
    measured = [
        c for c in group.configs if c.state == "measured" and c.median_ms is not None
    ]
    if not measured:
        return None
    return min(measured, key=lambda c: (c.median_ms, c.config_id)).config_id


def group_regret(
    group: EvalGroup, config_id: str, best_ms: float
) -> tuple[float, float] | None:
    """``(regret %, regret ms)`` of picking ``config_id`` (D315); None = not a
    measured configuration of the group."""
    if config_id == group.best_config_id or config_id in group.tied_config_ids:
        return 0.0, 0.0
    by_id = {c.config_id: c for c in group.configs}
    config = by_id.get(config_id)
    if config is None or config.state != "measured" or config.median_ms is None:
        return None
    delta = config.median_ms - best_ms
    # G4 fix round 1 (T2): the reported precision decides, not float noise
    return round(delta / best_ms * 100.0, _ROUND), round(delta, _ROUND)


def group_pick(
    group: EvalGroup, key: str, votes: Mapping[str, int], best_ms: float
) -> GroupPick:
    picked = vote(votes)
    if picked is None:
        return GroupPick(key, "no_replay", None, None, None, dict(votes))
    if picked == UNMEASURED:
        return GroupPick(key, "unmeasured", None, None, None, dict(votes))
    regret = group_regret(group, picked, best_ms)
    if regret is None:
        return GroupPick(key, "unmeasured", picked, None, None, dict(votes))
    return GroupPick(key, "counted", picked, regret[0], regret[1], dict(votes))


@dataclass(frozen=True, slots=True)
class _GroupResult:
    group: EvalGroup
    status: str
    excluded_reason: str | None
    best_ms: float | None
    records: int
    records_used: int
    picks: tuple[GroupPick, ...]
    ml_counts: Counter[str] = dataclasses.field(default_factory=Counter)
    """V0.15 G5a: :func:`ml_fallback` summed over the used records."""


def _origin(record: EvalRecord, configs: Mapping[str, ConfigRef]) -> str | None:
    own = configs.get(record.config_id)
    return None if own is None else own.origin_node_id


def _evaluate_group(
    group: EvalGroup,
    records: Sequence[EvalRecord],
    policies: Sequence[PolicySpec],
    choice: CalibrationChoice | None,
    skipped: Counter[str],
    ml: MlInput | None = None,
) -> _GroupResult:
    counts: Counter[str] = Counter()

    def result(
        status: str,
        reason: str | None,
        best: float | None = None,
        used: int = 0,
        picks: tuple[GroupPick, ...] = (),
    ) -> _GroupResult:
        return _GroupResult(
            group, status, reason, best, len(records), used, picks, counts
        )

    if group.label not in EVALUATED_LABELS:
        return result("excluded", f"{group.label}: {group.reason or 'not labelled'}")
    odd = _non_finite(group)
    if odd:  # G4 fix round 1 (T3): never planned, never written as NaN
        skipped["non_finite_configs"] += len(odd)
        return result("excluded", "non-finite median: " + ", ".join(odd[:5]))
    best_ms = best_median(group.configs)
    if best_ms is None or not math.isfinite(best_ms) or best_ms <= 0.0:
        return result("excluded", "no positive median to compare with")
    by_id = {c.config_id: c for c in group.configs}
    votes: dict[str, Counter[str]] = {spec.key: Counter() for spec in policies}
    used = 0
    for record in records:
        inputs = planned_inputs(record)
        origin = _origin(record, by_id)
        if isinstance(inputs, str):
            skipped[inputs] += 1
            continue
        if origin is None:  # a record of a configuration the labels do not know
            skipped["replay_unplannable"] += 1
            continue
        mine: Counter[str] = Counter()
        try:
            picked = _votes_of(
                inputs, origin, group.configs, policies, choice, ml, mine
            )
        except (ValueError, KeyError, TypeError, ArithmeticError):
            skipped["replay_unplannable"] += 1
            continue
        counts.update(mine)
        used += 1
        for key, config_id in picked.items():
            votes[key][config_id] += 1
    if used == 0:
        return result("no_replay", None, best_ms)
    oracle = oracle_pick(group)
    picks: list[GroupPick] = []
    for spec in policies:
        tally: Mapping[str, int] = votes[spec.key]
        if spec.policy == ORACLE:
            tally = {} if oracle is None else {oracle: used}
        picks.append(group_pick(group, spec.key, tally, best_ms))
    return result("evaluated", None, best_ms, used, tuple(picks))


# ------------------------------------------------------------ aggregates


def _summary(
    picks: Sequence[GroupPick], config: EvalConfig, *, with_ci: bool
) -> dict[str, Any]:
    counted = [p for p in picks if p.status == "counted"]
    rel = [p.regret_pct for p in counted if p.regret_pct is not None]
    ms = [p.regret_ms for p in counted if p.regret_ms is not None]
    out: dict[str, Any] = {
        "counted_groups": len(counted),
        "unmeasured_groups": sum(1 for p in picks if p.status == "unmeasured"),
        "no_replay_groups": sum(1 for p in picks if p.status == "no_replay"),
        "mean_regret_pct": _r(math.fsum(rel) / len(rel)) if rel else None,
        "mean_regret_ms": _r(math.fsum(ms) / len(ms)) if ms else None,
        "within_threshold_pct": _r(
            sum(1 for v in rel if v <= config.regret_threshold_pct) / len(rel) * 100.0
        )
        if rel
        else None,
    }
    if with_ci:
        if rel:
            ci = bootstrap_mean_ci(
                rel,
                seed=config.bootstrap_seed,
                resamples=config.bootstrap_resamples,
                level=config.ci_level,
            )
            out["ci_pct"] = {"low": _r(ci.low), "high": _r(ci.high)}
        else:
            out["ci_pct"] = {"low": None, "high": None}
        out["zero_regret_groups"] = sum(1 for v in rel if v == 0.0)
    return out


def _policy_row(
    spec: PolicySpec, picks: Sequence[GroupPick], config: EvalConfig
) -> dict[str, Any]:
    stats = _summary(picks, config, with_ci=True)
    cost = spec.policy == COST_MODEL
    return {
        "key": spec.key,
        "policy": spec.policy,
        "kind": spec.kind,
        "calibrated": spec.calibrated if cost else None,
        "objective": (spec.objective or "min_time") if cost else None,
        "counted_groups": stats["counted_groups"],
        "unmeasured_groups": stats["unmeasured_groups"],
        "no_replay_groups": stats["no_replay_groups"],
        "mean_regret_pct": stats["mean_regret_pct"],
        "ci_pct": stats["ci_pct"],
        "mean_regret_ms": stats["mean_regret_ms"],
        "within_threshold_pct": stats["within_threshold_pct"],
        "zero_regret_groups": stats["zero_regret_groups"],
    }


def _verdict(
    criterion: int,
    status: str,
    value: float | None,
    threshold: float | None,
    counted: int,
    detail: str,
    leaked: bool,
    comparisons: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "status": "leaked" if leaked else status,
        "would_be": status if leaked else None,
        "value": _r(value),
        "threshold": _r(threshold),
        "counted_groups": counted,
        "detail": detail,
        "comparisons": [dict(c) for c in comparisons],
    }


def _verdicts(
    rows: Mapping[str, Mapping[str, Any]],
    regrets: Mapping[str, Mapping[tuple[str, str], float]],
    config: EvalConfig,
    leaked: bool,
) -> list[dict[str, Any]]:
    """``rows`` = the policy rows; ``regrets`` = policy key -> (campaign,
    group) -> regret % of its counted groups (the pairing of criterion 2)."""
    key = CRITERION_POLICY.key
    row = rows.get(key)
    n = 0 if row is None else int(row["counted_groups"])
    out: list[dict[str, Any]] = []
    # ---- criterion 1
    within = None if row is None else row["within_threshold_pct"]
    if row is None or n < config.min_groups or within is None:
        out.append(
            _verdict(
                1,
                "too_few_groups",
                within,
                config.within_target_pct,
                n,
                f"{n} counted group(s) for {key}; at least {config.min_groups} needed",
                leaked,
            )
        )
    else:
        passed = within >= config.within_target_pct
        out.append(
            _verdict(
                1,
                "PASS" if passed else "FAIL",
                within,
                config.within_target_pct,
                n,
                f"{within:.1f} % of {n} counted groups within "
                f"{config.regret_threshold_pct:g} % of the oracle (target "
                f">= {config.within_target_pct:g} %)",
                leaked,
            )
        )
    # ---- criterion 2 (D322: paired)
    crit = regrets.get(key, {})
    comparisons: list[dict[str, Any]] = []
    for policy in RULE_POLICIES:
        if policy not in rows:
            continue
        mine = regrets.get(policy, {})
        common = sorted(crit.keys() & mine.keys())
        diffs = [round(mine[g] - crit[g], _ROUND) for g in common]
        low = high = mean = None
        if len(common) >= config.min_groups:
            ci = bootstrap_mean_ci(
                diffs,
                seed=config.bootstrap_seed,
                resamples=config.bootstrap_resamples,
                level=config.ci_level,
            )
            low, high, mean = _r(ci.low), _r(ci.high), _r(ci.mean)
        elif diffs:
            mean = _r(math.fsum(diffs) / len(diffs))
        comparisons.append(
            {
                "key": policy,
                "common_groups": len(common),
                "mean_diff_pct": mean,
                "ci_low": low,
                "ci_high": high,
                "unmeasured_groups": int(rows[policy]["unmeasured_groups"]),
            }
        )
    unmeasured = 0 if row is None else int(row["unmeasured_groups"])
    note = f"{key} unmeasured in {unmeasured} group(s); "
    common_min = min((c["common_groups"] for c in comparisons), default=0)
    short = [c["key"] for c in comparisons if c["common_groups"] < config.min_groups]
    if row is None or not comparisons or short:
        why = (
            "no rule policy evaluated"
            if row is None or not comparisons
            else "too few groups counted for both with " + ", ".join(short)
        )
        out.append(
            _verdict(
                2,
                "too_few_groups",
                None,
                0.0,
                common_min,
                f"{note}{why}; at least {config.min_groups} common groups needed",
                leaked,
                comparisons,
            )
        )
        return out
    lows = [float(c["ci_low"]) for c in comparisons]
    floor = min(lows)
    if all(v > 0.0 for v in lows):
        status, text = (
            "PASS",
            (
                f"{note}every rule policy's regret minus {key}'s has a CI above 0 "
                f"(weakest lower bound {floor:.3f} %)"
            ),
        )
    elif any(float(c["ci_high"]) < 0.0 for c in comparisons):
        better = [c["key"] for c in comparisons if float(c["ci_high"]) < 0.0]
        status, text = (
            "FAIL",
            (f"{note}rule policies paired-better than {key}: " + ", ".join(better)),
        )
    else:
        unclear = [c["key"] for c in comparisons if not float(c["ci_low"]) > 0.0]
        status, text = (
            "not_separable",
            (f"{note}difference CI reaches 0 for: " + ", ".join(unclear)),
        )
    out.append(_verdict(2, status, floor, 0.0, common_min, text, leaked, comparisons))
    return out


def _ml_verdict(
    name: str,
    status: str,
    detail: str,
    leaked: bool,
    **numbers: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "status": "leaked" if leaked else status,
        "would_be": status if leaked else None,
        "detail": detail,
        "common_groups": 0,
        "mean_diff_pct": None,
        "ci_low": None,
        "ci_high": None,
        "threshold": None,
        "families": [],
    }
    base.update(numbers)
    return base


def _ml_criterion_1(
    ml: Mapping[tuple[str, str], float],
    cal: Mapping[tuple[str, str], float],
    config: EvalConfig,
    leaked: bool,
) -> dict[str, Any]:
    """D370 criterion 1 (module docstring): paired, on common groups."""
    common = sorted(ml.keys() & cal.keys())
    diffs = [round(ml[g] - cal[g], _ROUND) for g in common]
    mean = _r(math.fsum(diffs) / len(diffs)) if diffs else None
    if len(common) < config.min_groups:
        return _ml_verdict(
            "ml_criterion_1",
            "too_few_groups",
            f"{len(common)} group(s) counted for both {ML_CRITERION_POLICY.key} and "
            f"{CRITERION_POLICY.key}; at least {config.min_groups} needed",
            leaked,
            common_groups=len(common),
            mean_diff_pct=mean,
            threshold=0.0,
        )
    ci = bootstrap_mean_ci(
        diffs,
        seed=config.bootstrap_seed,
        resamples=config.bootstrap_resamples,
        level=config.ci_level,
    )
    low, high = round(ci.low, _ROUND), round(ci.high, _ROUND)
    if high < 0.0:
        status = "PASS"
        text = f"ML regret minus calibrated regret: CI [{low:.3f}, {high:.3f}] % < 0"
    elif low > 0.0:
        status = "FAIL"
        text = f"ML regret minus calibrated regret: CI [{low:.3f}, {high:.3f}] % > 0"
    else:
        status = "not_separable"
        text = (
            f"ML regret minus calibrated regret: CI [{low:.3f}, {high:.3f}] % reaches 0"
        )
    return _ml_verdict(
        "ml_criterion_1",
        status,
        f"{text} over {len(common)} common groups",
        leaked,
        common_groups=len(common),
        mean_diff_pct=_r(ci.mean),
        ci_low=low,
        ci_high=high,
        threshold=0.0,
    )


def _ml_criterion_2(
    ml: Mapping[tuple[str, str], float],
    cal: Mapping[tuple[str, str], float],
    family_of: Mapping[tuple[str, str], str],
    config: EvalConfig,
    leaked: bool,
) -> dict[str, Any]:
    """D370 criterion 2 (module docstring): per family, an absolute
    percentage-point margin; small families never pass."""
    common = sorted(ml.keys() & cal.keys())
    by_family: dict[str, list[tuple[str, str]]] = {}
    for key in common:
        by_family.setdefault(family_of[key], []).append(key)
    tolerance = config.ml_family_tolerance_pp
    families: list[dict[str, Any]] = []
    failed: list[str] = []
    small: list[str] = []
    for family in sorted(by_family):
        keys = by_family[family]
        mean_ml = round(math.fsum(ml[k] for k in keys) / len(keys), _ROUND)
        mean_cal = round(math.fsum(cal[k] for k in keys) / len(keys), _ROUND)
        if len(keys) < config.ml_min_family_groups:
            state = "too_few_groups"
            small.append(family)
        elif mean_ml <= round(mean_cal + tolerance, _ROUND):
            state = "pass"
        else:
            state = "fail"
            failed.append(family)
        families.append(
            {
                "family": family,
                "common_groups": len(keys),
                "mean_regret_ml_pct": mean_ml,
                "mean_regret_calibrated_pct": mean_cal,
                "diff_pct": _r(mean_ml - mean_cal),
                "status": state,
            }
        )
    judged = [f for f in families if f["status"] != "too_few_groups"]
    note = (
        f"; too few groups (< {config.ml_min_family_groups}, not counted): "
        + ", ".join(small)
        if small
        else ""
    )
    if not judged:
        status = "too_few_groups"
        text = (
            f"no family has {config.ml_min_family_groups} or more groups counted for "
            f"both rows{note}"
        )
    elif failed:
        status = "FAIL"
        text = (
            f"ML mean regret above calibrated + {tolerance:g} % points on: "
            + ", ".join(failed)
            + note
        )
    else:
        status = "PASS"
        text = (
            f"every family with >= {config.ml_min_family_groups} common groups "
            f"({len(judged)}) within calibrated + {tolerance:g} % points{note}"
        )
    return _ml_verdict(
        "ml_criterion_2",
        status,
        text,
        leaked,
        common_groups=len(common),
        threshold=tolerance,
        families=families,
    )


def _fallback_json(counts: Counter[str]) -> dict[str, Any]:
    terms = {
        key.split(":", 1)[1]: n
        for key, n in sorted(counts.items())
        if key.startswith("term:")
    }
    asked = sum(terms.values())
    used = terms.get("used", 0)
    return {
        "decisions": counts.get("decisions", 0),
        NO_FOLD_MODEL: counts.get(NO_FOLD_MODEL, 0),
        PREDICTOR_ERROR: counts.get(PREDICTOR_ERROR, 0),
        "terms": terms,
        "used_rate": _r(used / asked) if asked else None,
    }


def _ml_block(
    ml: EvalMl | None,
    results: Sequence[_GroupResult],
    regrets: Mapping[str, Mapping[tuple[str, str], float]],
    config: EvalConfig,
    leaked: bool,
) -> dict[str, Any]:
    """The report's ``ml`` block (module docstring)."""
    settings = {
        "criterion_policy": ML_CRITERION_POLICY.key,
        "compared_with": CRITERION_POLICY.key,
        "family_tolerance_pp": config.ml_family_tolerance_pp,
        "min_family_groups": config.ml_min_family_groups,
        "min_groups": config.min_groups,
    }
    if ml is None or not ml.evaluated:
        status = ML_UNAVAILABLE if ml is None else ml.status
        reason = "no ML input given" if ml is None else ml.reason
        text = f"ML not evaluated ({status}: {reason or 'no reason'})"
        return {
            "status": status,
            "reason": reason,
            "until": ml.until if ml is not None else None,
            "settings": settings,
            "verdicts": [
                # never ``leaked``: nothing was claimed, on any data
                _ml_verdict(name, status, text, leaked=False)
                for name in ML_VERDICTS
            ],
            "fallback": {"total": _fallback_json(Counter()), "families": []},
            "folds": [dict(f) for f in (ml.folds if ml is not None else ())],
            "terms": dict(ml.terms) if ml is not None else {},
        }
    mine = regrets.get(ML_CRITERION_POLICY.key, {})
    cal = regrets.get(CRITERION_POLICY.key, {})
    family_of = {
        (r.group.campaign_id, r.group.group_id): r.group.family for r in results
    }
    total: Counter[str] = Counter()
    per_family: dict[str, Counter[str]] = {}
    for r in results:
        if r.status != "evaluated":
            continue
        total.update(r.ml_counts)
        per_family.setdefault(r.group.family, Counter()).update(r.ml_counts)
    return {
        "status": ml.status,
        "reason": ml.reason,
        "until": ml.until,
        "settings": settings,
        "verdicts": [
            _ml_criterion_1(mine, cal, config, leaked),
            _ml_criterion_2(mine, cal, family_of, config, leaked),
        ],
        "fallback": {
            "total": _fallback_json(total),
            "families": [
                {"family": family} | _fallback_json(per_family[family])
                for family in sorted(per_family)
            ],
        },
        "folds": [dict(f) for f in ml.folds],
        "terms": dict(ml.terms),
    }


def _config_json(config: ConfigRef) -> dict[str, Any]:
    return {
        "config_id": config.config_id,
        "strategy": config.strategy,
        "node_id": config.node_id,
        "origin_node_id": config.origin_node_id,
        "node_set": sorted(config.node_set),
        "state": config.state,
        "median_ms": _r(_finite(config.median_ms)),
        "repetitions": config.repetitions,
    }


def _group_json(result: _GroupResult) -> dict[str, Any]:
    group = result.group
    return {
        "campaign_id": group.campaign_id,
        "group_id": group.group_id,
        "family": group.family,
        "condition": group.condition,
        "label": group.label,
        "best_config_id": group.best_config_id,
        "tied_config_ids": sorted(group.tied_config_ids),
        "best_median_ms": _r(result.best_ms),
        "status": result.status,
        "excluded_reason": result.excluded_reason,
        "records": result.records,
        "records_used": result.records_used,
        "configs": [
            _config_json(c) for c in sorted(group.configs, key=lambda c: c.config_id)
        ],
        "picks": [
            {
                "key": p.key,
                "status": p.status,
                "config_id": p.config_id,
                "regret_pct": _r(p.regret_pct),
                "regret_ms": _r(p.regret_ms),
                "votes": dict(sorted(p.votes.items())),
            }
            for p in result.picks
        ],
    }


def _choice_json(choice: CalibrationChoice) -> dict[str, Any]:
    calibration = choice.calibration
    return {
        "family": choice.family,
        "source": choice.source,
        "version": choice.version,
        "fitted_runs": choice.fitted_runs,
        "excluded_runs": choice.excluded_runs,
        "factors": 0 if calibration is None else calibration.factor_count,
        "digest": calibration_digest(calibration),
    }


# ------------------------------------------------------------ evaluate


def _regrets(
    results: Sequence[_GroupResult], keys: Sequence[str]
) -> dict[str, dict[tuple[str, str], float]]:
    """key -> (campaign, group) -> regret % of every counted pick."""
    out: dict[str, dict[tuple[str, str], float]] = {k: {} for k in keys}
    for result in results:
        where = (result.group.campaign_id, result.group.group_id)
        for pick in result.picks:
            if pick.status == "counted" and pick.regret_pct is not None:
                out[pick.key][where] = pick.regret_pct
    return out


def _picks(result: _GroupResult, key: str) -> tuple[GroupPick, ...]:
    """The group's pick for ``key``: its own when evaluated, a ``no_replay``
    one when no record could be planned, none when excluded."""
    if result.status == "no_replay":
        return (GroupPick(key, "no_replay", None, None, None, {}),)
    return tuple(p for p in result.picks if p.key == key)


def evaluate(
    groups: Sequence[EvalGroup],
    records: Sequence[EvalRecord],
    calibrations: Mapping[str, CalibrationChoice],
    policies: Sequence[PolicySpec],
    config: EvalConfig,
    *,
    campaign_ids: Sequence[str],
    holdout: str | None = None,
    leaked: bool = False,
    latency: Sequence[LatencySample] = (),
    ml: EvalMl | None = None,
) -> dict[str, Any]:
    """The evaluation report (module docstring) as a JSON-ready dict.
    ``calibrations`` = family -> the calibration its calibrated rows use
    (:mod:`.heldout`); ``holdout`` restricts the groups to that family.
    ``latency`` = the campaigns' dispatched decisions (V0.14 G6, D324: the
    ``latency`` block and criterion 3, :mod:`.latency`; never filtered by
    ``holdout`` - a decision's latency does not depend on the family).
    ``ml`` (V0.15 G5a) = the fold models of the ML rows; the ML policy rows
    in ``policies`` are dropped unless it is evaluated."""
    if ml is None or not ml.evaluated:
        policies = [spec for spec in policies if not spec.ml]
    inputs = ml.inputs if ml is not None and ml.evaluated else {}
    keys = [spec.key for spec in policies]
    if len(set(keys)) != len(keys):
        raise ValueError("a policy row is listed twice")
    chosen = sorted(
        (g for g in groups if holdout is None or g.family == holdout),
        key=lambda g: (g.campaign_id, g.group_id),
    )
    by_group: dict[tuple[str, str], list[EvalRecord]] = {}
    for record in records:
        by_group.setdefault((record.campaign_id, record.group_id), []).append(record)
    skipped: Counter[str] = Counter()
    results: list[_GroupResult] = []
    for group in chosen:
        mine = sorted(
            by_group.get((group.campaign_id, group.group_id), []),
            key=lambda r: (r.config_id, r.repetition, r.attempt_id),
        )
        results.append(
            _evaluate_group(
                group,
                mine,
                policies,
                calibrations.get(group.family),
                skipped,
                inputs.get(group.family),
            )
        )
    evaluated = [r for r in results if r.status == "evaluated"]
    rows = {
        spec.key: _policy_row(
            spec, [p for r in results for p in _picks(r, spec.key)], config
        )
        for spec in policies
    }
    families = [
        {"family": family, "key": spec.key}
        | _summary(
            [
                p
                for r in results
                if r.group.family == family
                for p in _picks(r, spec.key)
            ],
            config,
            with_ci=False,
        )
        for family in sorted({r.group.family for r in results})
        for spec in policies
    ]
    labels = Counter(r.group.label for r in results)
    timing = latency_block(latency, config.latency)
    used_families = sorted({r.group.family for r in evaluated})
    regrets = _regrets(results, keys)
    return {
        "eval_version": EVAL_VERSION,
        "campaign_ids": sorted(set(campaign_ids)),
        "holdout": holdout,
        "leaked": leaked,
        "settings": {
            "regret_threshold_pct": config.regret_threshold_pct,
            "within_target_pct": config.within_target_pct,
            "min_groups": config.min_groups,
            "bootstrap_resamples": config.bootstrap_resamples,
            "bootstrap_seed": config.bootstrap_seed,
            "ci_level": config.ci_level,
            "criterion_policy": CRITERION_POLICY.key,
        },
        "counts": {
            "groups": len(results),
            "evaluated_groups": len(evaluated),
            "labels": {
                kind: labels.get(kind, 0)
                for kind in ("best", "tie", "insufficient", "pending")
            },
            "no_replay_groups": sum(1 for r in results if r.status == "no_replay"),
            "records": sum(r.records for r in results),
            "records_used": sum(r.records_used for r in results),
            "records_skipped": {k: skipped.get(k, 0) for k in SKIP_REASONS},
            "non_finite_configs": skipped.get("non_finite_configs", 0),
        },
        "verdicts": [
            *_verdicts(rows, regrets, config, leaked),
            latency_verdict(timing, leaked=leaked),  # V0.14 G6 (D324)
        ],
        "ml": _ml_block(ml, results, regrets, config, leaked),  # V0.15 G5a
        "latency": timing,
        "policies": [rows[k] for k in keys],
        "families": families,
        "calibrations": [
            _choice_json(calibrations[f]) for f in used_families if f in calibrations
        ],
        "groups": [_group_json(r) for r in results],
    }
