"""chunk_record.stalled_s (V1.0 G10 BUG-4, D451)

The longest "no progress" time a lane reported for a chunk lease
(``ChunkLeaseRef.stalled_s`` on ``LeaseRenew``) is kept with the lease's chunk
record, so a hung try shows in the chunk telemetry. Operational, like
``stderr_tail``: not a dataset column (the telemetry schema version is
unchanged).

Upgrade adds a nullable ``double precision`` column (catalogue-only on
PostgreSQL >= 11, no rewrite); downgrade drops it.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunk_record", sa.Column("stalled_s", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunk_record", "stalled_s")
