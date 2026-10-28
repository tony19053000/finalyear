"""attempt.recorded + partial index on the unrecorded terminal attempts (V1.0 G4 F-4, D412)

The telemetry recorder sweeps every 2 s for "terminal attempts with no
``run_record``". That was an anti-join of every attempt ever run against every
record (a sequential scan of both tables, ~47 % of all database time in the G4
profile; 1.8 ms at 4 200 attempts, 18 ms at 50 000, growing with history). Now
the recorder sets ``attempt.recorded`` in the same transaction as the record and
reads its work list from ``ix_attempt_unrecorded``, a partial index that only
holds terminal attempts not yet recorded - a few rows whatever the history.

Upgrade:

1. adds ``recorded boolean NOT NULL DEFAULT false`` (a constant default: a
   catalogue-only change on PostgreSQL >= 11, no table rewrite);
2. backfills ``recorded = true`` for every attempt that already has a record -
   one set-based ``UPDATE``, not batches: the controller runs its migrations in
   one transaction at start-up, before it serves (the D393 pre-upgrade backup and
   rollback rely on that), so batches inside it would hold the same locks to the
   end and only add round trips; an attempt left ``false`` with a record is
   anyway healed by the recorder's next sweep (it marks it without writing);
3. builds the index - plainly, not ``CONCURRENTLY``: that cannot run inside the
   migration transaction, and a failed concurrent build leaves an invalid index
   behind that ``IF NOT EXISTS`` would then accept.

Locks (all in the start-up transaction, nothing else writes the database then):
step 1 takes ``ACCESS EXCLUSIVE`` on ``attempt`` for a moment; step 2 row-locks
the recorded attempts; step 3 holds ``SHARE`` on ``attempt`` (blocks writes,
not reads) while the index builds. Measured with the 4 200 / 50 000-attempt
bench databases: see ``docs/briefs/progress/V1.0-G4.md`` (F-4).

Downgrade drops the index and the column (the records stay; the recorder of an
older release finds them by the anti-join again).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNRECORDED = "NOT recorded AND state IN ('done', 'failed', 'fenced', 'lost')"
"""Frozen here (a migration never follows later model changes)."""


def upgrade() -> None:
    op.add_column(
        "attempt",
        sa.Column(
            "recorded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE attempt SET recorded = true WHERE EXISTS "
        "(SELECT 1 FROM run_record r WHERE r.attempt_id = attempt.id)"
    )
    op.create_index(
        "ix_attempt_unrecorded",
        "attempt",
        ["created_at", "id"],
        postgresql_where=sa.text(_UNRECORDED),
    )


def downgrade() -> None:
    op.drop_index("ix_attempt_unrecorded", table_name="attempt")
    op.drop_column("attempt", "recorded")
