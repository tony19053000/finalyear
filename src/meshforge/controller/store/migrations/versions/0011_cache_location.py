"""cache_location: which node holds which blob / tree / env / image (V0.5 G4, D101)

Hand-written to match :mod:`meshforge.controller.store.models.cache` exactly
(``alembic check`` in the component tests fails on drift). Primary key
``(node_id, kind, key)``; an index on ``(kind, key)`` serves the dispatcher's
"who holds tree X" query (D100); rows go with their node (``ON DELETE CASCADE``).
The table is a cache of what agents report - the downgrade simply drops it.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from meshforge.controller.store.base import UINT64_MAX
from meshforge.controller.store.models.cache import KIND_CHECK

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cache_location",
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("size", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(KIND_CHECK, name=op.f("ck_cache_location_kind")),
        sa.CheckConstraint(
            f"size >= 0 AND size <= {UINT64_MAX}", name=op.f("ck_cache_location_size")
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.node_id"],
            name=op.f("fk_cache_location_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "node_id", "kind", "key", name=op.f("pk_cache_location")
        ),
    )
    op.create_index(
        "ix_cache_location_kind_key", "cache_location", ["kind", "key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_cache_location_kind_key", table_name="cache_location")
    op.drop_table("cache_location")
