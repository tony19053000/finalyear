"""Users and API tokens of the REST API (V0.7 G4, roadmap 0.7.6, D141;
migration 0018).

``app_user`` - one person who uses the fabric's REST API (``compute``; the V0.8
dashboard reuses it). ``name`` is the login name (lower-case, unique),
``role`` ``admin`` (invites, node revoke, users, every job) or ``user`` (only
their own jobs). ``pw_hash`` is a self-describing stdlib scrypt string
(``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``, :mod:`meshforge.controller.auth.passwords`)
- never the password. A user is never deleted, only ``disabled`` (their jobs
keep their owner and the audit trail keeps its actor).

``api_token`` - one bearer credential a user got from ``POST /v1/auth/login``.
The 256-bit token is **never stored**: only its SHA-256 (``token_hash``, 64
lower-case hex, unique), compared in constant time. Expiry is mandatory;
``revoked_at`` ends it early (``compute tokens revoke``, a password reset, a
disabled user). ``last_used_at`` is coarse (written at most once a minute per
token) - it says "still in use", it is not an access log.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base, id_column

__all__ = [
    "MAX_TOKEN_NAME_CHARS",
    "MAX_USER_NAME_CHARS",
    "USER_NAME_PATTERN",
    "ApiToken",
    "AppUser",
    "UserRole",
]

MAX_USER_NAME_CHARS: Final = 64
USER_NAME_PATTERN: Final = "^[a-z0-9][a-z0-9._-]{0,63}$"
"""Login names: lower-case letters, digits, ``.``, ``_``, ``-``; 1-64 chars."""
MAX_TOKEN_NAME_CHARS: Final = 64
_HEX64: Final = "'^[0-9a-f]{64}$'"


class UserRole(StrEnum):
    """Persisted: never rename, only add."""

    ADMIN = "admin"
    USER = "user"


class AppUser(Base):
    """One REST user (see module docstring)."""

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(f"name ~ '{USER_NAME_PATTERN}'", name="name"),
        CheckConstraint(
            "role IN (" + ", ".join(f"'{r.value}'" for r in UserRole) + ")",
            name="role",
        ),
        CheckConstraint("length(pw_hash) BETWEEN 16 AND 255", name="pw_hash"),
    )

    id: Mapped[str] = id_column(primary_key=True)
    name: Mapped[str] = mapped_column(
        String(MAX_USER_NAME_CHARS), nullable=False, unique=True
    )
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    pw_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=sa_func.now()
    )


class ApiToken(Base):
    """One API token (see module docstring)."""

    __tablename__ = "api_token"
    __table_args__ = (
        CheckConstraint(f"token_hash ~ {_HEX64}", name="token_hash"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            f"length(name) BETWEEN 1 AND {MAX_TOKEN_NAME_CHARS}", name="name"
        ),
        Index("ix_api_token_user_id", "user_id"),
        Index("ix_api_token_expires_at", "expires_at"),
    )

    id: Mapped[str] = id_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(MAX_TOKEN_NAME_CHARS), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
