"""a split job's combine (V0.10 G5, roadmap 0.10.7, D268/D269/D283)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``job.failure_type`` / ``attempt.failure_type`` CHECKs widened by
  ``combine_error`` (the combine refused its parts, or the combine script
  failed - a data error, never retried).
* ``job.split_combine_ms``: the combine attempt's run time (its ``run``
  phase, measured on the agent) once the job SUCCEEDED - the combine term of
  the Mode 3 records (G8).
* ``job.warnings``: warnings that do not fail the job - V0.10's
  ``nondeterministic_chunk`` (D269: two results of one chunk with different
  digests; the winner is kept). A small JSON list, bounded by its writer
  (``controller.split.warnings``).

Downgrade: ``combine_error`` becomes ``internal_error``, the columns are
dropped (development only).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
_BASE = (
    "'user_error', 'oom', 'vram_oom', 'env_error', 'disk_full', 'node_lost', "
    "'gpu_session_lost', 'transfer_error', 'integrity_error', 'fenced', 'cancelled', "
    "'timeout', 'deadline_exceeded', 'resource_busy', 'no_feasible_plan', "
    "'network_error', 'config_error', 'invalid_input', 'unauthorized', 'not_found', "
    "'conflict', 'internal_error', 'output_error'"
)
FAILURE_TYPES_0024 = _BASE + ", 'owner_preempted'"
FAILURE_TYPES_0028 = FAILURE_TYPES_0024 + ", 'combine_error'"


def _failure_checks(values: str) -> None:
    for table in ("job", "attempt"):
        op.drop_constraint(op.f(f"ck_{table}_failure_type"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_failure_type"),
            table,
            f"failure_type IS NULL OR failure_type IN ({values})",
        )


def upgrade() -> None:
    _failure_checks(FAILURE_TYPES_0028)
    op.add_column("job", sa.Column("split_combine_ms", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_job_split_combine_ms"),
        "job",
        "split_combine_ms IS NULL OR split_combine_ms >= 0",
    )
    op.add_column(
        "job",
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job", "warnings")
    op.drop_constraint(op.f("ck_job_split_combine_ms"), "job", type_="check")
    op.drop_column("job", "split_combine_ms")
    for table in ("job", "attempt"):
        op.execute(
            f"UPDATE {table} SET failure_type = 'internal_error' "  # noqa: S608 - constant table names
            "WHERE failure_type = 'combine_error'"
        )
    _failure_checks(FAILURE_TYPES_0024)
