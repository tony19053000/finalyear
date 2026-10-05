"""named_input, job_input, transfer_event (V0.5 G5, D92/D93)

Hand-written to match :mod:`meshforge.controller.store.models.data` exactly
(``alembic check`` in the component tests fails on drift). New tables only: the
downgrade drops them (named inputs registered meanwhile are forgotten; their
blobs stay in the CAS until GC).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from meshforge.controller.store.base import UINT64_MAX
from meshforge.controller.store.models.data import INPUT_NAME_PATTERN

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST = "{col} ~ '^sha256:[0-9a-f]{{64}}$'"


def upgrade() -> None:
    op.create_table(
        "named_input",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tree_id", sa.String(length=71), nullable=False),
        sa.Column("size", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column(
            "pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"name ~ '{INPUT_NAME_PATTERN}'", name=op.f("ck_named_input_name")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_named_input_version")),
        sa.CheckConstraint(
            _DIGEST.format(col="tree_id"), name=op.f("ck_named_input_tree_id")
        ),
        sa.CheckConstraint(
            f"size >= 0 AND size <= {UINT64_MAX}", name=op.f("ck_named_input_size")
        ),
        sa.CheckConstraint("files >= 0", name=op.f("ck_named_input_files")),
        sa.PrimaryKeyConstraint("name", "version", name=op.f("pk_named_input")),
        sa.UniqueConstraint(
            "name", "tree_id", name=op.f("uq_named_input_name_tree_id")
        ),
    )
    op.create_index("ix_named_input_tree_id", "named_input", ["tree_id"], unique=False)

    op.create_table(
        "job_input",
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("mount", sa.String(length=256), nullable=False),
        sa.Column("ref", sa.String(length=140), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("tree_id", sa.String(length=71), nullable=False),
        sa.CheckConstraint(
            _DIGEST.format(col="tree_id"), name=op.f("ck_job_input_tree_id")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_job_input_version")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_input_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "mount", name=op.f("pk_job_input")),
    )
    op.create_index(
        "ix_job_input_name_version", "job_input", ["name", "version"], unique=False
    )
    op.create_index("ix_job_input_tree_id", "job_input", ["tree_id"], unique=False)

    op.create_table(
        "transfer_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("attempt_id", sa.String(length=40), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("tree_id", sa.String(length=71), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("bytes", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column(
            "resumed_chunks", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("dropped", sa.String(length=32), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _DIGEST.format(col="tree_id"), name=op.f("ck_transfer_event_tree_id")
        ),
        sa.CheckConstraint(
            f"bytes >= 0 AND bytes <= {UINT64_MAX}",
            name=op.f("ck_transfer_event_bytes"),
        ),
        sa.CheckConstraint(
            "duration_ms >= 0", name=op.f("ck_transfer_event_duration_ms")
        ),
        sa.CheckConstraint("units >= 0", name=op.f("ck_transfer_event_units")),
        sa.CheckConstraint("failures >= 0", name=op.f("ck_transfer_event_failures")),
        sa.CheckConstraint(
            "resumed_chunks >= 0", name=op.f("ck_transfer_event_resumed_chunks")
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_transfer_event_attempt_id_attempt"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfer_event")),
        sa.UniqueConstraint(
            "attempt_id",
            "tree_id",
            "source",
            name=op.f("uq_transfer_event_attempt_id_tree_id_source"),
        ),
    )
    op.create_index(
        "ix_transfer_event_job_id", "transfer_event", ["job_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_transfer_event_job_id", table_name="transfer_event")
    op.drop_table("transfer_event")
    op.drop_index("ix_job_input_tree_id", table_name="job_input")
    op.drop_index("ix_job_input_name_version", table_name="job_input")
    op.drop_table("job_input")
    op.drop_index("ix_named_input_tree_id", table_name="named_input")
    op.drop_table("named_input")
