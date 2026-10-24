"""node.display_name: the friendly name the laptop's owner gave it (V0.17 G3, Q27)

``Hello.display_name`` (the agent's local console, or ``[agent] node_name``),
written on every Hello like ``hostname``: an empty string = no name (the
dashboard shows the hostname). ``String(64)`` with a ``NOT NULL DEFAULT ''`` so
existing rows need no backfill; the controller refuses a longer or
non-printable value at admission (session/limits.py), the column is the second
line of defence.

Downgrade drops the column (development only).

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "node",
        sa.Column(
            "display_name",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("node", "display_name")
