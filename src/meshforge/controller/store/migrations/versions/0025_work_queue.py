"""shared leased work queue (V0.10 G2, roadmap 0.10.2, D262)

Hand-written to match :mod:`meshforge.workqueue.tables` exactly (``alembic
check`` in the component tests fails on drift).

* ``work_queue`` - one queue per opaque owner ``(kind, owner_id)``, its policy
  and the counters kept under the queue row lock.
* ``work_item`` - one row per item (``idx`` order, opaque JSON payload).
* ``work_lease`` - every lease ever issued; the partial unique index
  ``ix_work_lease_live_slot`` allows one ordinary and one speculative live lease
  per item.

Downgrade drops the three tables (development only).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
QUEUE_STATES_0025 = "'open', 'done', 'failed', 'cancelled'"
ITEM_STATES_0025 = "'pending', 'leased', 'done', 'failed'"
LEASE_STATES_0025 = "'live', 'completed', 'failed', 'expired', 'released', 'superseded'"


def upgrade() -> None:
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "work_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("visibility_timeout_s", sa.Float(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("max_failed_sites", sa.Integer(), nullable=True),
        sa.Column("count_expiry", sa.Boolean(), nullable=False),
        sa.Column("speculative_max_frac", sa.Float(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column(
            "done_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "speculated_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_at", tz, server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", tz, nullable=True),
        sa.CheckConstraint(
            f"state IN ({QUEUE_STATES_0025})", name=op.f("ck_work_queue_state")
        ),
        sa.CheckConstraint(
            "visibility_timeout_s > 0 AND visibility_timeout_s <= 86400",
            name=op.f("ck_work_queue_visibility_timeout_s"),
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 100", name=op.f("ck_work_queue_max_attempts")
        ),
        sa.CheckConstraint(
            "max_failed_sites IS NULL OR max_failed_sites BETWEEN 1 AND 100",
            name=op.f("ck_work_queue_max_failed_sites"),
        ),
        sa.CheckConstraint(
            "speculative_max_frac >= 0 AND speculative_max_frac <= 1",
            name=op.f("ck_work_queue_speculative_max_frac"),
        ),
        sa.CheckConstraint("item_count >= 1", name=op.f("ck_work_queue_item_count")),
        sa.CheckConstraint(
            "done_count BETWEEN 0 AND item_count",
            name=op.f("ck_work_queue_done_count"),
        ),
        sa.CheckConstraint(
            "speculated_count BETWEEN 0 AND item_count",
            name=op.f("ck_work_queue_speculated_count"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_queue")),
        sa.UniqueConstraint(
            "kind", "owner_id", name=op.f("uq_work_queue_kind_owner_id")
        ),
    )
    op.create_table(
        "work_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("queue_id", sa.BigInteger(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "lease_seq", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "speculations", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("result_digest", sa.String(length=128), nullable=True),
        sa.Column("done_lease_seq", sa.Integer(), nullable=True),
        sa.Column("failed_kind", sa.String(length=32), nullable=True),
        sa.Column(
            "failed_sites",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("done_at", tz, nullable=True),
        sa.CheckConstraint(
            f"state IN ({ITEM_STATES_0025})", name=op.f("ck_work_item_state")
        ),
        sa.CheckConstraint("idx >= 0", name=op.f("ck_work_item_idx")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_work_item_attempts")),
        sa.CheckConstraint("lease_seq >= 0", name=op.f("ck_work_item_lease_seq")),
        sa.CheckConstraint("speculations >= 0", name=op.f("ck_work_item_speculations")),
        sa.CheckConstraint(
            "(state = 'done') = (result_digest IS NOT NULL AND done_lease_seq IS NOT NULL)",
            name=op.f("ck_work_item_done_result"),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (failed_kind IS NOT NULL)",
            name=op.f("ck_work_item_failed_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["work_queue.id"],
            name=op.f("fk_work_item_queue_id_work_queue"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_item")),
        sa.UniqueConstraint("queue_id", "idx", name=op.f("uq_work_item_queue_id_idx")),
    )
    op.create_index(
        "ix_work_item_pending",
        "work_item",
        ["queue_id", "idx"],
        unique=False,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_work_item_queue_id_state",
        "work_item",
        ["queue_id", "state"],
        unique=False,
    )
    op.create_table(
        "work_lease",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("site", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("speculative", sa.Boolean(), nullable=False),
        sa.Column("leased_at", tz, nullable=False),
        sa.Column("deadline", tz, nullable=False),
        sa.Column("ended_at", tz, nullable=True),
        sa.CheckConstraint(
            f"state IN ({LEASE_STATES_0025})", name=op.f("ck_work_lease_state")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_work_lease_seq")),
        sa.CheckConstraint(
            "deadline >= leased_at", name=op.f("ck_work_lease_deadline")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["work_item.id"],
            name=op.f("fk_work_lease_item_id_work_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_lease")),
        sa.UniqueConstraint("item_id", "seq", name=op.f("uq_work_lease_item_id_seq")),
    )
    op.create_index(
        "ix_work_lease_live_slot",
        "work_lease",
        ["item_id", "speculative"],
        unique=True,
        postgresql_where=sa.text("state = 'live'"),
    )
    op.create_index(
        "ix_work_lease_live_deadline",
        "work_lease",
        ["deadline"],
        unique=False,
        postgresql_where=sa.text("state = 'live'"),
    )
    op.create_index(
        "ix_work_lease_live_holder",
        "work_lease",
        ["holder"],
        unique=False,
        postgresql_where=sa.text("state = 'live'"),
    )


def downgrade() -> None:
    op.drop_index("ix_work_lease_live_holder", table_name="work_lease")
    op.drop_index("ix_work_lease_live_deadline", table_name="work_lease")
    op.drop_index("ix_work_lease_live_slot", table_name="work_lease")
    op.drop_table("work_lease")
    op.drop_index("ix_work_item_queue_id_state", table_name="work_item")
    op.drop_index("ix_work_item_pending", table_name="work_item")
    op.drop_table("work_item")
    op.drop_table("work_queue")
