"""split jobs: the resolved item list on ``job`` (V0.10 G1, roadmap 0.10.1, D260)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``job.split`` (JSONB, the canonical ``split:`` spec; NULL = not a split job),
  ``split_items_digest`` (the item-list CAS blob), ``split_item_count``,
  ``split_item_bytes``.
* CHECKs: the digest's shape, and "all four NULL, or a spec + digest + 1..100 000
  items".

Downgrade drops the columns (development only).

**Merge note (V0.10 G1 ∥ G2):** the brief reserves 0025 for G2's work queue.
This branch was cut before 0025 existed, so it chains from 0024; whoever merges
second sets ``down_revision = "0025"`` (the two touch different tables).

Revision ID: 0026
Revises: 0024
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later change of the Python constants must not change this).
MAX_SPLIT_ITEMS_0026 = 100_000
_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column("split", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "job", sa.Column("split_items_digest", sa.String(length=71), nullable=True)
    )
    op.add_column("job", sa.Column("split_item_count", sa.Integer(), nullable=True))
    op.add_column("job", sa.Column("split_item_bytes", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_job_split_items_digest"),
        "job",
        f"split_items_digest IS NULL OR split_items_digest ~ {_DIGEST_RE}",
    )
    op.create_check_constraint(
        op.f("ck_job_split_shape"),
        "job",
        "(split IS NULL AND split_items_digest IS NULL "
        "AND split_item_count IS NULL AND split_item_bytes IS NULL) OR "
        "(split IS NOT NULL AND split_items_digest IS NOT NULL "
        f"AND split_item_count >= 1 AND split_item_count <= {MAX_SPLIT_ITEMS_0026} "
        "AND (split_item_bytes IS NULL OR split_item_bytes >= 0))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_job_split_shape"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_split_items_digest"), "job", type_="check")
    op.drop_column("job", "split_item_bytes")
    op.drop_column("job", "split_item_count")
    op.drop_column("job", "split_items_digest")
    op.drop_column("job", "split")
