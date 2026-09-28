"""Job and attempt tables (ARCHITECTURE §19, brief D49; migration 0006).

``job`` is one row per accepted submission; ``job_transition`` is its state
history (one row per arrow, with the wall-clock time it happened - phase timings
such as queue time are read from it); ``attempt`` is one execution of a job on one
node, fenced by ``fencing_token`` (the job's attempt epoch, starting at 1);
``attempt_phase`` holds per-attempt phase durations (ARCHITECTURE §17).

State and failure columns are plain text with a CHECK over the Python enum values
(the ``node`` table's rule): adding a value is a one-line migration. Every state
write goes through :class:`~meshforge.controller.jobs.store.JobStore`, which
compare-and-sets on the current state. Rows are validated before they are
written (a rejected submission never creates a row, D50).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.store.base import (
    ID_MAX_LENGTH,
    Base,
    TimestampMixin,
    id_column,
)
from meshforge.controller.store.models.node import sql_in_list

__all__ = [
    "DIGEST_CHECK_SQL",
    "DIGEST_LENGTH",
    "MAX_DETAIL_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "Attempt",
    "AttemptPhaseRow",
    "Job",
    "JobTransition",
]

DIGEST_LENGTH: Final = 71
"""``sha256:`` + 64 hex."""
DIGEST_CHECK_SQL: Final = "{col} ~ '^sha256:[0-9a-f]{{64}}$'"
MAX_IDEMPOTENCY_KEY_CHARS: Final = 128
MAX_DETAIL_CHARS: Final = 1024
MAX_JOB_NAME_CHARS: Final = 64
RUNTIME_VALUES: Final = ("docker", "subprocess")
RUNTIME_VALUES_SQL: Final = ", ".join(f"'{v}'" for v in RUNTIME_VALUES)


def _digest_check(column: str, *, nullable: bool = False) -> str:
    check = DIGEST_CHECK_SQL.format(col=column)
    return f"{column} IS NULL OR {check}" if nullable else check


class Job(TimestampMixin, Base):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint(f"state IN ({sql_in_list(JobState)})", name="state"),
        CheckConstraint(
            f"failure_type IS NULL OR failure_type IN ({sql_in_list(FailureType)})",
            name="failure_type",
        ),
        CheckConstraint(_digest_check("bundle_digest"), name="bundle_digest"),
        CheckConstraint("attempt_epoch >= 0", name="attempt_epoch"),
        UniqueConstraint("idempotency_key"),
        Index("ix_job_created_at", "created_at"),
        Index("ix_job_state", "state"),
    )

    id: Mapped[str] = id_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_JOB_NAME_CHARS), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The canonical manifest (``Manifest.canonical_dict()``)."""
    bundle_digest: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    placement: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """``{"mode": "local"|"offload", "node_id": ...}`` (manual placement, V0.4)."""
    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_CHARS), nullable=True
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 hex of the canonical request; same key + other hash = 409."""
    attempt_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    """The last fencing token handed out for this job (0 = no attempt yet)."""
    failure_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(
        String(MAX_DETAIL_CHARS), nullable=True
    )
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """``POST /v1/jobs/{id}/cancel`` on a live attempt (D69, migration 0007)."""


class JobTransition(Base):
    __tablename__ = "job_transition"
    __table_args__ = (
        PrimaryKeyConstraint("job_id", "seq"),
        CheckConstraint(
            f"from_state IS NULL OR from_state IN ({sql_in_list(JobState)})",
            name="from_state",
        ),
        CheckConstraint(f"to_state IN ({sql_in_list(JobState)})", name="to_state"),
        CheckConstraint("seq >= 1", name="seq"),
    )

    job_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    at: Mapped[dt.datetime] = mapped_column(nullable=False)


class Attempt(TimestampMixin, Base):
    __tablename__ = "attempt"
    __table_args__ = (
        CheckConstraint(f"state IN ({sql_in_list(AttemptState)})", name="state"),
        CheckConstraint(
            f"failure_type IS NULL OR failure_type IN ({sql_in_list(FailureType)})",
            name="failure_type",
        ),
        CheckConstraint(
            _digest_check("output_digest", nullable=True), name="output_digest"
        ),
        CheckConstraint("fencing_token >= 1", name="fencing_token"),
        CheckConstraint(
            f"runtime IS NULL OR runtime IN ({RUNTIME_VALUES_SQL})", name="runtime"
        ),
        UniqueConstraint("job_id", "fencing_token"),
        Index("ix_attempt_node_id", "node_id"),
    )

    id: Mapped[str] = id_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH), ForeignKey("node.node_id"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(MAX_DETAIL_CHARS), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_digest: Mapped[str | None] = mapped_column(
        String(DIGEST_LENGTH), nullable=True
    )
    start_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    start_acked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When the agent acked ``StartAttempt`` (D66: resend vs LOST; migration 0007)."""
    runtime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """``docker`` / ``subprocess`` as the agent reported it (migration 0007)."""
    missing_outputs: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """Declared output globs that matched nothing (D74; migration 0008)."""
    logs: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """Final logs per stream: digest, bytes, truncated (D71; migration 0008)."""


class AttemptPhaseRow(Base):
    __tablename__ = "attempt_phase"
    __table_args__ = (
        PrimaryKeyConstraint("attempt_id", "phase"),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
    )

    attempt_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("attempt.id", ondelete="CASCADE"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    """An :class:`~meshforge.common.states.AttemptPhase` value (validated in
    Python; no CHECK, so a later phase such as ``compute`` needs no migration)."""
    started_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    """The phase never ran: ``duration_ms`` is 0 by convention, not measured (D68)."""
