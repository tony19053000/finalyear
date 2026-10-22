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

V0.14 G5 (D300, D320): each term also carries its calibration factor
(``calibration``), a ``note`` (bandwidth origins, cached env, risk p_fail) and,
on the chosen candidate once its attempt ended ``done``, ``actual_s`` and
``ratio`` (the calibration's own term -> phase match; null = no honest phase);
each decision its ``calibration_version``, ``replay_version`` and
``margin_sentence``. Derived at read time by
``controller/jobs/decision_explain.py`` - the same strings the CLI prints.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Path, Query
from pydantic import ConfigDict, Field

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_job_id
from meshforge.controller.api.auth import CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import Jobs
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.jobs.decision_explain import (
    explain_candidates,
    margin_sentence_of,
    term_actuals,
)
from meshforge.controller.jobs.decision_store import (
    MAX_DECISIONS,
    DecisionReads,
    DecisionRecordOut,
)

__all__ = [
    "CandidateOut",
    "DecisionList",
    "DecisionOut",
    "TermCalibrationOut",
    "TermOut",
    "decision_router",
]

DEFAULT_DECISION_LIMIT: Final = 50
_JOB_ID_MAX: Final = 40

decision_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


class _Stored(ApiModel):
    """Read back from our own JSON: tolerate keys a later version adds."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class TermCalibrationOut(_Stored):
    """V0.14 G2 (D293), shown since G5: the factor a term was scaled by."""

    factor: float
    n: int | None = None
    """How many matched runs the factor was fitted on."""
    key_level: str | None = None
    """``node_family`` | ``node`` | ``global``."""
    version: int | None = None
    raw_s: float | None = None
    """The uncalibrated prediction (``seconds`` = ``raw_s`` x ``factor``,
    capped)."""


class TermOut(_Stored):
    seconds: float
    source: Literal["measured", "history", "scaled", "assumed"]
    detail: dict[str, Any] = Field(default_factory=dict)
    calibration: TermCalibrationOut | None = None
    """V0.14 G5: null = not scaled (uncalibrated, ``measured``, or a row from
    before V0.14 G2)."""
    note: str | None = None
    """V0.14 G5: what ``detail`` adds (``common/explain_text.term_note``)."""
    actual_s: float | None = None
    """V0.14 G5 (D320): the measured seconds of the same work - chosen
    candidate only, after its attempt ended ``done``; null = no honest phase."""
    ratio: float | None = None
    """``actual_s / seconds`` (null without an actual or for a 0 s prediction)."""


class EstimateOut(_Stored):
    total_s: float
    terms: dict[str, TermOut]


class MarginTestOut(_Stored):
    """V0.10 G3 (D265): split only when ``set_s <= threshold_s``."""

    set_s: float | None = None
    best_single_s: float | None = None
    best_single_node_id: str | None = None
    margin: float
    threshold_s: float | None = None
    passed: bool
    forced: str | None = None
    """Why no set was compared: pinned, split_too_small, split_too_few_nodes."""


class PrunedOut(_Stored):
    """V0.14 G3 (D294): an idle node some Mode 3 sets were skipped for."""

    node_id: str
    by: str
    """The node that would finish every item before it could start."""


class CandidateOut(_Stored):
    node_id: str
    """For a ``split`` row: the first (fastest) node of ``node_set``."""
    mode: Literal["local", "offload", "split"]
    feasible: bool
    slot: Literal["cpu", "gpu"] | None = None
    reject_code: str | None = None
    reject_text: str | None = None
    estimate: EstimateOut | None = None
    score: float | None = None
    rank: int | None = None
    # V0.10 G3 - split jobs only (absent for a job that runs whole)
    node_set: tuple[str, ...] | None = None
    """The lanes' nodes, fastest first."""
    chunk_items: int | None = None
    chunks: int | None = None
    rate_items_s: float | None = None
    """Local / offload rows of a split job: the node's predicted items/s."""
    rate_source: Literal["measured", "history", "scaled", "assumed"] | None = None
    start_s: float | None = None
    """Predicted stage + env seconds before the node's first chunk."""
    inactive: tuple[str, ...] | None = None
    """``split`` rows: nodes of the set predicted to get no chunk."""
    margin_test: MarginTestOut | None = None
    pruned: tuple[PrunedOut, ...] | None = None
    """V0.14 G3 (D294): split-job rows - idle nodes whose sets were skipped
    as strictly dominated (absent when none was)."""


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
    shadow_chosen_node_id: str | None = None
    """V0.9 G3 (D215): for a pinned job, the node the planner would have chosen
    unpinned on the same snapshot; null = not pinned / none feasible."""
    shadow_estimate_s: float | None = None
    objective: str | None = None
    """V0.14 G3 (D295): the job's resolved objective the decision ranked by
    (``placement.objective`` > manifest > ``default_objective``); read only by
    ``cost_model``. Null for a decision recorded before V0.14."""
    calibration_version: int | None = None
    """V0.14 G5 (D300): the calibration version its estimates were scaled by;
    null = uncalibrated."""
    replay_version: int | None = None
    """V0.14 G5 (D300): the stored replay snapshot's format version; null =
    the decision cannot be replayed (pre-V0.14 row, or over
    ``replay_max_bytes``)."""
    margin_sentence: str | None = None
    """V0.14 G5 (D300): why a split job was (not) split - the margin test, the
    objective, pruned nodes; null for a job that runs whole."""


class DecisionList(ApiModel):
    job_id: str
    decisions: tuple[DecisionOut, ...]
    count: int


def _out(record: DecisionRecordOut) -> DecisionOut:
    actual = record.actual
    candidates = explain_candidates(
        record.candidates,
        chosen_node_id=record.chosen_node_id,
        actuals=term_actuals(record),
    )
    return DecisionOut(
        id=record.id,
        attempt_no=record.attempt_no,
        attempt_id=record.attempt_id,
        outcome=record.outcome,  # type: ignore[arg-type]  # CHECK-constrained
        policy=record.policy,
        chosen_node_id=record.chosen_node_id,
        created_at=record.created_at,
        snapshot_digest=record.snapshot_digest,
        candidates=tuple(CandidateOut.model_validate(c) for c in candidates),
        actual=None
        if actual is None
        else ActualOut(
            attempt_id=actual.attempt_id,
            node_id=actual.node_id,
            state=actual.state,
            failure_type=actual.failure_type,
            phases_ms=dict(actual.phases_ms),
        ),
        shadow_chosen_node_id=record.shadow_chosen_node_id,
        shadow_estimate_s=record.shadow_estimate_s,
        objective=record.objective,
        calibration_version=record.calibration_version,
        replay_version=record.replay_version,
        margin_sentence=margin_sentence_of(record),
    )


@decision_router.get(
    "/{job_id}/decisions",
    response_model=DecisionList,
    summary="A job's placement decisions: every candidate, reasons, estimates",
)
async def job_decisions(
    user: CurrentUser,
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
    job = await store.get_job(checked)
    if job is None or not user.may_see_job(job.owner_user_id):
        raise ApiError.not_found("job")
    records = await store.decisions(checked, attempt_no=attempt, limit=limit)
    decisions = tuple(_out(r) for r in records)
    return DecisionList(job_id=checked, decisions=decisions, count=len(decisions))
