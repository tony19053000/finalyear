"""baseline: root of the migration chain (no domain tables yet)

Deliberately empty. It establishes the single root every later migration builds on,
and ``upgrade head`` / ``downgrade base`` exercise the whole toolchain (async env,
settings-based URL, ``alembic_version`` bookkeeping). Domain tables (fabric, nodes,
jobs, ...; ARCHITECTURE section 19) are added by the versions that implement them, so
their columns are designed with the feature instead of guessed now.

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
