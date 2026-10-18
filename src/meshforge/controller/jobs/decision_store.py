"""Reading ``plan_decision`` back, with what actually happened (V0.6 G2, D112).

:class:`DecisionReads` adds one bounded read to the job store: a job's
decisions (optionally of one attempt number), oldest first, each joined to its
attempt - the node it ran on, its state and failure type, and its five phase
timings (``attempt_phase``, never copied into the decision). ``compute
explain`` prints predicted terms next to these actual phases.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select

from meshforge.controller.jobs.store import JobStore, typed_store_call
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow
from meshforge.controller.store.models.plan import PlanDecision

__all__ = [
    "MAX_DECISIONS",
    "ActualOutcome",
    "DecisionReads",
    "DecisionRecordOut",
]

MAX_DECISIONS: Final = 500


@dataclass(frozen=True, slots=True)
class ActualOutcome:
    attempt_id: str
    node_id: str
    state: str
    failure_type: str | None
    phases_ms: Mapping[str, int | None] = field(default_factory=dict)
    """phase -> duration in ms; None = the phase never ran (skipped)."""


@dataclass(frozen=True, slots=True)
class DecisionRecordOut:
    id: int
    attempt_no: int
    attempt_id: str | None
    outcome: str
    policy: str
    chosen_node_id: str | None
    created_at: dt.datetime
    snapshot_digest: str
    reason_key: str
    candidates: Sequence[Mapping[str, Any]]
    actual: ActualOutcome | None = None
    shadow_chosen_node_id: str | None = None
    """V0.9 G3 (D215): a pinned job's would-have-chosen node."""
    shadow_estimate_s: float | None = None


class DecisionReads(JobStore):
    """:class:`JobStore` plus the decision read."""

    @typed_store_call
    async def decisions(
        self, job_id: str, *, attempt_no: int | None = None, limit: int = 50
    ) -> list[DecisionRecordOut]:
        """The latest ``limit`` decisions of ``job_id``, returned oldest first."""
        limit = max(1, min(limit, MAX_DECISIONS))
        query = (
            select(PlanDecision)
            .where(PlanDecision.job_id == job_id)
            .order_by(PlanDecision.id.desc())
            .limit(limit)
        )
        if attempt_no is not None:
            query = query.where(PlanDecision.attempt_no == attempt_no)
        async with self._sessions() as db:
            rows = list(await db.scalars(query))
            rows.reverse()
            attempt_ids = [r.attempt_id for r in rows if r.attempt_id]
            actual: dict[str, ActualOutcome] = {}
            if attempt_ids:
                attempts = {
                    a.id: a
                    for a in await db.scalars(
                        select(Attempt).where(Attempt.id.in_(attempt_ids))
                    )
                }
                phases: dict[str, dict[str, int | None]] = {}
                for p in await db.scalars(
                    select(AttemptPhaseRow).where(
                        AttemptPhaseRow.attempt_id.in_(attempt_ids)
                    )
                ):
                    phases.setdefault(p.attempt_id, {})[p.phase] = (
                        None if p.skipped else int(p.duration_ms)
                    )
                for attempt_id, a in attempts.items():
                    actual[attempt_id] = ActualOutcome(
                        attempt_id=attempt_id,
                        node_id=a.node_id,
                        state=a.state,
                        failure_type=a.failure_type,
                        phases_ms=phases.get(attempt_id, {}),
                    )
            return [
                DecisionRecordOut(
                    id=int(r.id),
                    attempt_no=int(r.attempt_no),
                    attempt_id=r.attempt_id,
                    outcome=r.outcome,
                    policy=r.policy,
                    chosen_node_id=r.chosen_node_id,
                    created_at=r.created_at,
                    snapshot_digest=r.snapshot_digest,
                    reason_key=r.reason_key,
                    candidates=list(r.candidates),
                    actual=None if r.attempt_id is None else actual.get(r.attempt_id),
                    shadow_chosen_node_id=r.shadow_chosen_node_id,
                    shadow_estimate_s=r.shadow_estimate_s,
                )
                for r in rows
            ]
