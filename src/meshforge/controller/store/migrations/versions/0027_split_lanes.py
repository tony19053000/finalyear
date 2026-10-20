"""split jobs run as lanes: ``attempt.role`` and the parts index (V0.10 G4,
roadmap 0.10.4 / 0.10.6, D259/D261)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``attempt.role`` (``run`` | ``lane`` | ``combine``; default ``run`` - every
  existing attempt ran a whole job). A Mode 3 job runs one ``lane`` attempt per
  node; G5 adds the ``combine`` attempt.
* ``ux_attempt_live_lane``: at most one LIVE lane of a job per node (D267f) -
  the database refuses a second one even if the dispatcher had a bug.
* ``job.split_parts_digest``: the canonical parts index (every chunk's result
  digest in chunk order), stored as a CAS blob in the transaction of the
  job's last chunk commit; only a split job has one.

Downgrade drops them (development only).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later change of the Python constants must not change this).
_ROLES_0027 = "'run', 'lane', 'combine'"
_LIVE_0027 = "'assigned', 'staging', 'env_ready', 'running', 'uploading'"
_DIGEST_RE = "'^sha256:[0-9a-f]{64}$'"


def upgrade() -> None:
    op.add_column(
        "attempt",
        sa.Column(
            "role", sa.String(length=8), server_default=sa.text("'run'"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_attempt_role"), "attempt", f"role IN ({_ROLES_0027})"
    )
    op.create_index(
        "ux_attempt_live_lane",
        "attempt",
        ["job_id", "node_id"],
        unique=True,
        postgresql_where=sa.text(f"role = 'lane' AND state IN ({_LIVE_0027})"),
    )
    op.add_column(
        "job", sa.Column("split_parts_digest", sa.String(length=71), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_job_split_parts_digest"),
        "job",
        "split_parts_digest IS NULL OR (split IS NOT NULL "
        f"AND split_parts_digest ~ {_DIGEST_RE})",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_job_split_parts_digest"), "job", type_="check")
    op.drop_column("job", "split_parts_digest")
    op.drop_index("ux_attempt_live_lane", table_name="attempt")
    op.drop_constraint(op.f("ck_attempt_role"), "attempt", type_="check")
    op.drop_column("attempt", "role")
