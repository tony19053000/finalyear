"""attempt outputs and final logs; failure type output_error (V0.4 G5, D71/D74)

* ``job.failure_type`` / ``attempt.failure_type`` CHECKs widened by
  ``output_error`` (the workload finished, its declared outputs could not be
  collected or uploaded - D74).
* ``attempt.missing_outputs`` (JSONB list of strings, nullable) - declared output
  globs that matched nothing; listed, not an error (D54/D74).
* ``attempt.logs`` (JSONB, nullable) - the attempt's final logs (D71):
  ``{"stdout": {"digest": "sha256:...", "bytes": N, "truncated": bool}, ...}``.
  The digest names a blob this attempt's upload grant stored and verified.

The value lists are frozen literals (see 0006): a later failure type is a new
migration.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE = (
    "'user_error', 'oom', 'vram_oom', 'env_error', 'disk_full', 'node_lost', "
    "'gpu_session_lost', 'transfer_error', 'integrity_error', 'fenced', 'cancelled', "
    "'timeout', 'deadline_exceeded', 'resource_busy', 'no_feasible_plan', "
    "'network_error', 'config_error', 'invalid_input', 'unauthorized', 'not_found', "
    "'conflict', 'internal_error'"
)
FAILURE_TYPES_0006 = _BASE
FAILURE_TYPES_0008 = _BASE + ", 'output_error'"


def _failure_checks(values: str) -> None:
    for table in ("job", "attempt"):
        op.drop_constraint(op.f(f"ck_{table}_failure_type"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_failure_type"),
            table,
            f"failure_type IS NULL OR failure_type IN ({values})",
        )


def upgrade() -> None:
    _failure_checks(FAILURE_TYPES_0008)
    op.add_column(
        "attempt",
        sa.Column(
            "missing_outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "attempt",
        sa.Column("logs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attempt", "logs")
    op.drop_column("attempt", "missing_outputs")
    op.execute(
        "UPDATE job SET failure_type = 'internal_error' "
        "WHERE failure_type = 'output_error'"
    )
    op.execute(
        "UPDATE attempt SET failure_type = 'internal_error' "
        "WHERE failure_type = 'output_error'"
    )
    _failure_checks(FAILURE_TYPES_0006)
