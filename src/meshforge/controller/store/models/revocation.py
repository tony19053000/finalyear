"""``revoked_cert``: the fabric's certificate revocation set (V0.7 G3, D138;
migration 0016).

One row per node certificate serial that must never be accepted again even
though it is still signed by the fabric CA and not yet expired:

* ``revoked`` - ``node revoke`` took the node out of the fabric;
* ``superseded`` - a re-enrolment (``join --force``, ``invite --replace``)
  issued the node a new certificate; the old one stays CA-valid until it
  expires, so it is listed here and no plane accepts it any more.

The controller checks every session and HTTPS client certificate against it
(by serial, in memory, refreshed from this table); agents get the set in every
``HelloAck`` and as ``RevocationUpdate`` and refuse those serials on their
peer servers. ``not_after`` is the certificate's own expiry: once it passes,
the CA check refuses the certificate anyway and the row is no longer sent.
Rows are never deleted by the controller (the audit trail is ``audit_event``).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

from sqlalchemy import CheckConstraint, DateTime, Index, String
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = ["SERIAL_PATTERN", "RevocationReason", "RevokedCert"]

SERIAL_PATTERN: Final = "^[0-9a-f]{1,40}$"
"""A certificate serial as stored everywhere: lower-case hex, no leading 0x."""


class RevocationReason(StrEnum):
    """Why a serial is in ``revoked_cert``. Persisted: never rename, only add."""

    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class RevokedCert(Base):
    """One revoked node certificate (see module docstring)."""

    __tablename__ = "revoked_cert"
    __table_args__ = (
        CheckConstraint(f"serial ~ '{SERIAL_PATTERN}'", name="serial"),
        CheckConstraint(
            "reason IN (" + ", ".join(f"'{r.value}'" for r in RevocationReason) + ")",
            name="reason",
        ),
        Index("ix_revoked_cert_node_id", "node_id"),
        Index("ix_revoked_cert_not_after", "not_after"),
    )

    serial: Mapped[str] = mapped_column(String(40), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    revoked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa_func.now(), nullable=False
    )
    not_after: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """The certificate's own expiry (after it, the CA check refuses it anyway)."""
