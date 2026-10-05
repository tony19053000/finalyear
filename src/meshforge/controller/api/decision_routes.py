"""``GET /v1/jobs/{job_id}/decisions`` - what the planner saw and chose (V0.6
G2, D112; roadmap 0.6.5, criterion 3).

Fabric token required (like every ``/v1/jobs*`` read). Answer: the job's
decisions oldest first (at most ``limit``, the newest ones; ``attempt=N`` only
those for attempt number N), each with **every** candidate - chosen, ranked,
rejected - its rejection code and text, its predicted terms with their
``source`` (``measured`` / ``history`` / ``scaled`` / ``assumed``: nothing
assumed is shown as measured, checklist 18), the policy's score and rank; and,
for a decision that placed an attempt, what actually happened (node, state,
failure type, the five phase timings). ``compute explain`` prints this.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Path, Query
from pydantic import ConfigDict, Field

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_job_id
from meshforge.controller.api.auth import FabricTokenRequired
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import Jobs
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.jobs.decision_store import (
    MAX_DECISIONS,
    DecisionReads,
    DecisionRecordOut,
)

__all__ = ["DecisionList", "DecisionOut", "decision_router"]

DEFAULT_DECISION_LIMIT: Final = 50
_JOB_ID_MAX: Final = 40

decision_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


class _Stored(ApiModel):
    """Read back from our own JSON: tolerate keys a later version adds."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class TermOut(_Stored):
    seconds: float
    source: Literal["measured", "history", "scaled", "assumed"]
    detail: dict[str, Any] = Field(default_factory=dict)


class EstimateOut(_Stored):
    total_s: float
    terms: dict[str, TermOut]


class CandidateOut(_Stored):
    node_id: str
    mode: Literal["local", "offload"]
    feasible: bool
    slot: Literal["cpu", "gpu"] | None = None
    reject_code: str | None = None
    reject_text: str | None = None
    estimate: EstimateOut | None = None
    score: float | None = None
    rank: int | None = None


class ActualOut(ApiModel):
    attempt_id: str
    node_id: str
    state: str
    failure_type: str | None = None
    phases_ms: dict[str, int | None] = Field(default_factory=dict)
    """queue / stage / env / run / collect in ms; null = the phase never ran."""


class DecisionOut(ApiModel):
    id: int
    attempt_no: int
    attempt_id: str | None
    outcome: Literal["dispatched", "waiting", "failed"]
    policy: str
    chosen_node_id: str | None
    created_at: Utc
    snapshot_digest: str
    candidates: tuple[CandidateOut, ...]
    actual: ActualOut | None = None


class DecisionList(ApiModel):
    job_id: str
    decisions: tuple[DecisionOut, ...]
    count: int


def _out(record: DecisionRecordOut) -> DecisionOut:
    actual = record.actual
    return DecisionOut(
        id=record.id,
        attempt_no=record.attempt_no,
        attempt_id=record.attempt_id,
        outcome=record.outcome,  # type: ignore[arg-type]  # CHECK-constrained
        policy=record.policy,
        chosen_node_id=record.chosen_node_id,
        created_at=record.created_at,
        snapshot_digest=record.snapshot_digest,
        candidates=tuple(CandidateOut.model_validate(c) for c in record.candidates),
        actual=None
        if actual is None
        else ActualOut(
            attempt_id=actual.attempt_id,
            node_id=actual.node_id,
            state=actual.state,
            failure_type=actual.failure_type,
            phases_ms=dict(actual.phases_ms),
        ),
    )


@decision_router.get(
    "/{job_id}/decisions",
    response_model=DecisionList,
    summary="A job's placement decisions: every candidate, reasons, estimates",
)
async def job_decisions(
    _auth: FabricTokenRequired,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
    attempt: Annotated[
        int | None, Query(ge=1, le=1_000_000, description="Only attempt number N.")
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_DECISIONS, description="Newest decisions returned."),
    ] = DEFAULT_DECISION_LIMIT,
) -> DecisionList:
    try:
        checked = str(validate_job_id(job_id))
    except InvalidIdError:
        raise ApiError.invalid("job_id is not a canonical job_<ULID>") from None
    store = jobs.store
    if not isinstance(store, DecisionReads):
        raise ApiError(FailureType.CONFIG_ERROR, "decisions are not configured")
    if await store.get_job(checked) is None:
        raise ApiError.not_found("job")
    records = await store.decisions(checked, attempt_no=attempt, limit=limit)
    decisions = tuple(_out(r) for r in records)
    return DecisionList(job_id=checked, decisions=decisions, count=len(decisions))
