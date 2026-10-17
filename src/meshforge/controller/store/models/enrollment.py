"""Pairing tables: ``join_token`` and ``audit_event`` (V0.7 G2, D135/D136/D151;
migration 0015). The node certificate columns live on ``node`` (:mod:`.node`).

``join_token`` - one row per invite (``meshforge-controller fabric invite`` /
``compute fabric invite``). The 128-bit token itself is **never stored**: only
its SHA-256 (``token_hash``, 64 lower-case hex, unique). A row is redeemed by
one ``UPDATE ... WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at >
now() RETURNING`` inside the enrolment transaction, so it is single-use under
any race and a refused enrolment (rolled back) does not burn it.
``replace_node_id`` (``--replace NODE_ID``) is the only way to re-key an
already enrolled ``node_id``. Used and revoked are mutually exclusive; used
rows name the node (``used_by_node``, no foreign key: the audit outlives a
deleted node).

``audit_event`` - an append-only trail of security-relevant actions (invite
created / revoked, enrolment accepted / refused, node revoked - 0016). ``details`` is a small JSON
object and **never** holds a token, a code or key material (checklist 20);
``actor`` says who acted (``controller-cli``, ``rest:fabric-token``, later a
user id - G4).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, String
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = [
    "MAX_AUDIT_DETAILS_BYTES",
    "AuditAction",
    "AuditEvent",
    "JoinToken",
]

MAX_AUDIT_DETAILS_BYTES: Final = 4096
_HEX64: Final = "'^[0-9a-f]{64}$'"


class AuditAction(StrEnum):
    """What an ``audit_event`` row records. Persisted: never rename, only add."""

    INVITE_CREATED = "invite_created"
    INVITE_REVOKED = "invite_revoked"
    ENROLLED = "enrolled"
    """A join token was redeemed and a node certificate issued."""
    ENROLL_REFUSED = "enroll_refused"
    """An enrolment that reached the controller's checks was refused (reason in
    ``details``); requests refused by the rate limit are only logged."""
    NODE_REVOKED = "node_revoked"
    """A node was revoked (V0.7 G3, D138; added in 0016): its certificate
    serial in ``details``; ``actor`` = who revoked it."""
    # --- V0.7 G4 (migration 0018) ------------------------------------------
    CERT_RENEWED = "cert_renewed"
    """``RenewCertificate`` issued a node certificate (pending until used)."""
    CERT_RENEW_REFUSED = "cert_renew_refused"
    CERT_ACTIVATED = "cert_activated"
    """The node's first Hello with its renewed certificate: it is current now
    and the previous serial is ``superseded``."""
    USER_CREATED = "user_created"
    USER_DISABLED = "user_disabled"
    USER_ENABLED = "user_enabled"
    PASSWORD_RESET = "password_reset"  # noqa: S105 - an action name
    LOGIN = "login"
    """A login issued an API token (its id in ``details``, never the token)."""
    LOGIN_FAILED = "login_failed"
    TOKEN_REVOKED = "token_revoked"  # noqa: S105 - an action name
    LEGACY_TOKEN_USED = "legacy_token_used"  # noqa: S105 - an action name
    """The legacy fabric token was accepted (``[controller.auth]
    legacy_fabric_token = true``) - recorded once per controller run."""
    # --- V0.7 G6 (migration 0019) ------------------------------------------
    SECRET_SET = "secret_set"  # noqa: S105 - an action name
    """A user stored or replaced a secret (its name, never the value)."""
    SECRET_DELETED = "secret_deleted"  # noqa: S105 - an action name
    # --- V0.8 G1 (migration 0020) ------------------------------------------
    ATTEMPT_WRONG_NODE = "attempt_wrong_node"
    """D168 N3 / D180: a node reported status for an attempt placed on another
    node (THREAT_MODEL CG-E2); refused, nothing applied. ``node_id`` = the
    reporting node, ``details`` = attempt id, the attempt's node, the state
    reported. The dashboard's ``alert`` events are fed from the same refusal."""
    # --- V0.9 G1 (migration 0021) ------------------------------------------
    CLOCK_UNCERTAIN = "clock_uncertain"
    """D213: a node's clock offset became untrustworthy (bound over 250 ms or
    inconsistent exchanges) - one row per episode per session. ``details`` =
    session id, offset, bound, consistent, reasons (numbers and words only)."""
    TELEMETRY_RECORD_SKIPPED = "telemetry_record_skipped"
    """D212 (fix r1, T-5): the run-record sweep skipped an attempt - its record
    could not be built (a bug) or PostgreSQL refused it - so the dataset lacks
    that run until a controller restart retries it. ``details`` = attempt id,
    job id, reason (``unbuildable`` / ``refused``), error type."""


def _in_list(values: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in values)


class JoinToken(Base):
    """One invite (see module docstring)."""

    __tablename__ = "join_token"
    __table_args__ = (
        CheckConstraint(f"token_hash ~ {_HEX64}", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(used_at IS NULL) = (used_by_node IS NULL)", name="used_names_node"
        ),
        CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"
        ),
        Index("ix_join_token_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    used_by_node: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replace_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    """Set by ``invite --replace NODE_ID``: this code may (only) re-key that node."""


class AuditEvent(Base):
    """One security-relevant action (see module docstring)."""

    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(f"action IN ({_in_list(AuditAction)})", name="action"),
        CheckConstraint(
            "jsonb_typeof(details) = 'object' AND "
            f"octet_length(details::text) <= {MAX_AUDIT_DETAILS_BYTES}",
            name="details",
        ),
        Index("ix_audit_event_at", "at"),
        Index("ix_audit_event_node_id", "node_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_func.now(), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(ID_MAX_LENGTH), nullable=True)
    invite_id: Mapped[str | None] = mapped_column(String(ID_MAX_LENGTH), nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Where the request came from (gRPC peer ``ipv4:host:port``), if remote."""
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
