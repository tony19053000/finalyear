"""pairing: join_token, audit_event, node certificate columns (V0.7 G2, D135/D136)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``join_token`` - one row per invite; the token is stored as its SHA-256 only
  (see :mod:`meshforge.controller.store.models.enrollment`);
* ``audit_event`` - invite created/revoked, enrolment accepted/refused (D151);
* ``node``: ``cert_serial`` (unique), ``cert_fingerprint``, ``cert_not_after``,
  ``public_key_fingerprint`` - all NULL for a node that never enrolled (every
  node from before this migration), all set together otherwise.

Downgrade drops all of it: invites and the audit trail are lost and every node
forgets its certificate (the agents keep theirs; re-running the upgrade does
not restore the columns - nodes re-join, or the next enrolment fills them).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds an action must not change this one).
_ACTIONS = "'invite_created', 'invite_revoked', 'enrolled', 'enroll_refused'"
MAX_AUDIT_DETAILS_BYTES = 4096
_HEX64 = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.create_table(
        "join_token",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_node", sa.String(length=40), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replace_node_id", sa.String(length=40), nullable=True),
        sa.CheckConstraint(
            f"token_hash ~ {_HEX64}", name=op.f("ck_join_token_token_hash")
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_join_token_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "(used_at IS NULL) = (used_by_node IS NULL)",
            name=op.f("ck_join_token_used_names_node"),
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_join_token_used_or_revoked"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_join_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_join_token_token_hash")),
    )
    op.create_index(
        "ix_join_token_expires_at", "join_token", ["expires_at"], unique=False
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=40), nullable=True),
        sa.Column("invite_id", sa.String(length=40), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            f"action IN ({_ACTIONS})", name=op.f("ck_audit_event_action")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object' AND "
            f"octet_length(details::text) <= {MAX_AUDIT_DETAILS_BYTES}",
            name=op.f("ck_audit_event_details"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index("ix_audit_event_at", "audit_event", ["at"], unique=False)
    op.create_index("ix_audit_event_node_id", "audit_event", ["node_id"], unique=False)
    op.add_column("node", sa.Column("cert_serial", sa.String(length=40), nullable=True))
    op.add_column(
        "node", sa.Column("cert_fingerprint", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "node",
        sa.Column("cert_not_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "node",
        sa.Column("public_key_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(op.f("uq_node_cert_serial"), "node", ["cert_serial"])
    op.create_check_constraint(
        op.f("ck_node_cert_all_or_none"),
        "node",
        "(cert_serial IS NULL) = (cert_fingerprint IS NULL) AND "
        "(cert_serial IS NULL) = (cert_not_after IS NULL) AND "
        "(cert_serial IS NULL) = (public_key_fingerprint IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_node_cert_serial"),
        "node",
        "cert_serial IS NULL OR cert_serial ~ '^[0-9a-f]{1,40}$'",
    )
    op.create_check_constraint(
        op.f("ck_node_cert_fingerprint"),
        "node",
        f"cert_fingerprint IS NULL OR cert_fingerprint ~ {_HEX64}",
    )
    op.create_check_constraint(
        op.f("ck_node_public_key_fingerprint"),
        "node",
        f"public_key_fingerprint IS NULL OR public_key_fingerprint ~ {_HEX64}",
    )


def downgrade() -> None:
    for name in (
        "ck_node_public_key_fingerprint",
        "ck_node_cert_fingerprint",
        "ck_node_cert_serial",
        "ck_node_cert_all_or_none",
    ):
        op.drop_constraint(op.f(name), "node", type_="check")
    op.drop_constraint(op.f("uq_node_cert_serial"), "node", type_="unique")
    for column in (
        "public_key_fingerprint",
        "cert_not_after",
        "cert_fingerprint",
        "cert_serial",
    ):
        op.drop_column("node", column)
    op.drop_index("ix_audit_event_node_id", table_name="audit_event")
    op.drop_index("ix_audit_event_at", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_join_token_expires_at", table_name="join_token")
    op.drop_table("join_token")
