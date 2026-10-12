"""Job secrets, encrypted at rest (V0.7 G6, roadmap 0.7.10, D144; migration 0019).

One row per (owner, name): the AES-256-GCM ``ciphertext`` (value + 16-byte tag),
its 12-byte ``nonce`` and the ``key_id`` of the master key that sealed it
(:mod:`meshforge.controller.secret_box`). **No column ever holds the value.**
The ciphertext is bound to ``owner_user_id`` and ``name`` (AES-GCM associated
data): a row copied to another owner or renamed does not decrypt. Rows are
deleted with their user (``CASCADE``); a disabled user keeps them (enabling
the user again gives them back).
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.common.secret_names import MAX_SECRET_BYTES, SECRET_NAME_PATTERN
from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = ["MAX_CIPHERTEXT_BYTES", "Secret"]

GCM_TAG_BYTES: Final = 16
MAX_CIPHERTEXT_BYTES: Final = MAX_SECRET_BYTES + GCM_TAG_BYTES


class Secret(Base):
    """One user's secret (see module docstring)."""

    __tablename__ = "secret"
    __table_args__ = (
        CheckConstraint(f"name ~ '{SECRET_NAME_PATTERN}'", name="name"),
        CheckConstraint("octet_length(nonce) = 12", name="nonce"),
        CheckConstraint(
            f"octet_length(ciphertext) BETWEEN {GCM_TAG_BYTES + 1} "
            f"AND {MAX_CIPHERTEXT_BYTES}",
            name="ciphertext",
        ),
        CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name="key_id"),
    )

    owner_user_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=sa_func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=sa_func.now()
    )
