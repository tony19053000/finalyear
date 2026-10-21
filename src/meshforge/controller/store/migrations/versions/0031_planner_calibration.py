"""planner_calibration: versioned planner calibrations (V0.14 G2, D293/D301/D306)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``planner_calibration`` - one row per fitted calibration: ``version``
  (identity, the key ``plan_decision.calibration_version`` names), ``status``
  (``active`` / ``rejected`` / ``superseded`` / ``pinned``), ``factors`` and
  ``fit_inputs`` (JSON objects), the two held-out errors of the D306 gate and
  the rejection ``reason``.
* ``ux_planner_calibration_active`` - a partial unique index: **at most one**
  ``active`` row, whatever two refreshes do at once.

No existing row changes. Downgrade drops the table (development only).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
STATUSES_0031 = "'active', 'rejected', 'superseded', 'pinned'"
MAX_REASON_0031 = 512
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "planner_calibration",
        sa.Column(
            "version", sa.BigInteger(), sa.Identity(always=False), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("factors", _JSONB, nullable=False),
        sa.Column("fit_inputs", _JSONB, nullable=False),
        sa.Column("holdout_error_new", sa.Float(), nullable=True),
        sa.Column("holdout_error_active", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({STATUSES_0031})", name=op.f("ck_planner_calibration_status")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(factors) = 'object'",
            name=op.f("ck_planner_calibration_factors"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fit_inputs) = 'object'",
            name=op.f("ck_planner_calibration_fit_inputs"),
        ),
        sa.CheckConstraint(
            "holdout_error_new IS NULL OR holdout_error_new >= 0",
            name=op.f("ck_planner_calibration_error_new"),
        ),
        sa.CheckConstraint(
            "holdout_error_active IS NULL OR holdout_error_active >= 0",
            name=op.f("ck_planner_calibration_error_active"),
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_REASON_0031}",
            name=op.f("ck_planner_calibration_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR reason IS NOT NULL",
            name=op.f("ck_planner_calibration_rejected_has_reason"),
        ),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_planner_calibration")),
    )
    op.create_index(
        "ux_planner_calibration_active",
        "planner_calibration",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ux_planner_calibration_active", table_name="planner_calibration")
    op.drop_table("planner_calibration")
