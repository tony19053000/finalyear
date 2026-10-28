"""attempt: partial index on the live (non-terminal) attempts (V1.0 G4, D412)

The controller polls the live attempts about once a second (dispatcher and
reconcile reads, the timeout check, the start-snapshot read); each filtered on
the non-terminal states and was a sequential scan of every attempt ever run, so
its cost grew with history (4 200 attempts: 1.2 / 1.2 / 8.2 ms -> 0.06 / 0.04 /
0.01 ms with this index, EXPLAIN ANALYZE). The index only holds live rows, so it
stays a few entries whatever the history. The predicate is the same state list
as ``ux_attempt_live_lane``.

Lock: a plain ``CREATE INDEX`` (not ``CONCURRENTLY``, which cannot run in the
migration transaction) holds a ``SHARE`` lock on ``attempt`` while it builds -
writes wait, reads do not. The build scans the table once (tens of
milliseconds at a few thousand attempts) and runs in the controller's start-up
migration transaction, before it serves.

Downgrade drops the index.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE = "state IN ('assigned', 'staging', 'env_ready', 'running', 'uploading')"
"""Frozen here (a migration never follows later model changes)."""


def upgrade() -> None:
    op.create_index(
        "ix_attempt_live_created_at",
        "attempt",
        ["created_at"],
        postgresql_where=sa.text(_LIVE),
    )


def downgrade() -> None:
    op.drop_index("ix_attempt_live_created_at", table_name="attempt")
