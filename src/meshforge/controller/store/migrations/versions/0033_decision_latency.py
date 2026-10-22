"""plan_decision.decision_ms: stored decision latency (V0.14 G6, D324)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``plan_decision.decision_ms`` (REAL, NULL): the wall time of the
  dispatcher's decision in milliseconds (criterion 3 reads it through the
  evaluator's ``latency`` block). NULL for every row written before 0033.
* ``ck_plan_decision_decision_ms`` - never negative.

No existing row changes. Downgrade drops the column (development only).

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plan_decision", sa.Column("decision_ms", sa.REAL(), nullable=True))
    op.create_check_constraint(
        op.f("ck_plan_decision_decision_ms"),
        "plan_decision",
        "decision_ms IS NULL OR decision_ms >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_plan_decision_decision_ms"), "plan_decision", type_="check"
    )
    op.drop_column("plan_decision", "decision_ms")
