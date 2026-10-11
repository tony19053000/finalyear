"""Users and API tokens in PostgreSQL (V0.7 G4, roadmap 0.7.6, D141).

:class:`UserStore` is the only code that reads or writes ``app_user`` and
``api_token`` (+ their ``audit_event`` rows):

* users are created / disabled / enabled / given a new password on the
  controller host (``meshforge-controller user ...``); a disabled user and a
  password reset **revoke every live token** of that user in the same
  transaction, so the change takes effect on the very next request;
* :meth:`UserStore.issue_token` stores only the SHA-256 of a fresh 256-bit token
  (:mod:`meshforge.common.api_token`); ``created_at`` / ``expires_at`` come from
  the database clock, the clock :meth:`UserStore.authenticate` compares against;
* :meth:`UserStore.authenticate` - one ``SELECT`` per request (no cache: a
  revoke, an expiry or a disabled user is effective on the next request) for a
  token that is not revoked, not expired, and whose user is enabled; the hash is
  re-compared in constant time; ``last_used_at`` is written at most once per
  :data:`LAST_USED_RESOLUTION_S` per token.

Every database failure is a :class:`UserStoreError` (``network_error``,
retryable) - authentication then **fails closed** with 503, never "no user".
"""

from __future__ import annotations

import datetime as dt
import hmac
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import SecretStr
from sqlalchemy import Interval, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.api_token import api_token_hash, is_api_token, new_api_token
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.ids import new_api_token_id, new_user_id
from meshforge.common.logging import get_logger
from meshforge.controller.auth.principal import Principal
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.auth import (
    MAX_TOKEN_NAME_CHARS,
    USER_NAME_PATTERN,
    ApiToken,
    AppUser,
    UserRole,
)
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.job import Attempt, Job

__all__ = [
    "DEFAULT_TOKEN_NAME",
    "LAST_USED_RESOLUTION_S",
    "MAX_LISTED_TOKENS",
    "LoginRecord",
    "TokenRecord",
    "UserConflictError",
    "UserError",
    "UserNotFoundError",
    "UserRecord",
    "UserStore",
    "UserStoreError",
    "check_user_name",
]

log = get_logger(__name__)

LAST_USED_RESOLUTION_S: Final = 60
MAX_LISTED_TOKENS: Final = 500
MAX_LISTED_USERS: Final = 1000
DEFAULT_TOKEN_NAME: Final = "compute"  # noqa: S105 - a label, not a secret
_NAME_RE: Final = re.compile(USER_NAME_PATTERN)
_LEGACY_LOG_INTERVAL_S: Final = 60.0


class UserStoreError(MeshForgeError):
    """The database refused or lost a user/token read or write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class UserError(MeshForgeError):
    """A request the caller can fix (bad name, bad TTL)."""

    default_failure_type = FailureType.INVALID_INPUT
    exit_code = ExitCode.USAGE_ERROR


class UserNotFoundError(UserError):
    default_failure_type = FailureType.NOT_FOUND
    exit_code = ExitCode.FAILURE


class UserConflictError(UserError):
    default_failure_type = FailureType.CONFLICT
    exit_code = ExitCode.CONFLICT


def check_user_name(name: str) -> str:
    """Lower-case login name, 1-64 of ``a-z 0-9 . _ -`` starting alphanumeric."""
    value = name.strip()
    if _NAME_RE.fullmatch(value) is None:
        raise UserError(
            "a user name is 1-64 characters: lower-case letters, digits, '.', '_' "
            "or '-', starting with a letter or digit",
            details={"reason": "bad_name"},
        )
    return value


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: str
    name: str
    role: UserRole
    disabled: bool
    created_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "disabled": self.disabled,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LoginRecord:
    """What a login needs to check a password (the hash never leaves the store
    except into :class:`~meshforge.controller.auth.passwords.PasswordHasher`)."""

    user_id: str
    name: str
    role: UserRole
    pw_hash: str
    disabled: bool


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One API token as listed (never the token, never its hash)."""

    id: str
    user_id: str
    user_name: str
    name: str
    created_at: dt.datetime
    expires_at: dt.datetime
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    now: dt.datetime

    @property
    def state(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= self.now:
            return "expired"
        return "active"

    def as_dict(self, *, current_token_id: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user": self.user_name,
            "name": self.name,
            "state": self.state,
            "current": self.id == current_token_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat()
            if self.last_used_at
            else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


def _user(row: AppUser) -> UserRecord:
    return UserRecord(
        id=row.id,
        name=row.name,
        role=UserRole(row.role),
        disabled=bool(row.disabled),
        created_at=row.created_at,
    )


def _token(row: ApiToken, user_name: str, now: dt.datetime) -> TokenRecord:
    return TokenRecord(
        id=row.id,
        user_id=row.user_id,
        user_name=user_name,
        name=row.name,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        now=now,
    )


def _store_error(operation: str, exc: BaseException) -> UserStoreError:
    return UserStoreError(
        f"users.{operation} failed: {type(exc).__name__} (is the database running "
        "and migrated?)",
        details={"operation": operation, "cause": type(exc).__name__},
    )


async def _db_now(db: AsyncSession) -> dt.datetime:
    value = await db.scalar(select(func.now()))
    if not isinstance(value, dt.datetime):  # pragma: no cover - driver contract
        raise UserStoreError("the database returned no time")
    return value


class UserStore:
    """Async repository over ``app_user`` / ``api_token`` (+ audit rows)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        self._legacy_audited = False
        self._legacy_logged_at: float | None = None
        self.legacy_uses = 0
        """Calls accepted with the legacy fabric token in this process."""

    # ------------------------------------------------------------- users

    async def create_user(
        self, name: str, *, pw_hash: str, role: UserRole, actor: str
    ) -> UserRecord:
        checked = check_user_name(name)
        user_id = new_user_id()
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(
                    insert(AppUser).values(
                        id=user_id, name=checked, role=role.value, pw_hash=pw_hash
                    )
                )
                await audit(
                    db,
                    AuditAction.USER_CREATED,
                    actor=actor,
                    details={"user_id": user_id, "role": role.value},
                )
                row = await db.get_one(AppUser, user_id)
                record = _user(row)
        except IntegrityError:
            raise UserConflictError(
                f"a user named {checked!r} already exists",
                details={"reason": "name_taken"},
            ) from None
        except SQLAlchemyError as exc:
            raise _store_error("create_user", exc) from exc
        log.info("controller.users.created", user_id=user_id, role=role.value)
        return record

    async def list_users(self) -> list[UserRecord]:
        try:
            async with self._sessions() as db:
                rows = await db.scalars(
                    select(AppUser).order_by(AppUser.name).limit(MAX_LISTED_USERS)
                )
                return [_user(row) for row in rows]
        except SQLAlchemyError as exc:
            raise _store_error("list_users", exc) from exc

    async def _locked_user(self, db: AsyncSession, name: str) -> AppUser:
        row = await db.scalar(
            select(AppUser)
            .where(AppUser.name == check_user_name(name))
            .with_for_update()
        )
        if row is None:
            raise UserNotFoundError("no such user", details={"reason": "no_user"})
        return row

    async def _revoke_all(self, db: AsyncSession, user_id: str) -> int:
        result = await db.execute(
            update(ApiToken)
            .where(
                ApiToken.user_id == user_id,
                ApiToken.revoked_at.is_(None),
                ApiToken.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
            .returning(ApiToken.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())

    async def set_disabled(
        self, name: str, *, disabled: bool, actor: str
    ) -> tuple[UserRecord, int]:
        """Disable (revoking every live token) or enable a user; ``(record,
        tokens revoked)``. Idempotent."""
        try:
            async with self._sessions() as db, db.begin():
                row = await self._locked_user(db, name)
                revoked = await self._revoke_all(db, row.id) if disabled else 0
                if bool(row.disabled) != disabled:
                    row.disabled = disabled
                    await audit(
                        db,
                        AuditAction.USER_DISABLED
                        if disabled
                        else AuditAction.USER_ENABLED,
                        actor=actor,
                        details={"user_id": row.id, "tokens_revoked": revoked},
                    )
                await db.flush()
                record = _user(row)
        except UserError:
            raise
        except SQLAlchemyError as exc:
            raise _store_error("set_disabled", exc) from exc
        log.info(
            "controller.users.disabled" if disabled else "controller.users.enabled",
            user_id=record.id,
            tokens_revoked=revoked,
        )
        return record, revoked

    async def reset_password(
        self, name: str, *, pw_hash: str, actor: str
    ) -> tuple[UserRecord, int]:
        """New password; every live token of the user is revoked with it."""
        try:
            async with self._sessions() as db, db.begin():
                row = await self._locked_user(db, name)
                row.pw_hash = pw_hash
                revoked = await self._revoke_all(db, row.id)
                await audit(
                    db,
                    AuditAction.PASSWORD_RESET,
                    actor=actor,
                    details={"user_id": row.id, "tokens_revoked": revoked},
                )
                await db.flush()
                record = _user(row)
        except UserError:
            raise
        except SQLAlchemyError as exc:
            raise _store_error("reset_password", exc) from exc
        log.info(
            "controller.users.password_reset", user_id=record.id, tokens_revoked=revoked
        )
        return record, revoked

    async def login_record(self, name: str) -> LoginRecord | None:
        """The user ``name`` with its password hash, or ``None`` (no such user)."""
        try:
            async with self._sessions() as db:
                row = await db.scalar(select(AppUser).where(AppUser.name == name))
        except SQLAlchemyError as exc:
            raise _store_error("login_record", exc) from exc
        if row is None:
            return None
        return LoginRecord(
            user_id=row.id,
            name=row.name,
            role=UserRole(row.role),
            pw_hash=row.pw_hash,
            disabled=bool(row.disabled),
        )

    # ------------------------------------------------------------- tokens

    async def issue_token(
        self,
        user: LoginRecord,
        *,
        ttl_s: int,
        token_name: str = DEFAULT_TOKEN_NAME,
        source: str | None = None,
    ) -> tuple[SecretStr, TokenRecord]:
        """A fresh API token for ``user`` (the login's audit row in the same
        transaction); the token is returned once and never stored."""
        token = new_api_token()
        token_id = new_api_token_id()
        name = token_name[:MAX_TOKEN_NAME_CHARS] or DEFAULT_TOKEN_NAME
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(
                    insert(ApiToken).values(
                        id=token_id,
                        user_id=user.user_id,
                        name=name,
                        token_hash=api_token_hash(token.get_secret_value()),
                        created_at=func.now(),
                        expires_at=func.now()
                        + literal(dt.timedelta(seconds=ttl_s), Interval()),
                    )
                )
                await audit(
                    db,
                    AuditAction.LOGIN,
                    actor=f"user:{user.user_id}",
                    source=source,
                    details={"token_id": token_id, "ttl_s": ttl_s},
                )
                row = await db.get_one(ApiToken, token_id)
                record = _token(row, user.name, row.created_at)
        except SQLAlchemyError as exc:
            raise _store_error("issue_token", exc) from exc
        log.info(
            "controller.auth.login",
            user_id=user.user_id,
            token_id=token_id,
            expires_at=record.expires_at.isoformat(),
        )
        return token, record

    async def record_login_failure(
        self, *, user_id: str | None, reason: str, source: str | None
    ) -> None:
        """One ``login_failed`` audit row (best effort: a failed write is logged,
        the login is refused anyway)."""
        try:
            async with self._sessions() as db, db.begin():
                await audit(
                    db,
                    AuditAction.LOGIN_FAILED,
                    actor=f"user:{user_id}" if user_id else "anonymous",
                    source=source,
                    details={"reason": reason},
                )
        except SQLAlchemyError as exc:
            log.warning(
                "controller.auth.audit_failed",
                action="login_failed",
                cause=type(exc).__name__,
            )

    async def authenticate(self, token: str) -> Principal | None:
        """The user behind a live API token, or ``None`` (unknown, revoked,
        expired, or the user is disabled - one answer for all four)."""
        if not is_api_token(token):
            return None
        wanted = api_token_hash(token)
        try:
            async with self._sessions() as db, db.begin():
                row = (
                    await db.execute(
                        select(
                            ApiToken.id,
                            ApiToken.token_hash,
                            ApiToken.last_used_at,
                            AppUser.id,
                            AppUser.name,
                            AppUser.role,
                            func.now(),
                        )
                        .join(AppUser, AppUser.id == ApiToken.user_id)
                        .where(
                            ApiToken.token_hash == wanted,
                            ApiToken.revoked_at.is_(None),
                            ApiToken.expires_at > func.now(),
                            AppUser.disabled.is_(False),
                        )
                    )
                ).one_or_none()
                if row is None:
                    return None
                token_id, stored, last_used, user_id, name, role, now = row
                if not hmac.compare_digest(str(stored), wanted):  # pragma: no cover
                    return None
                stale = now - dt.timedelta(seconds=LAST_USED_RESOLUTION_S)
                if last_used is None or last_used < stale:
                    await db.execute(
                        update(ApiToken)
                        .where(
                            ApiToken.id == token_id,
                            or_(
                                ApiToken.last_used_at.is_(None),
                                ApiToken.last_used_at < stale,
                            ),
                        )
                        .values(last_used_at=func.now())
                        .execution_options(synchronize_session=False)
                    )
        except SQLAlchemyError as exc:
            raise _store_error("authenticate", exc) from exc
        return Principal(
            user_id=str(user_id), name=str(name), role=UserRole(role), token_id=token_id
        )

    async def get_token(self, token_id: str) -> TokenRecord | None:
        try:
            async with self._sessions() as db:
                now = await _db_now(db)
                found = (
                    await db.execute(
                        select(ApiToken, AppUser.name)
                        .join(AppUser, AppUser.id == ApiToken.user_id)
                        .where(ApiToken.id == token_id)
                    )
                ).one_or_none()
        except SQLAlchemyError as exc:
            raise _store_error("get_token", exc) from exc
        return None if found is None else _token(found[0], found[1], now)

    async def list_tokens(self, user_id: str | None) -> list[TokenRecord]:
        """Newest first; ``user_id`` None = every user's (admin)."""
        query = (
            select(ApiToken, AppUser.name)
            .join(AppUser, AppUser.id == ApiToken.user_id)
            .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
            .limit(MAX_LISTED_TOKENS)
        )
        if user_id is not None:
            query = query.where(ApiToken.user_id == user_id)
        try:
            async with self._sessions() as db:
                now = await _db_now(db)
                rows: Sequence[Any] = (await db.execute(query)).all()
                return [_token(row[0], row[1], now) for row in rows]
        except SQLAlchemyError as exc:
            raise _store_error("list_tokens", exc) from exc

    async def revoke_token(
        self, token_id: str, *, by: Principal, source: str | None = None
    ) -> tuple[TokenRecord, bool]:
        """Revoke one token; ``(record, already_revoked)``. A user may revoke only
        their own tokens - another user's is :class:`UserNotFoundError` (404, the
        same answer as an unknown id); an admin may revoke any."""
        try:
            async with self._sessions() as db, db.begin():
                found = (
                    await db.execute(
                        select(ApiToken, AppUser.name)
                        .join(AppUser, AppUser.id == ApiToken.user_id)
                        .where(ApiToken.id == token_id)
                        .with_for_update(of=ApiToken)
                    )
                ).one_or_none()
                if found is None or not (by.is_admin or found[0].user_id == by.user_id):
                    raise UserNotFoundError(
                        "no such token", details={"reason": "no_token"}
                    )
                row, user_name = found
                already = row.revoked_at is not None
                if not already:
                    row.revoked_at = await _db_now(db)
                    await audit(
                        db,
                        AuditAction.TOKEN_REVOKED,
                        actor=by.actor,
                        source=source,
                        details={"token_id": row.id, "user_id": row.user_id},
                    )
                    await db.flush()
                record = _token(row, user_name, await _db_now(db))
        except UserError:
            raise
        except SQLAlchemyError as exc:
            raise _store_error("revoke_token", exc) from exc
        if not already:
            log.info(
                "controller.auth.token_revoked",
                token_id=token_id,
                user_id=record.user_id,
            )
        return record, already

    # ------------------------------------------------------------- authz reads

    async def owns_output(self, user_id: str, digest_hex: str) -> bool:
        """Is ``sha256:<digest_hex>`` the output archive of a job ``user_id``
        owns? (A user downloads only their own jobs' outputs, D141.)"""
        try:
            async with self._sessions() as db:
                found = await db.scalar(
                    select(Attempt.id)
                    .join(Job, Job.id == Attempt.job_id)
                    .where(
                        Job.owner_user_id == user_id,
                        Attempt.output_digest == f"sha256:{digest_hex}",
                    )
                    .limit(1)
                )
        except SQLAlchemyError as exc:
            raise _store_error("owns_output", exc) from exc
        return found is not None

    async def note_legacy_use(self, source: str | None) -> None:
        """The legacy fabric token was accepted: a warning at most once a minute
        and one ``legacy_token_used`` audit row per controller run."""
        self.legacy_uses += 1
        now = time.monotonic()
        if (
            self._legacy_logged_at is None
            or now - self._legacy_logged_at >= _LEGACY_LOG_INTERVAL_S
        ):
            self._legacy_logged_at = now
            log.warning(
                "controller.auth.legacy_token_used",
                uses=self.legacy_uses,
                hint="[controller.auth] legacy_fabric_token is on; switch every "
                "client to `compute login` and turn it off (removed in V0.8)",
            )
        if self._legacy_audited:
            return
        self._legacy_audited = True
        try:
            async with self._sessions() as db, db.begin():
                await audit(
                    db,
                    AuditAction.LEGACY_TOKEN_USED,
                    actor="rest:fabric-token",
                    source=source,
                    details={"first_use_this_run": True},
                )
        except SQLAlchemyError as exc:
            self._legacy_audited = False  # try again on the next use
            log.warning(
                "controller.auth.audit_failed",
                action="legacy_token_used",
                cause=type(exc).__name__,
            )
