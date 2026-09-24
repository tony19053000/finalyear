"""node.hardware_scores: the node's newest hardware-score run (V0.3 G7, D38)

One nullable JSONB column on ``node`` (roadmap 0.3.4, brief §5g D37/D38). ``NULL``
means "not measured for this node's current hardware": every ``Hello`` rewrites the
column (with the scores it carries, or ``NULL`` when it carries none), and a
mid-session ``hardware_scores`` report replaces it only when the write is fenced
on the node's current session and the run is not older than the stored one
(:meth:`meshforge.controller.registry.Registry.set_hardware_scores`).

The document is validated at ingestion (``controller/session/limits.py``
``check_hardware_scores``) and shaped by ``controller/scores.py``; the database
does not re-check it. The downgrade drops the column - scores are re-sent by every
agent's next ``Hello`` and are cheap to lose.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "node",
        sa.Column(
            "hardware_scores",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("node", "hardware_scores")
