"""The regret part of the model set's activation gate (V0.15 G5a, D371 -
D339/D360 completed). Runs in the controller's retrain task, after the
child's error gate accepted a candidate set and before it is stored.

Prediction error can improve while decisions get worse (a model better on
average but wrong on the close calls), so an accepted candidate must also not
make the planner pick worse configurations:

1. **the slice** - the error gate's own D329 held-out decisions
   (:func:`~.holdout.held_out_decisions` over the same dataset rows, so both
   checks of one retrain judge the same decisions), their jobs, and the
   labelled benchmark groups those jobs ran in
   (:func:`~meshforge.controller.planner_eval.loader.load_for_jobs`, the
   calibration's D319 read);
2. **the candidate** - its artefacts loaded through the serving path
   (:func:`~.folds.loaded_set` -> :class:`~.source.SetPredictor` ->
   :func:`~.predictor.ml_input`, no time budget);
3. **what serves now** - the reference set the child was gated against
   (pinned, else active), loaded by version through the
   :class:`~.registry_loader.RegistryLoader`; none serves (or it cannot be
   loaded) -> the calibration alone;
4. :func:`~meshforge.controller.planner_eval.regret_gate.ml_regret_check`
   (worker thread): ``cost_model:ml`` (``min_time``) on the slice's groups
   with each, on the **live** calibration, paired; ``passed`` iff the
   candidate's mean regret <= current x 1.05 (the D319 rule), ``skipped``
   below 10 common groups (the error gate alone decides).

The result is the gate document's ``regret`` block (:func:`regret_block`); a
``rejected`` check turns the set ``rejected`` with rule ``regret_worse``
(:func:`apply`). G3 rows keep ``"regret": "pending_g4"``; this is never
written again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.folds import loaded_set, planner_input
from meshforge.controller.ml.holdout import held_out_decisions, rows_of_dataset
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.planner.calibration import Calibration
from meshforge.controller.planner_eval.loader import EvalTooLargeError, load_for_jobs
from meshforge.controller.planner_eval.regret_gate import (
    MIN_GROUPS,
    TOLERANCE,
    RegretCheck,
    ml_regret_check,
)

__all__ = ["REGRET_RULE", "RegretOutcome", "apply", "check_candidate", "regret_block"]

REGRET_RULE: Final = "regret_worse"
"""The gate rule of a set the regret check rejected."""


@dataclass(frozen=True, slots=True)
class RegretOutcome:
    check: RegretCheck
    current_version: str | None
    """The set the candidate was compared with (None = calibration only)."""
    current_note: str | None = None
    """Why the reference set was not used (it could not be loaded)."""
    held_decisions: int = 0
    jobs: int = 0


def _skipped(reason: str) -> RegretCheck:
    return RegretCheck("skipped", 0, None, None, f"regret check skipped ({reason})")


def regret_block(outcome: RegretOutcome) -> dict[str, Any]:
    """The gate document's ``regret`` block."""
    check = outcome.check
    return {
        "status": check.status,
        "reason": check.reason,
        "groups": check.groups,
        "regret_candidate_pct": check.regret_new,
        "regret_current_pct": check.regret_active,
        "current": {
            "kind": "set" if outcome.current_version is not None else "calibration",
            "set_version": outcome.current_version,
            "note": outcome.current_note,
        },
        "held_decisions": outcome.held_decisions,
        "jobs": outcome.jobs,
        "tolerance": TOLERANCE,
        "min_groups": MIN_GROUPS,
        "policy": "cost_model:ml:min_time",
    }


async def check_candidate(
    engine: AsyncEngine,
    rows: Sequence[Mapping[str, Any]],
    doc: Mapping[str, Any],
    artefacts: Mapping[str, bytes],
    *,
    reference_version: str | None,
    loader: RegistryLoader,
    calibration: Calibration | None,
    max_label_rows: int,
    max_interval_log: float,
) -> RegretOutcome:
    """The D371 check of the candidate set (module docstring). ``rows`` =
    the dataset rows the child trained and gated on. Raises
    :class:`~meshforge.controller.planner_eval.loader.EvalLoadError` /
    :class:`~.registry.MlRegistryError` when the database fails."""
    try:
        candidate = planner_input(loaded_set(doc, artefacts), max_interval_log)
    except (ArtefactError, KeyError, TypeError, ValueError):
        candidate = None
    current = None
    note = None
    if reference_version is not None:
        current = await loader.load(reference_version)
        if current is None:
            note = "the reference set cannot be loaded: judged against calibration"
    current_version = reference_version if current is not None else None
    if candidate is None:
        return RegretOutcome(
            _skipped("the candidate set has no usable model"), current_version, note
        )
    held, _counts = held_out_decisions(rows_of_dataset(rows))
    jobs = sorted({str(row["job_id"]) for row in rows if row["decision_id"] in held})
    try:
        inputs = await load_for_jobs(engine, jobs, max_rows=max_label_rows)
    except EvalTooLargeError:
        return RegretOutcome(
            _skipped("campaign too large"), current_version, note, len(held), len(jobs)
        )
    check = await asyncio.to_thread(
        ml_regret_check,
        inputs.groups,
        inputs.records,
        calibration,
        candidate,
        current,
    )
    return RegretOutcome(check, current_version, note, len(held), len(jobs))


def apply(doc: Mapping[str, Any], outcome: RegretOutcome) -> dict[str, Any]:
    """``doc`` (the child's ``set.json``) with the regret block in its gate,
    and - when the check rejected - ``accepted`` false, rule
    :data:`REGRET_RULE` and the check's reason (module docstring)."""
    gate = dict(doc.get("gate") or {})
    gate["regret"] = regret_block(outcome)
    out = {**doc, "gate": gate}
    if outcome.check.status == "rejected":
        failed = [r for r in gate.get("rules_failed") or () if r != REGRET_RULE]
        gate.update(
            accepted=False, rule=REGRET_RULE, rules_failed=[*failed, REGRET_RULE]
        )
        out.update(
            accepted=False,
            rule=REGRET_RULE,
            reason=f"{REGRET_RULE}: {outcome.check.reason}",
        )
    return out
