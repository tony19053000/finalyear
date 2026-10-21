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
   measured configurations. An ``unmeasured`` pick is never counted.

**Per policy**: mean relative regret over its counted groups with a seeded
bootstrap CI (:func:`meshforge.bench.stats.bootstrap_mean_ci`, D317), mean
absolute regret, the share within ``regret_threshold_pct``; per family the
same without CI.

**Verdicts** (D317), on :data:`.model.CRITERION_POLICY` (calibrated
``cost_model``, ``min_time``):

* criterion 1 - ``PASS`` iff at least ``within_target_pct`` % of its counted
  groups are within ``regret_threshold_pct`` %; else ``FAIL``;
* criterion 2 - ``PASS`` iff its CI upper bound is below **every** rule
  policy's CI lower bound; ``FAIL`` when some rule policy's CI is entirely
  below its own; otherwise ``not_separable``;
* fewer than ``min_groups`` counted groups (for criterion 2: in any row it
  compares) -> ``too_few_groups``, never ``PASS``;
* a ``leaked`` report (``allow_leak``) -> every verdict ``leaked``, with
  ``would_be`` the verdict above (D299: never a claim on training data).
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
from meshforge.controller.planner.replay import (
    ReplayError,
    from_replay,
    shadow_from_replay,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Objective,
    PlanRequest,
)
from meshforge.controller.planner_eval.heldout import (
    CalibrationChoice,
    calibration_digest,
)
from meshforge.controller.planner_eval.mapping import config_for, pick_of
from meshforge.controller.planner_eval.model import (
    COST_MODEL,
    CRITERION_POLICY,
    EVAL_VERSION,
    ORACLE,
    RULE_POLICIES,
    UNMEASURED,
    ConfigRef,
    EvalConfig,
    EvalGroup,
    EvalRecord,
    PolicySpec,
)

__all__ = [
    "SKIP_REASONS",
    "GroupPick",
    "PlannedInputs",
    "evaluate",
    "group_pick",
    "group_regret",
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


def _votes_of(
    inputs: PlannedInputs,
    origin: str,
    configs: Sequence[ConfigRef],
    policies: Sequence[PolicySpec],
    choice: CalibrationChoice | None,
) -> dict[str, str]:
    """policy key -> the configuration id (or ``unmeasured``) this record's
    replay picks under each policy (the oracle is not planned)."""
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
            asked, snapshot, spec.policy, now, calibration=_calibration(spec, choice)
        )
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


def _best_median(configs: Sequence[ConfigRef]) -> float | None:
    medians = [
        c.median_ms
        for c in configs
        if c.state == "measured" and c.median_ms is not None
    ]
    return min(medians) if medians else None


def _oracle_pick(group: EvalGroup) -> str | None:
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
    return delta / best_ms * 100.0, delta


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


def _origin(record: EvalRecord, configs: Mapping[str, ConfigRef]) -> str | None:
    own = configs.get(record.config_id)
    return None if own is None else own.origin_node_id


def _evaluate_group(
    group: EvalGroup,
    records: Sequence[EvalRecord],
    policies: Sequence[PolicySpec],
    choice: CalibrationChoice | None,
    skipped: Counter[str],
) -> _GroupResult:
    def result(
        status: str,
        reason: str | None,
        best: float | None = None,
        used: int = 0,
        picks: tuple[GroupPick, ...] = (),
    ) -> _GroupResult:
        return _GroupResult(group, status, reason, best, len(records), used, picks)

    if group.label not in EVALUATED_LABELS:
        return result("excluded", f"{group.label}: {group.reason or 'not labelled'}")
    best_ms = _best_median(group.configs)
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
        try:
            picked = _votes_of(inputs, origin, group.configs, policies, choice)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            skipped["replay_unplannable"] += 1
            continue
        used += 1
        for key, config_id in picked.items():
            votes[key][config_id] += 1
    if used == 0:
        return result("no_replay", None, best_ms)
    oracle = _oracle_pick(group)
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
) -> dict[str, Any]:
    return {
        "criterion": criterion,
        "status": "leaked" if leaked else status,
        "would_be": status if leaked else None,
        "value": _r(value),
        "threshold": _r(threshold),
        "counted_groups": counted,
        "detail": detail,
    }


def _verdicts(
    rows: Mapping[str, Mapping[str, Any]], config: EvalConfig, leaked: bool
) -> list[dict[str, Any]]:
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
    # ---- criterion 2
    rules = [rows[p] for p in RULE_POLICIES if p in rows]
    short = [r["key"] for r in rules if int(r["counted_groups"]) < config.min_groups]
    high = None if row is None else row["ci_pct"]["high"]
    low = None if row is None else row["ci_pct"]["low"]
    if row is None or n < config.min_groups or short or not rules or high is None:
        why = (
            f"{n} counted group(s) for {key}"
            if row is None or n < config.min_groups
            else "too few counted groups for " + ", ".join(short)
            if short
            else "no rule policy evaluated"
        )
        out.append(
            _verdict(
                2,
                "too_few_groups",
                high,
                None,
                n,
                f"{why}; at least {config.min_groups} needed",
                leaked,
            )
        )
        return out
    lows = [r["ci_pct"]["low"] for r in rules]
    highs = [r["ci_pct"]["high"] for r in rules]
    floor = min(lows)
    if all(high < other for other in lows):
        status, text = (
            "PASS",
            (
                f"{key} CI upper {high:.3f} % < every rule policy's CI lower "
                f"(lowest {floor:.3f} %)"
            ),
        )
    elif low is not None and any(other < low for other in highs):
        better = [r["key"] for r in rules if r["ci_pct"]["high"] < low]
        status, text = (
            "FAIL",
            (f"rule policies with a CI entirely below {key}'s: " + ", ".join(better)),
        )
    else:
        overlap = [r["key"] for r in rules if not high < r["ci_pct"]["low"]]
        status, text = (
            "not_separable",
            (f"{key} CI [{low:.3f}, {high:.3f}] % overlaps: " + ", ".join(overlap)),
        )
    out.append(_verdict(2, status, high, floor, n, text, leaked))
    return out


def _config_json(config: ConfigRef) -> dict[str, Any]:
    return {
        "config_id": config.config_id,
        "strategy": config.strategy,
        "node_id": config.node_id,
        "origin_node_id": config.origin_node_id,
        "node_set": sorted(config.node_set),
        "state": config.state,
        "median_ms": _r(config.median_ms),
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
) -> dict[str, Any]:
    """The evaluation report (module docstring) as a JSON-ready dict.
    ``calibrations`` = family -> the calibration its calibrated rows use
    (:mod:`.heldout`); ``holdout`` restricts the groups to that family."""
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
                group, mine, policies, calibrations.get(group.family), skipped
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
    used_families = sorted({r.group.family for r in evaluated})
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
        },
        "verdicts": _verdicts(rows, config, leaked),
        "policies": [rows[k] for k in keys],
        "families": families,
        "calibrations": [
            _choice_json(calibrations[f]) for f in used_families if f in calibrations
        ],
        "groups": [_group_json(r) for r in results],
    }
