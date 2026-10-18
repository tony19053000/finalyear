"""telemetry schema v2: result version + benchmark condition (V0.9 G4, D233.3/D241)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

``run_record`` gains three nullable columns (``controller/telemetry/schema.py``
v2): ``result_schema_version`` (the ``schema_version`` of the workload's
``results/workload.json`` the agent read), ``condition`` (the run's benchmark
condition, canonical ``name[:arg]`` string, D241) and ``condition_measured``
(JSONB: what was measured when the condition was applied and verified, D217).
Rows written before this migration keep ``schema_version`` 1 and NULL in all
three - they are not rewritten (a record is never changed after it is written).

Downgrade drops the three columns (v2 rows then lose them; their
``schema_version`` still says 2 - downgrade is for development only).

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONDITION_CHARS_0023 = 128


def upgrade() -> None:
    op.add_column(
        "run_record",
        sa.Column("result_schema_version", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "run_record",
        sa.Column("condition", sa.String(length=CONDITION_CHARS_0023), nullable=True),
    )
    op.add_column(
        "run_record",
        sa.Column(
            "condition_measured",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("run_record", "condition_measured")
    op.drop_column("run_record", "condition")
    op.drop_column("run_record", "result_schema_version")
