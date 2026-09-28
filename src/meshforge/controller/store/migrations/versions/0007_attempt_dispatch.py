"""attempt dispatch bookkeeping: start ack, runtime, skipped phases, cancel requests (V0.4 G4)

Four nullable/defaulted columns, no data rewrite:

* ``attempt.start_acked_at`` - when the agent acknowledged this attempt's
  ``StartAttempt`` (D64/D66). Reconciliation after a reconnect or a controller
  restart splits on it: never acked -> resend the same attempt; acked but not held
  by the agent -> ``LOST`` (re-running it could duplicate an execution).
* ``attempt.runtime`` - ``docker`` / ``subprocess``, as the agent reported it.
* ``attempt_phase.skipped`` - a phase that never ran is stored with
  ``duration_ms = 0`` **and** ``skipped = true`` (D68), never as a missing row or an
  invented number.
* ``job.cancel_requested_at`` - ``POST /v1/jobs/{id}/cancel`` on a job whose attempt
  is live (D69); kept so a cancel survives a lost stream or a controller restart
  (the ``CancelAttempt`` is re-sent when the agent is reachable again).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIMES = "'docker', 'subprocess'"


def upgrade() -> None:
    op.add_column(
        "attempt",
        sa.Column("start_acked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("attempt", sa.Column("runtime", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_attempt_runtime"),
        "attempt",
        f"runtime IS NULL OR runtime IN ({RUNTIMES})",
    )
    op.add_column(
        "attempt_phase",
        sa.Column(
            "skipped", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "job",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job", "cancel_requested_at")
    op.drop_column("attempt_phase", "skipped")
    op.drop_constraint(op.f("ck_attempt_runtime"), "attempt", type_="check")
    op.drop_column("attempt", "runtime")
    op.drop_column("attempt", "start_acked_at")
