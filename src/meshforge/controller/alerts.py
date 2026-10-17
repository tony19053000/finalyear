"""Operator alerts: the audit rows an admin should see (V0.8 G3, D180).

An alert is not a new table: it is an ``audit_event`` row whose action is in
:data:`ALERT_KINDS` (today only ``attempt_wrong_node``, D181 - other security
refusals join by adding their action here). The audit row stays the source of
truth; :class:`AlertLog` reads it newest first for ``GET /v1/alerts``, and the
live ``alert`` event published for the same refusal carries the row's ``id``
(``jobs/dispatcher.py``), so the dashboard merges the list and the live events
by id with no duplicates.

Keyset paging over the row id (an identity column: strictly increasing in
insertion order), never an offset.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.store.models.enrollment import AuditAction, AuditEvent

__all__ = ["ALERT_KINDS", "AlertLog", "AlertLogError", "AlertRecord", "alert_kind"]

ALERT_KINDS: Final = MappingProxyType(
    {
        AuditAction.ATTEMPT_WRONG_NODE.value: "wrong_node",
        # V0.9 G1 (D213): a node's clock offset is untrustworthy.
        AuditAction.CLOCK_UNCERTAIN.value: "clock_uncertain",
    }
)
"""``audit_event.action`` -> the alert ``kind`` (the same word the live event uses).

An alert's ``details`` (the audit row's ``details`` that ``GET /v1/alerts``
returns, and the data of the live ``alert`` event) go to every admin's browser
and stay in the audit table: they carry **ids, states and node names only -
never a token, password, join code, secret value, cookie, key or certificate**
(D195). Adding an action here means its ``audit(...)`` details must be a dict
literal with plain keys; ``tests/unit/controller/test_v08_g5_alert_details.py``
checks every alert producer for secret-looking keys and fails otherwise."""


def alert_kind(action: str) -> str:
    return ALERT_KINDS.get(action, action)


class AlertLogError(MeshForgeError):
    """The database refused or lost the read (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: int
    at: dt.datetime
    kind: str
    actor: str
    node_id: str | None
    details: dict[str, Any]


class AlertLog:
    """Reads alerts from ``audit_event`` (one statement per page)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list(self, *, limit: int, before: int | None = None) -> list[AlertRecord]:
        """Up to ``limit`` alerts, newest first, all with ``id < before``."""
        query = (
            select(AuditEvent)
            .where(AuditEvent.action.in_(tuple(ALERT_KINDS)))
            .order_by(AuditEvent.id.desc())
            .limit(limit)
        )
        if before is not None:
            query = query.where(AuditEvent.id < before)
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(query)).all()
        except SQLAlchemyError as exc:
            raise AlertLogError(
                "cannot read the alerts", details={"cause": type(exc).__name__}
            ) from exc
        return [
            AlertRecord(
                id=int(row.id),
                at=row.at,
                kind=alert_kind(row.action),
                actor=row.actor,
                node_id=row.node_id,
                details=dict(row.details or {}),
            )
            for row in rows
        ]
