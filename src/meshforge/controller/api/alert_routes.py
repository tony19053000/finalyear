"""``GET /v1/alerts`` - operator alerts, newest first (V0.8 G3, D180).

**Admin only** (403 for a user, like the ``alert`` events on ``/v1/events/ws``).
The source is the ``audit_event`` rows of :data:`~meshforge.controller.alerts.ALERT_KINDS`
(:mod:`meshforge.controller.alerts`); the dashboard's Overview shows this list
and merges the live ``alert`` events into it by ``id``.

Query: ``limit`` (1-200, default 50) and ``before`` - the previous page's
``next_cursor``, i.e. the id of its oldest alert (decimal digits). Anything
that is not such an id is 400 ``invalid_input``, never "start from the top".
Database down -> 503 ``network_error`` (``Retry-After``).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request

from meshforge.common.errors import FailureType
from meshforge.controller.alerts import AlertLog, AlertLogError, AlertRecord
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc

__all__ = [
    "ALERTS_STATE_KEY",
    "DEFAULT_ALERT_LIMIT",
    "MAX_ALERT_LIMIT",
    "AlertList",
    "AlertOut",
    "alert_router",
]

ALERTS_STATE_KEY: Final = "meshforge_alerts"
DEFAULT_ALERT_LIMIT: Final = 50
MAX_ALERT_LIMIT: Final = 200
_MAX_CURSOR_DIGITS: Final = 19
_MAX_ID: Final = (1 << 63) - 1

alert_router = APIRouter(prefix="/v1", tags=["alerts"])


class AlertOut(ApiModel):
    id: str
    """The audit row id (decimal string): the key live ``alert`` events share."""
    at: Utc
    kind: str
    """``wrong_node``: a node reported status for an attempt placed on another node."""
    node_id: str | None
    """The node the refusal is about (for ``wrong_node``: the reporting node)."""
    actor: str
    details: dict[str, Any]


class AlertList(ApiModel):
    alerts: tuple[AlertOut, ...]
    count: int
    next_cursor: str | None = None
    """Pass as ``before`` for the next (older) page; null = no more."""


def get_alert_log(request: Request) -> AlertLog:
    found: AlertLog | None = getattr(request.app.state, ALERTS_STATE_KEY, None)
    if found is None:
        raise ApiError(FailureType.CONFIG_ERROR, "alerts are not configured")
    return found


def _cursor(before: str | None) -> int | None:
    if before is None:
        return None
    bad = ApiError.invalid("before is not a cursor from this list's next_cursor")
    if not before.isascii() or not before.isdigit() or len(before) > _MAX_CURSOR_DIGITS:
        raise bad
    value = int(before)
    if value < 1 or value > _MAX_ID:
        raise bad
    return value


def _out(record: AlertRecord) -> AlertOut:
    return AlertOut(
        id=str(record.id),
        at=record.at,
        kind=record.kind,
        node_id=record.node_id,
        actor=record.actor,
        details=record.details,
    )


@alert_router.get(
    "/alerts",
    response_model=AlertList,
    summary="Operator alerts (security refusals), newest first - admin only",
)
async def list_alerts(
    _admin: AdminUser,
    log: Annotated[AlertLog, Depends(get_alert_log)],
    limit: Annotated[
        int, Query(ge=1, le=MAX_ALERT_LIMIT, description="Alerts per page.")
    ] = DEFAULT_ALERT_LIMIT,
    before: Annotated[
        str | None,
        Query(max_length=40, description="The previous page's next_cursor."),
    ] = None,
) -> AlertList:
    start = _cursor(before)
    try:
        records = await log.list(limit=limit + 1, before=start)  # one more: next page?
    except AlertLogError:
        raise ApiError(
            FailureType.NETWORK_ERROR,
            "the database is unavailable; retry",
            headers={"Retry-After": "2"},
        ) from None
    page = records[:limit]
    more = len(records) > limit
    return AlertList(
        alerts=tuple(_out(r) for r in page),
        count=len(page),
        next_cursor=str(page[-1].id) if more and page else None,
    )
