"""Node registry tables: ``node`` and ``node_session`` (ARCHITECTURE §19, roadmap 0.3.6).

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
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
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
    )

    node_id: Mapped[str] = id_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    os: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    hardware: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """``Hello.hardware`` as JSON (message-to-dict); interpreted by 0.3.3 (G4)."""
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
