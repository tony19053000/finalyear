"""Invites (join tokens) in PostgreSQL and the audit trail (V0.7 G2, D135, D151).

* :meth:`InviteStore.create` - a fresh 128-bit token (``secrets``); the row keeps
  only its SHA-256. ``created_at`` / ``expires_at`` come from the **database**
  clock, the same clock the redeeming ``UPDATE`` compares against, so a laptop's
  or controller's wrong wall clock cannot stretch a code's life.
* :meth:`InviteStore.list` / :meth:`InviteStore.revoke` - for ``fabric invites``.
  Revoking a used or already revoked invite is a typed ``conflict``.
* :func:`redeem` - the single-use ``UPDATE ... RETURNING`` the enrolment
  transaction runs (``controller/enrollment/service.py``); :func:`why_unusable`
  names the reason when it matched nothing.
* :func:`audit` - one ``audit_event`` row; ``details`` never carries a token or
  a code (the caller passes ids and reason codes only).

Every DB failure is a :class:`InviteStoreError` (``network_error``, retryable).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Interval, func, insert, literal, select, update
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common import join_code
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.ids import InviteId, new_invite_id, validate_invite_id
from meshforge.common.logging import get_logger
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import (
    AuditAction,
    AuditEvent,
    JoinToken,
)

__all__ = [
    "DEFAULT_TTL_S",
    "MAX_LISTED",
    "MAX_TTL_S",
    "MIN_TTL_S",
    "InviteConflictError",
    "InviteError",
    "InviteNotFoundError",
    "InviteRecord",
    "InviteState",
    "InviteStore",
    "InviteStoreError",
    "NewInvite",
    "audit",
    "check_ttl",
    "redeem",
    "why_unusable",
]

log = get_logger(__name__)

MIN_TTL_S: Final = 60
MAX_TTL_S: Final = 24 * 3600
DEFAULT_TTL_S: Final = 15 * 60
MAX_LISTED: Final = 500
MAX_ACTOR_CHARS: Final = 64


class InviteStoreError(MeshForgeError):
    """The database refused or lost an invite read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class InviteError(MeshForgeError):
    """An invite request the caller can fix (bad TTL, malformed id)."""

    default_failure_type = FailureType.INVALID_INPUT
    exit_code = ExitCode.USAGE_ERROR


class InviteNotFoundError(InviteError):
    """No invite with that id."""

    default_failure_type = FailureType.NOT_FOUND
    exit_code = ExitCode.FAILURE


class InviteConflictError(InviteError):
    """The invite is already used or revoked."""

    default_failure_type = FailureType.CONFLICT
    exit_code = ExitCode.CONFLICT


class InviteState(StrEnum):
    PENDING = "pending"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InviteRecord:
    """One invite as listed (never the token)."""

    id: InviteId
    state: InviteState
    created_by: str
    created_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None
    used_by_node: str | None
    revoked_at: dt.datetime | None
    replace_node_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "used_by_node": self.used_by_node,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "replace_node_id": self.replace_node_id,
        }


@dataclass(frozen=True, slots=True)
class NewInvite:
    """A freshly created invite: the record plus the token (shown once)."""

    record: InviteRecord
    token: bytes


def _hint(exc: BaseException) -> str:
    if isinstance(exc, ProgrammingError):  # e.g. "relation join_token does not exist"
        return " (is the database migrated? start the controller once)"
    if isinstance(exc, DBAPIError | OSError):
        return " (is the database running and reachable?)"
    return ""


def _state(row: JoinToken, now: dt.datetime) -> InviteState:
    if row.used_at is not None:
        return InviteState.USED
    if row.revoked_at is not None:
        return InviteState.REVOKED
    if row.expires_at <= now:
        return InviteState.EXPIRED
    return InviteState.PENDING


def _record(row: JoinToken, now: dt.datetime) -> InviteRecord:
    return InviteRecord(
        id=InviteId(row.id),
        state=_state(row, now),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        used_by_node=row.used_by_node,
        revoked_at=row.revoked_at,
        replace_node_id=row.replace_node_id,
    )


def check_ttl(ttl_s: int) -> int:
    """``ttl_s`` within 1 minute .. 24 hours (D135), else :class:`InviteError`."""
    if isinstance(ttl_s, bool) or not MIN_TTL_S <= ttl_s <= MAX_TTL_S:
        raise InviteError(
            f"an invite lives {MIN_TTL_S} s (1 min) to {MAX_TTL_S} s (24 h); "
            f"got {ttl_s}",
            details={"reason": "bad_ttl"},
        )
    return ttl_s


async def _db_now(db: AsyncSession) -> dt.datetime:
    value = await db.scalar(select(func.now()))
    if not isinstance(value, dt.datetime):  # pragma: no cover - driver contract
        raise InviteStoreError("the database returned no time")
    return value


async def audit(
    db: AsyncSession,
    action: AuditAction,
    *,
    actor: str,
    node_id: str | None = None,
    invite_id: str | None = None,
    source: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Add one ``audit_event`` row to ``db``'s transaction (no token, no code)
    and return it (its ``id`` is set once the session flushes)."""
    event = AuditEvent(
        action=action.value,
        actor=actor[:MAX_ACTOR_CHARS],
        node_id=node_id,
        invite_id=invite_id,
        source=source[:128] if source else None,
        details=dict(details or {}),
    )
    db.add(event)
    return event


async def redeem(
    db: AsyncSession, token_hash: str, *, node_id: str
) -> tuple[str, str | None] | None:
    """Mark the invite with ``token_hash`` used by ``node_id``; ``(invite_id,
    replace_node_id)`` or ``None`` when no usable invite matched.

    One statement: ``UPDATE join_token SET used_at = now(), used_by_node = :node
    WHERE token_hash = :h AND used_at IS NULL AND revoked_at IS NULL AND
    expires_at > now() RETURNING id, replace_node_id``. The row lock makes it
    single-use under any number of concurrent callers; it takes effect only if
    the caller's transaction commits (a refused enrolment rolls it back).
    """
    result = await db.execute(
        update(JoinToken)
        .where(
            JoinToken.token_hash == token_hash,
            JoinToken.used_at.is_(None),
            JoinToken.revoked_at.is_(None),
            JoinToken.expires_at > func.now(),
        )
        .values(used_at=func.now(), used_by_node=node_id)
        .returning(JoinToken.id, JoinToken.replace_node_id)
        .execution_options(synchronize_session=False)
    )
    row = result.one_or_none()
    return None if row is None else (str(row[0]), row[1])


async def why_unusable(db: AsyncSession, token_hash: str) -> tuple[str, str | None]:
    """After :func:`redeem` matched nothing: ``(reason, invite_id)`` with reason
    ``token_unknown`` / ``token_used`` / ``token_revoked`` / ``token_expired``."""
    row = await db.scalar(select(JoinToken).where(JoinToken.token_hash == token_hash))
    if row is None:
        return "token_unknown", None
    state = _state(row, await _db_now(db))
    reason = {
        InviteState.USED: "token_used",
        InviteState.REVOKED: "token_revoked",
        InviteState.EXPIRED: "token_expired",
    }.get(state, "token_expired")  # PENDING here = it expired between the two reads
    return reason, row.id


class InviteStore:
    """Async repository over ``join_token`` (+ the audit rows it writes)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    async def create(
        self,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        actor: str,
        replace_node_id: str | None = None,
        source: str | None = None,
    ) -> NewInvite:
        """A new invite; the token is returned once and never stored."""
        check_ttl(ttl_s)
        token = join_code.new_token()
        invite_id = new_invite_id()
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(
                    insert(JoinToken).values(
                        id=invite_id,
                        token_hash=join_code.token_hash(token),
                        created_by=actor[:MAX_ACTOR_CHARS],
                        created_at=func.now(),
                        expires_at=func.now()
                        + literal(dt.timedelta(seconds=ttl_s), Interval()),
                        replace_node_id=replace_node_id,
                    )
                )
                stored = await db.scalar(
                    select(JoinToken).where(JoinToken.id == invite_id)
                )
                if stored is None:  # pragma: no cover - same transaction
                    raise InviteStoreError("the new invite vanished")
                await audit(
                    db,
                    AuditAction.INVITE_CREATED,
                    actor=actor,
                    invite_id=invite_id,
                    node_id=replace_node_id,
                    source=source,
                    details={"ttl_s": ttl_s, "replace": replace_node_id is not None},
                )
                record = _record(stored, stored.created_at)
        except (SQLAlchemyError, OSError) as exc:
            raise InviteStoreError(
                f"cannot store the invite: {type(exc).__name__}{_hint(exc)}",
                details={"cause": type(exc).__name__},
            ) from exc
        log.info(
            "controller.invite.created",
            invite_id=invite_id,
            expires_at=record.expires_at.isoformat(),
            replace_node_id=replace_node_id,
            actor=actor,
        )
        return NewInvite(record=record, token=token)

    async def list(self, *, limit: int = MAX_LISTED) -> list[InviteRecord]:
        """Newest first (``id`` is a ULID, so it sorts by creation time)."""
        try:
            async with self._sessions() as db:
                now = await _db_now(db)
                rows = await db.scalars(
                    select(JoinToken)
                    .order_by(JoinToken.id.desc())
                    .limit(min(max(limit, 1), MAX_LISTED))
                )
                return [_record(row, now) for row in rows]
        except (SQLAlchemyError, OSError) as exc:
            raise InviteStoreError(
                f"cannot read invites: {type(exc).__name__}{_hint(exc)}",
                details={"cause": type(exc).__name__},
            ) from exc

    async def revoke(
        self, invite_id: str, *, actor: str, source: str | None = None
    ) -> InviteRecord:
        """Revoke a pending (or expired) invite. Used / already revoked ->
        ``conflict``; unknown -> ``not_found``."""
        try:
            validate_invite_id(invite_id)
        except MeshForgeError:
            raise InviteError(
                f"{invite_id!r} is not an invite id (inv_...)",
                details={"reason": "bad_id"},
            ) from None
        try:
            async with self._sessions() as db, db.begin():
                row = await db.scalar(
                    select(JoinToken).where(JoinToken.id == invite_id).with_for_update()
                )
                if row is None:
                    raise InviteNotFoundError(
                        f"no invite {invite_id}", details={"reason": "unknown"}
                    )
                if row.used_at is not None or row.revoked_at is not None:
                    state = "used" if row.used_at is not None else "revoked"
                    raise InviteConflictError(
                        f"invite {invite_id} is already {state}",
                        details={"reason": state},
                    )
                row.revoked_at = await _db_now(db)
                await audit(
                    db,
                    AuditAction.INVITE_REVOKED,
                    actor=actor,
                    invite_id=invite_id,
                    source=source,
                )
                await db.flush()
                record = _record(row, row.revoked_at)
        except (SQLAlchemyError, OSError) as exc:
            raise InviteStoreError(
                f"cannot revoke the invite: {type(exc).__name__}{_hint(exc)}",
                details={"cause": type(exc).__name__},
            ) from exc
        log.info("controller.invite.revoked", invite_id=invite_id, actor=actor)
        return record
