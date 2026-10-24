"""``GET /v1/planner/decisions/{decision_id}/replay`` - re-plan one stored
decision and say whether it reproduces (V0.15 G6a; D364, Q23; the
production caller of ``ml/replay.replay_decision``).

**Admin only** (403 for a user): the replay holds the whole cluster snapshot
the planner saw (every node, every user's queue). The controller re-plans the
decision from its stored replay - with the calibration it used and, for an ML
decision, the model set it used, loaded by version from the registry - and
compares the candidates field for field
(:mod:`meshforge.controller.ml.decision_replay`). Status ``reproduced`` /
``differs`` (with the first differing paths) / ``ml_model_unavailable`` (the
set is gone: **never** silently re-planned with calibration) /
``no_replay`` / ``replay_unreadable``. Unknown id -> 404; database down (or a
model set load over ``ml_replay_load_timeout_s``) ->
503 ``network_error``. ``compute planner replay ID`` calls this route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Request

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel
from meshforge.controller.ml.decision_replay import (
    LOAD_TIMEOUT,
    DecisionReplayer,
    DecisionReplayError,
    ReplayStatus,
)

__all__ = [
    "DECISION_REPLAY_STATE_KEY",
    "DecisionReplayApi",
    "DecisionReplayOut",
    "ReplayStatus",
    "decision_replay_router",
]

log = get_logger(__name__)

DECISION_REPLAY_STATE_KEY: Final = "meshforge_decision_replay"

decision_replay_router = APIRouter(prefix="/v1", tags=["planner"])


@dataclass(frozen=True, slots=True)
class DecisionReplayApi:
    """What the route needs (on ``app.state``)."""

    replayer: DecisionReplayer


def get_decision_replay_api(request: Request) -> DecisionReplayApi:
    api: DecisionReplayApi | None = getattr(
        request.app.state, DECISION_REPLAY_STATE_KEY, None
    )
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "decision replay is not configured")
    return api


class DecisionReplayOut(ApiModel):
    decision_id: int
    job_id: str
    outcome: str
    policy: str
    status: ReplayStatus
    ml_model_version: str | None
    """The model set the stored decision was planned with (null = none)."""
    stored_chosen_node_id: str | None
    replayed_chosen_node_id: str | None
    snapshot_digest_match: bool | None
    """null = not re-planned."""
    differences: list[str]
    """``path: stored != replayed``, the first ones in document order."""
    differences_total: int


@decision_replay_router.get(
    "/planner/decisions/{decision_id}/replay",
    response_model=DecisionReplayOut,
    summary="Re-plan a stored decision and compare it - admin only",
)
async def get_decision_replay(
    admin: AdminUser,
    api: Annotated[DecisionReplayApi, Depends(get_decision_replay_api)],
    decision_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
) -> DecisionReplayOut:
    try:
        found = await api.replayer.replay(decision_id)
    except DecisionReplayError as exc:
        slow = exc.details.get("cause") == LOAD_TIMEOUT
        raise ApiError(
            FailureType.NETWORK_ERROR,
            f"{exc.message}; retry" if slow else "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    if found is None:
        raise ApiError(FailureType.NOT_FOUND, f"no decision {decision_id}")
    log.info(
        "controller.planner.decision_replayed",
        user_id=admin.user_id,
        decision_id=decision_id,
        job_id=found.job_id,
        status=found.status,
        ml_set_version=found.ml_model_version,
        differences=found.differences_total,
    )
    return DecisionReplayOut(
        decision_id=found.decision_id,
        job_id=found.job_id,
        outcome=found.outcome,
        policy=found.policy,
        status=found.status,
        ml_model_version=found.ml_model_version,
        stored_chosen_node_id=found.stored_chosen_node_id,
        replayed_chosen_node_id=found.replayed_chosen_node_id,
        snapshot_digest_match=found.snapshot_digest_match,
        differences=list(found.differences),
        differences_total=found.differences_total,
    )
