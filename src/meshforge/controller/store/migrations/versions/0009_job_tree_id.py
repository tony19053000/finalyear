"""job.tree_id: jobs reference a tree manifest (V0.5 G1, D83)

* ``job.tree_id`` (``sha256:<64 hex>``, nullable) - the project's tree manifest
  (D81) in the content-addressed store; set for every job submitted from V0.5 on.
* ``job.bundle_digest`` becomes nullable: kept for V0.4 rows (and the legacy
  bundle submit path); a tree job has none.
* CHECK ``job_source``: at least one of the two is set.

Downgrade deletes tree-only jobs (V0.4 cannot run them; their attempts go with
them by the existing cascade) before restoring ``bundle_digest NOT NULL``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIGEST = "^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    op.add_column("job", sa.Column("tree_id", sa.String(length=71), nullable=True))
    op.create_check_constraint(
        op.f("ck_job_tree_id"),
        "job",
        f"tree_id IS NULL OR tree_id ~ '{DIGEST}'",
    )
    op.alter_column("job", "bundle_digest", existing_type=sa.String(71), nullable=True)
    op.drop_constraint(op.f("ck_job_bundle_digest"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_bundle_digest"),
        "job",
        f"bundle_digest IS NULL OR bundle_digest ~ '{DIGEST}'",
    )
    op.create_check_constraint(
        op.f("ck_job_job_source"),
        "job",
        "bundle_digest IS NOT NULL OR tree_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_job_job_source"), "job", type_="check")
    op.execute("DELETE FROM job WHERE bundle_digest IS NULL")
    op.drop_constraint(op.f("ck_job_bundle_digest"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_bundle_digest"), "job", f"bundle_digest ~ '{DIGEST}'"
    )
    op.alter_column("job", "bundle_digest", existing_type=sa.String(71), nullable=False)
    op.drop_constraint(op.f("ck_job_tree_id"), "job", type_="check")
    op.drop_column("job", "tree_id")
