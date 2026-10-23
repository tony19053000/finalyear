"""The 0.15.10 classifier experiment's examples (V0.15 G6a, D343, D375,
D376) - the controller side, pure.

"Why not just classify?" - a direct classifier that maps a job and the
cluster it meets to the **winning configuration class** is the obvious
alternative to the planner's cost regression. To answer it with the same
numbers the evaluator uses, every labelled benchmark group (V0.9 labels,
:mod:`.core`'s inputs) becomes one **example**:

* **label** - the class of the oracle's configuration (:func:`.core.
  oracle_pick`, D298): ``local``, ``mode1`` or ``mode3_k`` (a Mode 3 set of
  ``k`` nodes). A ``mode3_k`` the planner never enumerated for the group is
  mapped to the nearest ``mode3_j`` it did enumerate (smaller ``j`` on a tie,
  D375 "the nearest candidate the planner actually enumerated");
* **features** - fixed width on a changing fleet: the job's own size
  features plus, over the decision's feasible **single-node** candidates,
  the min / max / mean of each node's snapshot features (hardware, load,
  cache, links: :func:`~meshforge.controller.ml.features.features`, the
  ML dataset's own definitions) and the node count; averaged over the
  group's usable records (missing stays NaN, never 0);
* **class outcomes** - for each class the planner enumerated, the
  configuration of **the planner's best-ranked candidate of that class**
  (the calibrated ``cost_model``, ``min_time``, on the family's held-out
  calibration, D316 - exactly the evaluator's criterion row), voted over the
  records like a policy (:func:`.core.vote`) and turned into regret vs the
  oracle (:func:`.core.group_pick`, D315). A classifier's pick of class
  ``c`` scores the regret of that configuration - never the label's own
  configuration (a correct class can still pick the wrong node);
* **cost model** - the planner's own pick on the same records (the
  "cost regression" row the classifier is compared with).

Groups that are not ``best`` / ``tie``, or have no usable record, are
counted, not used. Pure: no database, no clock, no logging. The training
and leave-one-family-out evaluation run in the ``ml`` child
(:mod:`meshforge.controller.ml.train.classify`); this module never imports
an ML library.
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from meshforge.controller.ml.features import Predicted, features
from meshforge.controller.ml.schema import FEATURES, FeatureGroup
from meshforge.controller.planner.plan import plan
from meshforge.controller.planner.types import CandidateEval, Decision, Objective
from meshforge.controller.planner_eval.core import (
    EVALUATED_LABELS,
    PlannedInputs,
    best_median,
    group_pick,
    oracle_pick,
    planned_inputs,
)
from meshforge.controller.planner_eval.heldout import CalibrationChoice
from meshforge.controller.planner_eval.mapping import Pick, config_for, pick_of
from meshforge.controller.planner_eval.model import (
    COST_MODEL,
    CRITERION_POLICY,
    UNMEASURED,
    ConfigRef,
    EvalGroup,
    EvalRecord,
)

__all__ = [
    "AGGREGATES",
    "EXAMPLES_VERSION",
    "FEATURE_NAMES",
    "JOB_FEATURES",
    "NODE_FEATURES",
    "Example",
    "build_examples",
    "class_of_candidate",
    "class_of_config",
    "examples_document",
    "nearest_class",
]

EXAMPLES_VERSION: Final = 1
"""Version of the examples document; bump on any change of its shape or of
the feature list."""
JOB_FEATURES: Final = (
    "job_input_bytes",
    "job_items",
    "job_item_bytes",
    "job_cpus",
    "job_ram_bytes",
    "job_vram_bytes",
    "job_gpu",
    "job_hist_output_bytes",
)
"""The job's own size (decision-independent ``job_size`` features; not the
chosen plan's lanes or chunk size)."""
_NODE_GROUPS: Final = frozenset(
    {FeatureGroup.HARDWARE, FeatureGroup.LOAD, FeatureGroup.CACHE, FeatureGroup.NETWORK}
)
NODE_FEATURES: Final = tuple(
    f.name
    for f in FEATURES
    if f.kind == "float"
    and f.group in _NODE_GROUPS
    # the bandwidth one term used: a property of a prediction, not of the node
    and f.name != "net_term_bw_bytes_s"
)
AGGREGATES: Final = ("min", "max", "mean")
FEATURE_NAMES: Final = (
    *JOB_FEATURES,
    *(f"{agg}_{name}" for name in NODE_FEATURES for agg in AGGREGATES),
    "node_count",
)
_NO_TERM: Final = Predicted(raw_s=math.nan, source=None)


# ------------------------------------------------------------ classes


def class_of_config(config: ConfigRef) -> str:
    """A campaign configuration's class: ``local`` | ``mode1`` |
    ``mode3_k``."""
    if config.strategy == "mode3":
        return f"mode3_{len(config.node_set)}"
    return config.strategy


def _class_of_pick(pick: Pick) -> str:
    if pick.strategy == "mode3":
        return f"mode3_{len(pick.node_set)}"
    return pick.strategy


def _candidate_pick(item: CandidateEval) -> Pick:
    """What running ``item`` means in campaign terms (the
    :func:`~.mapping.pick_of` rules, for any ranked row)."""
    if item.mode == "split":
        active = [lane for lane in item.lanes if lane.active]
        if len(active) > 1:
            nodes = tuple(sorted(lane.node_id for lane in active))
            return Pick("mode3", active[0].node_id, nodes)
        if len(active) == 1:  # a set with one active lane runs that lane alone
            lane = active[0]
            return Pick("local" if lane.local else "mode1", lane.node_id)
        return Pick("mode3", item.node_id, tuple(sorted(item.node_set)))
    if item.mode == "local":
        return Pick("local", item.node_id)
    return Pick("mode1", item.node_id)


def class_of_candidate(item: CandidateEval) -> str:
    """A ranked candidate row's class (the same names as
    :func:`class_of_config`)."""
    return _class_of_pick(_candidate_pick(item))


def _mode3_size(name: str) -> int | None:
    if not name.startswith("mode3_"):
        return None
    try:
        return int(name.removeprefix("mode3_"))
    except ValueError:
        return None


def nearest_class(label: str, enumerated: Sequence[str]) -> str:
    """``label`` itself when the planner enumerated it (or it is not a Mode 3
    class, or no Mode 3 class was enumerated); else the enumerated
    ``mode3_j`` with the nearest ``j`` (the smaller on a tie)."""
    size = _mode3_size(label)
    if size is None or label in enumerated:
        return label
    sizes = sorted(j for j in (_mode3_size(c) for c in enumerated) if j is not None)
    if not sizes:
        return label
    best = min(sizes, key=lambda j: (abs(j - size), j))
    return f"mode3_{best}"


def best_per_class(decision: Decision) -> dict[str, Pick]:
    """Class -> the pick of its best-ranked candidate (rank 1 first)."""
    out: dict[str, Pick] = {}
    ranked = sorted(
        (c for c in decision.candidates if c.feasible and c.rank is not None),
        key=lambda c: (c.rank or 0, c.node_id),
    )
    for item in ranked:
        pick = _candidate_pick(item)
        out.setdefault(_class_of_pick(pick), pick)
    return out


# ------------------------------------------------------------ features


def _row_features(inputs: PlannedInputs, decision: Decision) -> list[float]:
    request, snapshot, now = inputs
    nodes = sorted(
        {
            c.node_id
            for c in decision.candidates
            if c.feasible and c.mode in ("local", "offload")
        }
    )
    per_node = [
        features(request, snapshot, now, node_id, _NO_TERM) for node_id in nodes
    ]
    first = per_node[0] if per_node else features(request, snapshot, now, "", _NO_TERM)
    out = [float(first[name]) for name in JOB_FEATURES]
    for name in NODE_FEATURES:
        values = [v[name] for v in per_node if not math.isnan(v[name])]
        if values:
            out += [min(values), max(values), math.fsum(values) / len(values)]
        else:
            out += [math.nan, math.nan, math.nan]
    out.append(float(len(nodes)))
    return out


def _mean_columns(rows: Sequence[Sequence[float]]) -> list[float]:
    out: list[float] = []
    for column in zip(*rows, strict=True):
        present = [v for v in column if not math.isnan(v)]
        out.append(math.fsum(present) / len(present) if present else math.nan)
    return out


# ------------------------------------------------------------ examples


@dataclass(frozen=True, slots=True)
class Example:
    """One labelled group (module docstring)."""

    campaign_id: str
    group_id: str
    family: str
    fingerprint: str | None
    label: str
    oracle_class: str
    """The oracle configuration's own class (before :func:`nearest_class`)."""
    records_used: int
    features: tuple[float, ...]
    classes: Mapping[str, Mapping[str, Any]]
    """Enumerated class -> ``{config_id, status, regret_pct, votes}``."""
    cost_model: Mapping[str, Any]
    """The planner's own pick: ``{config_id, status, regret_pct, votes}``."""


def _outcome(group: EvalGroup, votes: Counter[str], best_ms: float) -> dict[str, Any]:
    pick = group_pick(group, "", votes, best_ms)
    return {
        "config_id": pick.config_id,
        "status": pick.status,
        "regret_pct": pick.regret_pct,
        "votes": dict(sorted(votes.items())),
    }


def _example(
    group: EvalGroup,
    records: Sequence[EvalRecord],
    choice: CalibrationChoice | None,
    skipped: Counter[str],
) -> Example | str:
    """The group's example, or why it has none."""
    if group.label not in EVALUATED_LABELS:
        return "not_labelled"
    best_ms = best_median(group.configs)
    oracle = oracle_pick(group)
    by_id = {c.config_id: c for c in group.configs}
    if (
        best_ms is None
        or not math.isfinite(best_ms)
        or best_ms <= 0.0
        or oracle is None
        or oracle not in by_id
    ):
        return "no_positive_median"
    calibration = None if choice is None else choice.calibration
    class_votes: dict[str, Counter[str]] = {}
    planner_votes: Counter[str] = Counter()
    rows: list[list[float]] = []
    fingerprint: str | None = None
    for record in records:
        inputs = planned_inputs(record)
        own = by_id.get(record.config_id)
        if isinstance(inputs, str):
            skipped[inputs] += 1
            continue
        if own is None:
            skipped["replay_unplannable"] += 1
            continue
        request, snapshot, now = inputs
        # the evaluator's criterion row: calibrated cost_model, min_time
        asked = dataclasses.replace(
            request, objective=cast("Objective | None", CRITERION_POLICY.objective)
        )
        try:
            decision = plan(asked, snapshot, COST_MODEL, now, calibration=calibration)
            row = _row_features(inputs, decision)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            skipped["replay_unplannable"] += 1
            continue
        origin = own.origin_node_id
        found = config_for(pick_of(decision), origin, group.configs)
        planner_votes[UNMEASURED if found is None else found.config_id] += 1
        for name, pick in best_per_class(decision).items():
            hit = config_for(pick, origin, group.configs)
            class_votes.setdefault(name, Counter())[
                UNMEASURED if hit is None else hit.config_id
            ] += 1
        rows.append(row)
        fingerprint = fingerprint or record.fingerprint
    if not rows:
        return "no_replay"
    oracle_class = class_of_config(by_id[oracle])
    return Example(
        campaign_id=group.campaign_id,
        group_id=group.group_id,
        family=group.family,
        fingerprint=fingerprint,
        label=nearest_class(oracle_class, sorted(class_votes)),
        oracle_class=oracle_class,
        records_used=len(rows),
        features=tuple(_mean_columns(rows)),
        classes={
            name: _outcome(group, votes, best_ms)
            for name, votes in sorted(class_votes.items())
        },
        cost_model=_outcome(group, planner_votes, best_ms),
    )


def build_examples(
    groups: Sequence[EvalGroup],
    records: Sequence[EvalRecord],
    calibrations: Mapping[str, CalibrationChoice],
) -> tuple[list[Example], dict[str, int]]:
    """``(examples, counts)``: one example per usable labelled group, in
    ``(campaign, group)`` order; ``counts`` = groups by why they have none
    (``not_labelled``, ``no_positive_median``, ``no_replay``) and records
    skipped by reason (``record:<reason>``)."""
    by_group: dict[tuple[str, str], list[EvalRecord]] = {}
    for record in records:
        by_group.setdefault((record.campaign_id, record.group_id), []).append(record)
    examples: list[Example] = []
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for group in sorted(groups, key=lambda g: (g.campaign_id, g.group_id)):
        mine = sorted(
            by_group.get((group.campaign_id, group.group_id), []),
            key=lambda r: (r.config_id, r.repetition, r.attempt_id),
        )
        made = _example(group, mine, calibrations.get(group.family), skipped)
        if isinstance(made, str):
            counts[made] += 1
        else:
            examples.append(made)
    counts["groups"] = len(groups)
    counts["examples"] = len(examples)
    for reason, n in skipped.items():
        counts[f"record:{reason}"] = n
    return examples, dict(sorted(counts.items()))


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def examples_document(
    examples: Sequence[Example],
    counts: Mapping[str, int],
    *,
    campaign_ids: Sequence[str],
    calibrations: Mapping[str, CalibrationChoice],
) -> dict[str, Any]:
    """The JSON document the ``ml`` child reads (NaN -> ``null``)."""
    return {
        "examples_version": EXAMPLES_VERSION,
        "campaign_ids": sorted(set(campaign_ids)),
        "policy": CRITERION_POLICY.key,
        "feature_names": list(FEATURE_NAMES),
        "counts": dict(counts),
        "calibrations": {
            family: {
                "source": choice.source,
                "fitted_runs": choice.fitted_runs,
                "excluded_runs": choice.excluded_runs,
            }
            for family, choice in sorted(calibrations.items())
        },
        "examples": [
            {
                "campaign_id": e.campaign_id,
                "group_id": e.group_id,
                "family": e.family,
                "fingerprint": e.fingerprint,
                "label": e.label,
                "oracle_class": e.oracle_class,
                "records_used": e.records_used,
                "features": [_finite(v) for v in e.features],
                "classes": {k: dict(v) for k, v in e.classes.items()},
                "cost_model": dict(e.cost_model),
            }
            for e in examples
        ],
    }
