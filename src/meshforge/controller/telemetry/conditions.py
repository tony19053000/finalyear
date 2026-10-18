"""A run record's benchmark condition: asked, measured, held? (V0.9 G4, D217). Pure.

:func:`condition_outcome` turns what the recorder already has for an attempt
- its campaign identity (the run's canonical ``condition``, the campaign's
``session_label`` and the controller-side ``condition_facts``,
:func:`meshforge.controller.bench.conditions.bench_identities`) and the
agent's condition report (``attempt.workload["condition"]``) - into

* ``condition`` - the canonical string (null for a job that is no campaign run);
* ``condition_measured`` - a JSON object: ``verified`` (bool), ``reason``,
  ``agent`` (what the agent measured: load cores, GPU utilisation, RTT
  before/after, blobs evicted / hit), ``controller`` (fillers ahead of the
  run, the warm-up's state, when the run started after the pause),
  ``reverted`` and ``session_label``;
* a queue **filler** / cache **warm-up** job (identity ``bench_role``, no
  campaign) - ``condition`` null, ``condition_measured`` =
  ``{"bench_role", "run_id"}`` (:data:`BENCH_ROLES`), never failed;
* ``failed`` - the condition did not hold although it should have been
  measured: the record's failure class becomes ``condition_failed`` (never
  ``ok``: an unverified condition is never labelled, D217).

**When does an unverified condition fail the record?** When the agent said
so (its report is ``verified = false``: it refused to run, or a load died
while the job ran), or when the attempt succeeded but a controller-side check
failed (fewer fillers ahead than asked, dispatched after the pause ended) or
an agent condition has no report at all. An attempt that failed for its own
reason (node lost, workload error) keeps that class - the condition is then
only marked unverified in ``condition_measured``.

The two columns are telemetry schema v2's (migration 0023);
:func:`meshforge.controller.telemetry.record.build_record` is the only caller and
bounds / sanitises both (non-finite floats in the agent's ``measured`` -> null).
A stored condition that does not parse yields a null ``condition`` and a failed
outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from meshforge.common.bench_conditions import (
    ConditionError,
    QueueCondition,
    Unavailable,
    parse_condition,
)
from meshforge.common.states import AttemptState

__all__ = ["BENCH_ROLES", "ConditionOutcome", "condition_outcome"]

BENCH_ROLES: Final = frozenset({"filler", "warmup"})
"""System-made background jobs of a campaign run (``queue:K`` fillers, the
``cache:warm`` warm-up): no campaign identity, never labels, never planner
training rows - their record's ``condition_measured`` is
``{"bench_role": ..., "run_id": ...}`` so a filter can drop them."""


@dataclass(frozen=True, slots=True)
class ConditionOutcome:
    condition: str | None
    measured: dict[str, Any] | None
    failed: bool = False


_NOT_A_RUN: Final = ConditionOutcome(None, None)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _label(identity: Mapping[str, Any]) -> str | None:
    label = identity.get("session_label")
    return label if isinstance(label, str) else None


def condition_outcome(
    *,
    campaign: Mapping[str, Any] | None,
    workload: Mapping[str, Any] | None,
    state: str,
) -> ConditionOutcome:
    """See the module docstring."""
    identity = _mapping(campaign)
    text = identity.get("condition")
    role = identity.get("bench_role")
    if not isinstance(text, str) and isinstance(role, str) and role in BENCH_ROLES:
        run_id = identity.get("run_id")
        return ConditionOutcome(
            None,
            {
                "bench_role": role,
                "run_id": run_id[:64] if isinstance(run_id, str) else None,
            },
        )
    if not isinstance(text, str):
        return _NOT_A_RUN
    try:
        condition = parse_condition(text)
    except ConditionError:
        return ConditionOutcome(
            None,  # never store text that is not a canonical condition
            {
                "verified": False,
                "reason": "the stored condition is not valid",
                "session_label": _label(identity),
            },
            failed=True,
        )
    canonical = condition.canonical()
    facts = _mapping(identity.get("condition_facts"))
    report = _mapping(_mapping(workload).get("condition"))
    done = state == AttemptState.DONE.value
    measured: dict[str, Any] = {}
    reason: str | None = None
    failed = False

    if condition.needs_agent:
        if not report:
            verified = False
            reason = "no condition report from the agent"
            failed = done
        elif report.get("spec") != canonical:
            verified = False
            reason = "the agent reported another condition"
            failed = True
        else:
            verified = report.get("verified") is True
            reason = (
                report.get("reason") if isinstance(report.get("reason"), str) else None
            )
            failed = not verified
            if isinstance(report.get("measured"), Mapping):
                measured["agent"] = dict(report["measured"])
            measured["reverted"] = report.get("reverted") is True
    elif isinstance(condition, QueueCondition):
        ahead = facts.get("fillers_ahead")
        counted = isinstance(ahead, int) and not isinstance(ahead, bool)
        verified = counted and isinstance(ahead, int) and ahead >= condition.jobs
        if not verified:
            reason = (
                f"{ahead} filler job(s) were ahead of the run, {condition.jobs} asked"
                if counted
                else "the filler jobs were not found"
            )
        failed = done and not verified
    elif isinstance(condition, Unavailable):
        after = facts.get("dispatched_after_s")
        lost = facts.get("pause_lost") is True
        numeric = isinstance(after, int | float) and not isinstance(after, bool)
        verified = (
            numeric
            and isinstance(after, int | float)
            and after < condition.seconds
            and not lost
        )
        if lost:
            reason = "the controller restarted during the pause (it lives in memory)"
        elif not verified:
            if after is None:
                reason = "the run was not dispatched"
            elif numeric:  # never format a stored value that is not a number
                reason = (
                    f"dispatched {after:g} s after the pause began "
                    f"(it lasted {condition.seconds} s)"
                )
            else:
                reason = "the run's dispatch time is not valid"
        failed = done and not verified
    else:  # none
        verified = True
    if facts:
        measured["controller"] = dict(facts)
    measured = {
        "verified": bool(verified),
        "reason": reason,
        **measured,
        "session_label": _label(identity),
    }
    return ConditionOutcome(canonical, measured, failed=failed)
