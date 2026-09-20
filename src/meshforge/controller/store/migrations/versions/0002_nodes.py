"""nodes: node registry tables ``node`` and ``node_session`` (roadmap 0.3.6)

Hand-written to match :mod:`meshforge.controller.store.models.node` exactly
(``alembic check`` in the component tests fails on drift). CHECK constraints are
named through the metadata naming convention (``ck_<table>_<name>``).

``session_epoch`` is ``NUMERIC(20, 0)`` with a ``[0, 2^64-1]`` CHECK: it mirrors a
protobuf ``uint64`` and ``bigint`` (signed) would refuse the upper half (G2 tester
DEFECT-2). Amended in place during the G2 fix round - this revision had only been
applied by test databases, never on a lab machine.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from meshforge.controller.liveness import NodeState
from meshforge.controller.store.base import UINT64_MAX
from meshforge.controller.store.models.node import SessionEndReason, sql_in_list

SESSION_EPOCH_CHECK = f"session_epoch >= 0 AND session_epoch <= {UINT64_MAX}"

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node",
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("os", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("hardware", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_session_id", sa.String(length=40), nullable=True),
        sa.Column("session_epoch", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"state IN ({sql_in_list(NodeState)})", name=op.f("ck_node_state")
        ),
        sa.CheckConstraint(SESSION_EPOCH_CHECK, name=op.f("ck_node_session_epoch")),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_node")),
    )
    op.create_table(
        "node_session",
        sa.Column("session_id", sa.String(length=40), nullable=False),
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("session_epoch", sa.Numeric(precision=20, scale=0), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("peer_address", sa.String(length=128), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("last_rtt_ms", sa.REAL(), nullable=True),
        sa.Column("rtt_ewma_ms", sa.REAL(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"end_reason IS NULL OR end_reason IN ({sql_in_list(SessionEndReason)})",
            name=op.f("ck_node_session_end_reason"),
        ),
        sa.CheckConstraint(
            SESSION_EPOCH_CHECK, name=op.f("ck_node_session_session_epoch")
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.node_id"],
            name=op.f("fk_node_session_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_node_session")),
    )
    op.create_index(
        "ix_node_session_node_id_started_at",
        "node_session",
        ["node_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_node_session_node_id_started_at", table_name="node_session")
    op.drop_table("node_session")
    op.drop_table("node")
