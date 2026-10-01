"""attempt.runtime may be ``venv`` (V0.5 G2, D86)

The agent's venv runtime (an unsandboxed process in a cached uv venv) reports
``AttemptStatus.runtime = "venv"``; the CHECK on ``attempt.runtime`` widens from
``docker`` / ``subprocess`` to include it.

Downgrade maps ``venv`` rows to ``subprocess`` (the closest V0.4 meaning: the same
unsandboxed process runtime) before restoring the narrower CHECK.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_RUNTIMES = "'docker', 'subprocess'"
NEW_RUNTIMES = "'docker', 'subprocess', 'venv'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_attempt_runtime"), "attempt", type_="check")
    op.create_check_constraint(
        op.f("ck_attempt_runtime"),
        "attempt",
        f"runtime IS NULL OR runtime IN ({NEW_RUNTIMES})",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_attempt_runtime"), "attempt", type_="check")
    op.execute("UPDATE attempt SET runtime = 'subprocess' WHERE runtime = 'venv'")
    op.create_check_constraint(
        op.f("ck_attempt_runtime"),
        "attempt",
        f"runtime IS NULL OR runtime IN ({OLD_RUNTIMES})",
    )
