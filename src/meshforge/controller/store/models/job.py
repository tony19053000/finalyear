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
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.common.errors import FailureType
from meshforge.common.manifest import (
    DEFAULT_BACKOFF_S,
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS,
    MAX_BACKOFF_S,
    MAX_PRIORITY,
    MAX_SPLIT_ITEMS,
    MIN_PRIORITY,
    POLICIES,
)
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.store.base import (
    ID_MAX_LENGTH,
    Base,
    TimestampMixin,
    UInt64,
    id_column,
    uint64_check,
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
RUNTIME_VALUES: Final = ("docker", "subprocess", "venv")
RUNTIME_VALUES_SQL: Final = ", ".join(f"'{v}'" for v in RUNTIME_VALUES)
POLICY_VALUES_SQL: Final = ", ".join(f"'{v}'" for v in POLICIES)
SLOT_VALUES: Final = ("cpu", "gpu")
SLOT_VALUES_SQL: Final = ", ".join(f"'{v}'" for v in SLOT_VALUES)
FINGERPRINT_LENGTH: Final = 64


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
        CheckConstraint(
            _digest_check("bundle_digest", nullable=True), name="bundle_digest"
        ),
        CheckConstraint(_digest_check("tree_id", nullable=True), name="tree_id"),
        CheckConstraint(
            "bundle_digest IS NOT NULL OR tree_id IS NOT NULL", name="job_source"
        ),
        CheckConstraint("attempt_epoch >= 0", name="attempt_epoch"),
        CheckConstraint(
            f"priority >= {MIN_PRIORITY} AND priority <= {MAX_PRIORITY}",
            name="priority",
        ),
        CheckConstraint(
            f"policy IS NULL OR policy IN ({POLICY_VALUES_SQL})", name="policy"
        ),
        CheckConstraint(
            f"max_attempts >= 1 AND max_attempts <= {MAX_ATTEMPTS}",
            name="max_attempts",
        ),
        CheckConstraint(
            f"retry_backoff_s >= 0 AND retry_backoff_s <= {MAX_BACKOFF_S}",
            name="retry_backoff_s",
        ),
        uint64_check("input_bytes", name="input_bytes"),
        CheckConstraint(
            _digest_check("split_items_digest", nullable=True),
            name="split_items_digest",
        ),
        CheckConstraint(
            "(split IS NULL AND split_items_digest IS NULL "
            "AND split_item_count IS NULL AND split_item_bytes IS NULL) OR "
            "(split IS NOT NULL AND split_items_digest IS NOT NULL "
            f"AND split_item_count >= 1 AND split_item_count <= {MAX_SPLIT_ITEMS} "
            "AND (split_item_bytes IS NULL OR split_item_bytes >= 0))",
            name="split_shape",
        ),
        UniqueConstraint("idempotency_key"),
        Index("ix_job_created_at", "created_at"),
        Index("ix_job_state", "state"),
        Index(
            "ix_job_queued_at",
            "queued_at",
            postgresql_where=text("state = 'queued'"),
        ),
        Index("ix_job_fingerprint", "fingerprint"),
        Index("ix_job_owner_user_id", "owner_user_id"),
    )

    id: Mapped[str] = id_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(MAX_JOB_NAME_CHARS), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The canonical manifest (``Manifest.canonical_dict()``)."""
    bundle_digest: Mapped[str | None] = mapped_column(
        String(DIGEST_LENGTH), nullable=True
    )
    """V0.4 bundle (``sha256:``); NULL for a tree job (migration 0009)."""
    tree_id: Mapped[str | None] = mapped_column(String(DIGEST_LENGTH), nullable=True)
    """The project's tree manifest (V0.5 D81/D83, migration 0009)."""
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
    # --- V0.6 G2 (migration 0013): planner inputs ---------------------------
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    """D106: -10..10, higher first; the dispatcher ages it while the job waits."""
    policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """D107: the placement policy asked for; NULL = the controller's default."""
    max_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text(str(DEFAULT_MAX_ATTEMPTS))
    )
    """D110: attempts in total (used by V0.6 G3's retries)."""
    retry_backoff_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_BACKOFF_S))
    )
    on_lost_retry: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    """D117: false = a LOST attempt is never re-run."""
    submitter_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    """D103: the submitting laptop's own node (its candidate is *Local*); not a
    foreign key - an unknown id only means there is no Local candidate."""
    submitter_seeded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    """D109: the CLI put the project's blobs into the submitter node's CAS."""
    not_before: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """D110: a retry waits until then (dispatch skips it before)."""
    queued_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When the job last entered QUEUED (queue order and priority aging, D106)."""
    input_bytes: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    """Bytes of the project: the tree's unique file blobs, or the bundle - the
    estimator's transfer volume (D108; named inputs are sized from
    ``named_input``)."""
    fingerprint: Mapped[str | None] = mapped_column(
        String(FINGERPRINT_LENGTH), nullable=True
    )
    """D108 workload fingerprint (64 hex): same project, entrypoint and env
    request -> same fingerprint; history of past runs is grouped by it."""
    owner_user_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    """V0.7 G4 (D141, migration 0018): the user who submitted the job. A user
    sees, cancels and downloads only their own jobs, an admin every job. NULL =
    a job from before 0018, or one submitted with the legacy fabric token
    (admin-only, like every NULL-owner job)."""
    # --- V0.10 G1 (migration 0026, D260): Mode 3 task splitting -------------
    split: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """The canonical ``split:`` spec (``SplitSpec``); NULL = the job runs
    whole. Also inside ``manifest``; a column of its own so "is this a split
    job" never parses the manifest."""
    split_items_digest: Mapped[str | None] = mapped_column(
        String(DIGEST_LENGTH), nullable=True
    )
    """The item list resolved at submit, stored as a CAS blob (canonical JSON,
    ``meshforge.common.split_items``); every later step reads this one list."""
    split_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    split_item_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Bytes the items stand for (glob files / lines records); NULL for list,
    list_file and range items."""


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
        CheckConstraint(f"slot IS NULL OR slot IN ({SLOT_VALUES_SQL})", name="slot"),
        CheckConstraint(
            "env_hash IS NULL OR env_hash ~ '^[0-9a-f]{64}$'", name="env_hash"
        ),
        uint64_check("output_bytes", name="output_bytes"),
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
    """``docker`` / ``venv`` / ``subprocess`` as the agent reported it (migrations
    0007, 0010)."""
    missing_outputs: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """Declared output globs that matched nothing (D74; migration 0008)."""
    logs: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """Final logs per stream: digest, bytes, truncated (D71; migration 0008)."""
    slot: Mapped[str | None] = mapped_column(String(8), nullable=True)
    """D105: the node slot the attempt holds while live (``cpu``/``gpu``);
    NULL for attempts dispatched before V0.6 (counted as a CPU slot)."""
    env_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """V0.6 G4 (D108, migration 0014): the agent's env hash from the terminal
    report - with ``cache_location`` (kind ``env``) it tells the estimator that
    a node still holds this workload's environment."""
    output_bytes: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    """V0.6 G4 (D108, migration 0014): bytes the attempt uploaded under its
    grant (outputs + final logs), on DONE - the estimator's ``out_bytes``."""
    workload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """V0.9 G1 (D212, migration 0021): a benchmark's own report from the
    terminal status (``results/workload.json``: family, params, seed, status,
    device, compute_ns, compute_iterations); NULL for every other job. Bounded by
    the report parser."""


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
