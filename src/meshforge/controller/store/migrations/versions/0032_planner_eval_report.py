"""planner_eval_report: stored planner evaluation reports (V0.14 G4, D318)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``planner_eval_report`` - one row per evaluation: ``id`` (identity),
  ``created_at``, ``campaign_ids`` (JSON array), ``holdout`` (one workload
  family or NULL), ``leaked`` and the ``report`` (JSON object,
  ``eval_version`` 1).
* ``ix_planner_eval_report_created_at`` - the list reads newest first.

No existing row changes. Downgrade drops the table (development only).

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy (a later migration that changes the bound must not change this one).
MAX_HOLDOUT_0032 = 64
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "planner_eval_report",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("campaign_ids", _JSONB, nullable=False),
        sa.Column("holdout", sa.Text(), nullable=True),
        sa.Column("leaked", sa.Boolean(), nullable=False),
        sa.Column("report", _JSONB, nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(campaign_ids) = 'array'",
            name=op.f("ck_planner_eval_report_campaign_ids"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object'",
            name=op.f("ck_planner_eval_report_report"),
        ),
        sa.CheckConstraint(
            f"holdout IS NULL OR (length(holdout) BETWEEN 1 AND {MAX_HOLDOUT_0032})",
            name=op.f("ck_planner_eval_report_holdout"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planner_eval_report")),
    )
    op.create_index(
        "ix_planner_eval_report_created_at",
        "planner_eval_report",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_planner_eval_report_created_at", table_name="planner_eval_report")
    op.drop_table("planner_eval_report")
