"""resource_snapshot: downsampled usage history + the internal_error end reason (0.3.11)

Hand-written to match :mod:`meshforge.controller.store.models.resource` exactly
(``alembic check`` in the component tests fails on drift). CHECK constraints are
named through the metadata naming convention (``ck_<table>_<name>``).

Two changes in one revision, both from V0.3 G4:

* the ``resource_snapshot`` table (roadmap 0.3.11, brief §5d D11): one row per
  closed 1-minute bucket per node, primary key ``(node_id, bucket_start)`` so a
  re-flush upserts instead of duplicating, and an index on ``bucket_start`` for the
  retention delete;
* ``node_session.end_reason`` gains ``internal_error`` (G2 review follow-up): the
  CHECK is dropped and recreated over the widened enum. The downgrade rewrites any
  ``internal_error`` row back to ``protocol_error`` before narrowing the CHECK, so
  it cannot fail on real data.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from meshforge.controller.store.base import UINT64_MAX
from meshforge.controller.store.models.node import SessionEndReason, sql_in_list
from meshforge.controller.store.models.resource import (
    CPU_MAX_CHECK,
    CPU_MEAN_CHECK,
    SAMPLES_CHECK,
)

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_END_REASON_CHECK = "ck_node_session_end_reason"
_UINT64 = "{column} >= 0 AND {column} <= " + str(UINT64_MAX)
#: The enum as it was before this revision (for the downgrade).
_END_REASONS_0002 = (
    "'superseded', 'agent_closed', 'lease_expired', 'controller_shutdown', "
    "'controller_restart', 'protocol_error'"
)


def upgrade() -> None:
    op.create_table(
        "resource_snapshot",
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("cpu_percent_mean", sa.REAL(), nullable=False),
        sa.Column("cpu_percent_max", sa.REAL(), nullable=False),
        sa.Column(
            "ram_used_bytes_mean", sa.Numeric(precision=20, scale=0), nullable=False
        ),
        sa.Column(
            "ram_used_bytes_max", sa.Numeric(precision=20, scale=0), nullable=False
        ),
        sa.Column("ram_total_bytes", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column("gpu", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("disks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint(SAMPLES_CHECK, name=op.f("ck_resource_snapshot_samples")),
        sa.CheckConstraint(
            CPU_MEAN_CHECK, name=op.f("ck_resource_snapshot_cpu_percent_mean")
        ),
        sa.CheckConstraint(
            CPU_MAX_CHECK, name=op.f("ck_resource_snapshot_cpu_percent_max")
        ),
        sa.CheckConstraint(
            _UINT64.format(column="ram_used_bytes_mean"),
            name=op.f("ck_resource_snapshot_ram_used_bytes_mean"),
        ),
        sa.CheckConstraint(
            _UINT64.format(column="ram_used_bytes_max"),
            name=op.f("ck_resource_snapshot_ram_used_bytes_max"),
        ),
        sa.CheckConstraint(
            _UINT64.format(column="ram_total_bytes"),
            name=op.f("ck_resource_snapshot_ram_total_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.node_id"],
            name=op.f("fk_resource_snapshot_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "node_id", "bucket_start", name=op.f("pk_resource_snapshot")
        ),
    )
    op.create_index(
        "ix_resource_snapshot_bucket_start",
        "resource_snapshot",
        ["bucket_start"],
        unique=False,
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        "end_reason",
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({sql_in_list(SessionEndReason)})",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE node_session SET end_reason = 'protocol_error' "
            "WHERE end_reason = 'internal_error'"
        )
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        "end_reason",
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({_END_REASONS_0002})",
    )
    op.drop_index("ix_resource_snapshot_bucket_start", table_name="resource_snapshot")
    op.drop_table("resource_snapshot")
