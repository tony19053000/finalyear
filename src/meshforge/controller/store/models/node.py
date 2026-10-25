"""Node registry tables: ``node`` and ``node_session`` (ARCHITECTURE §19, roadmap 0.3.6).

V0.7 G2 adds the node certificate columns (``cert_*``, ``public_key_fingerprint``;
migration 0015). An enrolment of a ``node_id`` the controller has never seen
inserts the row in state ``enrolling`` with empty descriptive fields; its first
Hello fills them (``ENROLLING -> ONLINE``, ARCHITECTURE §12.3).

``node`` is one row per ``node_id`` (the agent's persistent identity, ADR-0013) and
carries the *current* liveness state plus what the last ``Hello`` told us.
``node_session`` is one row per accepted stream (ADR-0002 Updates §1-2): it records
when the stream lived, why it ended and the RTT measured over it (roadmap 0.3.10).

Rules carried over from the 0.1.7 review: every ``CheckConstraint`` is named;
``updated_at`` is maintained by SQLAlchemy ``onupdate`` (all writes go through the
ORM / Core, never raw SQL); the state and end-reason columns are plain text with
a CHECK over the Python enum values, so adding a value is a one-line migration
rather than a PostgreSQL ``ALTER TYPE``.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    REAL,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.liveness import NodeState
from meshforge.controller.store.base import (
    ID_MAX_LENGTH,
    Base,
    TimestampMixin,
    UInt64,
    id_column,
    uint64_check,
)

__all__ = [
    "END_REASON_CHECK_NAME",
    "MAX_SLOTS",
    "NODE_STATE_CHECK_NAME",
    "SESSION_EPOCH_CHECK_NAME",
    "Node",
    "NodeSession",
    "SessionEndReason",
    "sql_in_list",
]


class SessionEndReason(StrEnum):
    """Why a ``node_session`` ended. Values are persisted; never rename, only add."""

    SUPERSEDED = "superseded"
    """A Hello with a higher epoch for the same node replaced this stream."""
    AGENT_CLOSED = "agent_closed"
    """The agent half-closed its stream (a clean close). Before 0004 a transport
    drop was recorded as this too; since G6 that is ``stream_lost``."""
    LEASE_EXPIRED = "lease_expired"
    """Silence reached ``offline_after_s``; the node went OFFLINE."""
    CONTROLLER_SHUTDOWN = "controller_shutdown"
    """Graceful controller stop (SIGINT/SIGTERM)."""
    CONTROLLER_RESTART = "controller_restart"
    """Found still open at controller start-up (the previous process died)."""
    PROTOCOL_ERROR = "protocol_error"
    """The agent broke the session contract (second Hello, empty message_id,
    outbound queue overflow...)."""
    INTERNAL_ERROR = "internal_error"
    """A bug on *our* side ended the stream (the reader task raised). Kept apart
    from ``protocol_error`` so "the peer misbehaved" and "we misbehaved" are not
    the same number on a dashboard (G2 review follow-up, added in 0003)."""
    STREAM_LOST = "stream_lost"
    """The stream ended abruptly - transport drop, call cancelled by the peer or
    its channel closed without a half-close (V0.3 G6, D19, added in 0004). The
    node goes SUSPECT and the lease decides whether it goes OFFLINE."""
    REVOKED = "revoked"
    """The node was revoked (``node revoke``) and its live stream cut at once
    with ``PERMISSION_DENIED`` (V0.7 G3, D138, added in 0016)."""
    CERT_SUPERSEDED = "cert_superseded"
    """The stream's certificate was superseded by a newer enrolment of the node
    (its serial joined the revocation set) and the stream was cut with
    ``PERMISSION_DENIED`` (V0.7 G3 fix r1, D155 T-1, added in 0017)."""
    CERT_EXPIRED = "cert_expired"
    """The stream's certificate reached its ``not_after`` while the stream was
    open; cut with ``UNAUTHENTICATED`` (V0.7 G3 fix r1, D155 T-3, added in
    0017)."""


MAX_SLOTS: Final = 64
"""Largest per-node CPU/GPU slot count a Hello may advertise (V0.6 D105)."""
NODE_STATE_CHECK_NAME: Final = "state"
END_REASON_CHECK_NAME: Final = "end_reason"
SESSION_EPOCH_CHECK_NAME: Final = "session_epoch"


def sql_in_list(values: type[StrEnum]) -> str:
    """``'a', 'b', ...`` for a CHECK constraint over a StrEnum's values."""
    return ", ".join(f"'{member.value}'" for member in values)


class Node(TimestampMixin, Base):
    """One row per known node (agent identity)."""

    __tablename__ = "node"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({sql_in_list(NodeState)})", name=NODE_STATE_CHECK_NAME
        ),
        uint64_check("session_epoch", name=SESSION_EPOCH_CHECK_NAME),
        CheckConstraint(
            f"cpu_slots IS NULL OR (cpu_slots >= 0 AND cpu_slots <= {MAX_SLOTS})",
            name="cpu_slots",
        ),
        CheckConstraint(
            f"gpu_slots IS NULL OR (gpu_slots >= 0 AND gpu_slots <= {MAX_SLOTS})",
            name="gpu_slots",
        ),
        # V0.7 G2 (migration 0015): the four certificate columns are set together.
        CheckConstraint(
            "(cert_serial IS NULL) = (cert_fingerprint IS NULL) AND "
            "(cert_serial IS NULL) = (cert_not_after IS NULL) AND "
            "(cert_serial IS NULL) = (public_key_fingerprint IS NULL)",
            name="cert_all_or_none",
        ),
        CheckConstraint(
            "cert_serial IS NULL OR cert_serial ~ '^[0-9a-f]{1,40}$'",
            name="cert_serial",
        ),
        CheckConstraint(
            "cert_fingerprint IS NULL OR cert_fingerprint ~ '^[0-9a-f]{64}$'",
            name="cert_fingerprint",
        ),
        CheckConstraint(
            "public_key_fingerprint IS NULL OR "
            "public_key_fingerprint ~ '^[0-9a-f]{64}$'",
            name="public_key_fingerprint",
        ),
        # V0.7 G4 (migration 0018): a renewed certificate not yet in use.
        CheckConstraint(
            "(pending_cert_serial IS NULL) = (pending_cert_fingerprint IS NULL) AND "
            "(pending_cert_serial IS NULL) = (pending_cert_not_after IS NULL) AND "
            "(pending_cert_serial IS NULL) = (pending_public_key_fingerprint IS NULL)",
            name="pending_cert_all_or_none",
        ),
        CheckConstraint(
            "pending_cert_serial IS NULL OR pending_cert_serial ~ '^[0-9a-f]{1,40}$'",
            name="pending_cert_serial",
        ),
        CheckConstraint(
            "pending_cert_fingerprint IS NULL OR "
            "pending_cert_fingerprint ~ '^[0-9a-f]{64}$'",
            name="pending_cert_fingerprint",
        ),
        CheckConstraint(
            "pending_public_key_fingerprint IS NULL OR "
            "pending_public_key_fingerprint ~ '^[0-9a-f]{64}$'",
            name="pending_public_key_fingerprint",
        ),
        CheckConstraint(
            "pending_cert_serial IS NULL OR cert_serial IS NOT NULL",
            name="pending_needs_current",
        ),
    )

    node_id: Mapped[str] = id_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("''"), default=""
    )
    """V0.17 G3 (Q27): ``Hello.display_name``, the owner's name for the laptop;
    empty = none (migration 0035)."""
    os: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    hardware: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """``Hello.hardware`` as JSON (message-to-dict); interpreted by 0.3.3 (G4)."""
    hardware_scores: Mapped[dict[str, Any] | None] = mapped_column(
        # ``none_as_null``: Python ``None`` must be SQL ``NULL``, not the JSON
        # ``null`` document - the registry's ordering guard tests ``IS NULL``.
        JSONB(none_as_null=True),
        nullable=True,
    )
    """The newest validated hardware-score run (roadmap 0.3.4, D38; added in 0005),
    shaped by ``controller/scores.py``. ``NULL`` = not measured for the hardware the
    node's latest ``Hello`` described (REST ``scores.status`` pending/none)."""
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_session_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    session_epoch: Mapped[int] = mapped_column(UInt64, nullable=False)
    """Highest ``Hello.session_epoch`` ever accepted for this node (never lowered).

    ``Hello.session_epoch`` is a protobuf ``uint64``, so the column must hold the
    full ``[0, 2^64-1]`` domain (ADR-0013 §6 lets the agent's file hold any such
    value; refusing the upper half would strand a node until an identity reset).
    """
    cpu_slots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """``Hello.max_cpu_jobs`` of the latest Hello (V0.6 D105, migration 0013);
    NULL = the agent did not say (an older agent: 1 CPU slot)."""
    gpu_slots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """``Hello.max_gpu_jobs``; NULL = not said (1 GPU slot with ``exec-gpu``)."""
    cert_serial: Mapped[str | None] = mapped_column(
        String(40), nullable=True, unique=True
    )
    """V0.7 G2 (D136): serial of the node's current certificate, lower-case hex
    (<= 159 bits). NULL = never enrolled (a node from before V0.7 G2)."""
    cert_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """SHA-256 of the current node certificate's DER (hex)."""
    cert_not_after: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    public_key_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    """SHA-256 of the certified public key's DER SubjectPublicKeyInfo (hex) -
    the same value as the agent's ``node.json`` ``key_fingerprint_sha256``. A
    second enrolment of this ``node_id`` with another key is refused unless the
    invite names it (``--replace``)."""
    pending_cert_serial: Mapped[str | None] = mapped_column(
        String(40), nullable=True, unique=True
    )
    """V0.7 G4 (D140): a certificate ``RenewCertificate`` issued that the agent
    has not taken into use yet. Its first Hello with it makes it current (the
    four ``cert_*`` columns take these values, the old serial is recorded
    ``superseded``); until then the current certificate keeps working, so a
    renewal never cuts a live session. A newer renewal or a re-enrolment
    supersedes an unused pending certificate; a revocation revokes it too."""
    pending_cert_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    pending_cert_not_after: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    pending_public_key_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    """Differs from ``public_key_fingerprint`` after ``meshforge-agent
    rotate-key`` (the renewal certified a new key)."""
    cert_renewed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When ``RenewCertificate`` last issued a certificate to this node (the
    per-node renewal rate limit, ``[controller.renewal] min_interval_s``)."""


class NodeSession(TimestampMixin, Base):
    """One row per accepted ``AgentControl.Session`` stream."""

    __tablename__ = "node_session"
    __table_args__ = (
        CheckConstraint(
            f"end_reason IS NULL OR end_reason IN ({sql_in_list(SessionEndReason)})",
            name=END_REASON_CHECK_NAME,
        ),
        uint64_check("session_epoch", name=SESSION_EPOCH_CHECK_NAME),
        Index("ix_node_session_node_id_started_at", "node_id", "started_at"),
    )

    session_id: Mapped[str] = id_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("node.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_epoch: Mapped[int] = mapped_column(UInt64, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    ended_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    peer_address: Mapped[str] = mapped_column(String(128), nullable=False)
    """gRPC peer as reported by the transport (``ipv4:host:port``); never a token."""
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_rtt_ms: Mapped[float | None] = mapped_column(REAL, nullable=True)
    rtt_ewma_ms: Mapped[float | None] = mapped_column(REAL, nullable=True)
    # V0.9 G1 (D213, migration 0021): the node's clock offset over this
    # session (agent wall clock minus controller wall clock, from Ping/Pong
    # exchanges - controller/session/clocks.py). Double precision: an offset of
    # hours must keep sub-millisecond digits. All NULL until the first Pong.
    clock_offset_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    clock_offset_bound_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    clock_consistent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    clock_exchanges: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clock_measured_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """The controller's wall clock when the estimate was written."""
