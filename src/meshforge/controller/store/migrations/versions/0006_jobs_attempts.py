"""jobs and attempts: job, job_transition, attempt, attempt_phase (V0.4 G2, D49)

The persistence behind ``POST /v1/jobs`` (roadmap 0.4.4/0.4.5). See
``controller/store/models/job.py`` for the column meanings and
``controller/jobs/store.py`` for the compare-and-set writer.

The value lists in the CHECK constraints are **frozen literals** (not imported
from the enums): a migration must keep meaning what it meant when it ran; a new
state value is a new migration that widens the CHECK.

``idempotency_key`` is UNIQUE (NULLs allowed, PostgreSQL treats them as distinct):
two concurrent POSTs with one key produce exactly one row.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATES = (
    "'submitted', 'planning', 'queued', 'preparing', 'running', 'collecting', "
    "'succeeded', 'failed', 'cancelled', 'retrying'"
)
ATTEMPT_STATES = (
    "'assigned', 'staging', 'env_ready', 'running', 'uploading', 'done', 'failed', "
    "'lost', 'fenced'"
)
FAILURE_TYPES = (
    "'user_error', 'oom', 'vram_oom', 'env_error', 'disk_full', 'node_lost', "
    "'gpu_session_lost', 'transfer_error', 'integrity_error', 'fenced', 'cancelled', "
    "'timeout', 'deadline_exceeded', 'resource_busy', 'no_feasible_plan', "
    "'network_error', 'config_error', 'invalid_input', 'unauthorized', 'not_found', "
    "'conflict', 'internal_error'"
)
DIGEST = "^sha256:[0-9a-f]{64}$"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("bundle_digest", sa.String(length=71), nullable=False),
        sa.Column("placement", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("failure_type", sa.String(length=32), nullable=True),
        sa.Column("failure_detail", sa.String(length=1024), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(f"state IN ({JOB_STATES})", name=op.f("ck_job_state")),
        sa.CheckConstraint(
            f"failure_type IS NULL OR failure_type IN ({FAILURE_TYPES})",
            name=op.f("ck_job_failure_type"),
        ),
        sa.CheckConstraint(
            f"bundle_digest ~ '{DIGEST}'", name=op.f("ck_job_bundle_digest")
        ),
        sa.CheckConstraint("attempt_epoch >= 0", name=op.f("ck_job_attempt_epoch")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_job_idempotency_key")),
    )
    op.create_index("ix_job_created_at", "job", ["created_at"], unique=False)
    op.create_index("ix_job_state", "job", ["state"], unique=False)

    op.create_table(
        "job_transition",
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"from_state IS NULL OR from_state IN ({JOB_STATES})",
            name=op.f("ck_job_transition_from_state"),
        ),
        sa.CheckConstraint(
            f"to_state IN ({JOB_STATES})", name=op.f("ck_job_transition_to_state")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_job_transition_seq")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_transition_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "seq", name=op.f("pk_job_transition")),
    )

    op.create_table(
        "attempt",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_type", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.String(length=1024), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output_digest", sa.String(length=71), nullable=True),
        sa.Column(
            "start_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            f"state IN ({ATTEMPT_STATES})", name=op.f("ck_attempt_state")
        ),
        sa.CheckConstraint(
            f"failure_type IS NULL OR failure_type IN ({FAILURE_TYPES})",
            name=op.f("ck_attempt_failure_type"),
        ),
        sa.CheckConstraint(
            f"output_digest IS NULL OR output_digest ~ '{DIGEST}'",
            name=op.f("ck_attempt_output_digest"),
        ),
        sa.CheckConstraint("fencing_token >= 1", name=op.f("ck_attempt_fencing_token")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_attempt_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["node.node_id"], name=op.f("fk_attempt_node_id_node")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attempt")),
        sa.UniqueConstraint(
            "job_id", "fencing_token", name=op.f("uq_attempt_job_id_fencing_token")
        ),
    )
    op.create_index("ix_attempt_node_id", "attempt", ["node_id"], unique=False)

    op.create_table(
        "attempt_phase",
        sa.Column("attempt_id", sa.String(length=40), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "duration_ms >= 0", name=op.f("ck_attempt_phase_duration_ms")
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_attempt_phase_attempt_id_attempt"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", "phase", name=op.f("pk_attempt_phase")),
    )


def downgrade() -> None:
    op.drop_table("attempt_phase")
    op.drop_index("ix_attempt_node_id", table_name="attempt")
    op.drop_table("attempt")
    op.drop_table("job_transition")
    op.drop_index("ix_job_state", table_name="job")
    op.drop_index("ix_job_created_at", table_name="job")
    op.drop_table("job")
