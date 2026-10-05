"""network matrix + estimator inputs (V0.6 G4, D108/D111)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``network_link`` - one EWMA row per ordered node pair measured by peer
  probes (D111; see :mod:`meshforge.controller.store.models.network`);
* ``attempt.env_hash`` - the agent's env hash from the terminal report (the
  estimator's "environment cached on this node" check, D108);
* ``attempt.output_bytes`` - bytes uploaded under the attempt's grant on DONE
  (the estimator's ``out_bytes``, D108);
* index ``ix_transfer_event_source_id`` - the newest controller transfers per
  dispatch tick (controller -> node bandwidth, D111) without a table scan.

Downgrade drops all of it (measurements are lost; nothing else depends on it).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from meshforge.controller.store.base import UINT64_MAX

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "network_link",
        sa.Column("src_node_id", sa.String(length=40), nullable=False),
        sa.Column("dst_node_id", sa.String(length=40), nullable=False),
        sa.Column("rtt_ms", sa.Float(), nullable=False),
        sa.Column("bw_bytes_s", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "src_node_id <> dst_node_id", name=op.f("ck_network_link_distinct_nodes")
        ),
        sa.CheckConstraint("rtt_ms >= 0", name=op.f("ck_network_link_rtt_ms")),
        sa.CheckConstraint("bw_bytes_s > 0", name=op.f("ck_network_link_bw_bytes_s")),
        sa.CheckConstraint("samples >= 1", name=op.f("ck_network_link_samples")),
        sa.ForeignKeyConstraint(
            ["src_node_id"],
            ["node.node_id"],
            name=op.f("fk_network_link_src_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dst_node_id"],
            ["node.node_id"],
            name=op.f("fk_network_link_dst_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "src_node_id", "dst_node_id", name=op.f("pk_network_link")
        ),
    )
    op.create_index(
        "ix_transfer_event_source_id",
        "transfer_event",
        ["source", "id"],
        unique=False,
    )
    op.add_column("attempt", sa.Column("env_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "attempt",
        sa.Column("output_bytes", sa.Numeric(precision=20, scale=0), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_attempt_env_hash"),
        "attempt",
        "env_hash IS NULL OR env_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_attempt_output_bytes"),
        "attempt",
        f"output_bytes >= 0 AND output_bytes <= {UINT64_MAX}",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_attempt_output_bytes"), "attempt", type_="check")
    op.drop_constraint(op.f("ck_attempt_env_hash"), "attempt", type_="check")
    op.drop_column("attempt", "output_bytes")
    op.drop_column("attempt", "env_hash")
    op.drop_index("ix_transfer_event_source_id", table_name="transfer_event")
    op.drop_table("network_link")
