"""plan_decision replay snapshot + objective + calibration version (V0.14 G1, D292)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``plan_decision.replay`` (JSONB, nullable): the decision's versioned replay
  snapshot (``controller/planner/replay.py``) - the planner's exact inputs, so
  ``plan()`` on it reproduces the decision and can be re-run with another
  policy (the V0.14 evaluator). NULL = a row written before V0.14, or a replay
  over ``[controller.planner] replay_max_bytes`` (counted and logged).
* ``plan_decision.replay_version`` (SMALLINT): the replay format's version;
  set exactly when ``replay`` is.
* ``plan_decision.objective`` (TEXT): the D295 objective the decision ranked
  by - ``min_time`` for every row until V0.14 G3 adds the others; NULL = a row
  from before V0.14.
* ``plan_decision.calibration_version`` (BIGINT): the D293 calibration the
  estimates were scaled by - NULL until V0.14 G2 (uncalibrated).

Existing rows keep NULL in all four (a decision is never rewritten).
Downgrade drops the columns (development only).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
OBJECTIVES_0030 = "'min_time', 'min_network', 'prefer_local'"
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("plan_decision", sa.Column("replay", _JSONB, nullable=True))
    op.add_column(
        "plan_decision", sa.Column("replay_version", sa.SmallInteger(), nullable=True)
    )
    op.add_column("plan_decision", sa.Column("objective", sa.Text(), nullable=True))
    op.add_column(
        "plan_decision",
        sa.Column("calibration_version", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_plan_decision_replay"),
        "plan_decision",
        "(replay IS NULL) = (replay_version IS NULL) AND "
        "(replay IS NULL OR jsonb_typeof(replay) = 'object') AND "
        "(replay_version IS NULL OR replay_version >= 1)",
    )
    op.create_check_constraint(
        op.f("ck_plan_decision_objective"),
        "plan_decision",
        f"objective IS NULL OR objective IN ({OBJECTIVES_0030})",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_plan_decision_objective"), "plan_decision", type_="check"
    )
    op.drop_constraint(op.f("ck_plan_decision_replay"), "plan_decision", type_="check")
    for name in ("calibration_version", "objective", "replay_version", "replay"):
        op.drop_column("plan_decision", name)
