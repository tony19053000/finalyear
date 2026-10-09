"""revocation: revoked_cert, audit action node_revoked, end reason revoked (V0.7 G3, D138)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``revoked_cert`` - the fabric's revocation set (serial, node, reason
  ``revoked`` | ``superseded``, when, the certificate's own expiry);
* ``audit_event.action`` gains ``node_revoked`` (the 0015 list is frozen
  there; this revision drops and recreates the named CHECK over a written-out
  list, the 0003/0004 pattern);
* ``node_session.end_reason`` gains ``revoked`` (a stream cut by ``node
  revoke``).

Downgrade: ``revoked_cert`` is dropped (the revocation set is lost: revoked
nodes stay ``revoked`` in ``node.state``, so they are still refused at Hello,
but their serials are no longer refused by the HTTPS and peer planes - re-run
the upgrade before serving); ``node_revoked`` audit rows are deleted (the
narrowed CHECK cannot hold them); sessions that ended ``revoked`` are recorded
as ``protocol_error`` (the controller cut them).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
ACTIONS_0015: tuple[str, ...] = (
    "invite_created",
    "invite_revoked",
    "enrolled",
    "enroll_refused",
)
ACTIONS_0016: tuple[str, ...] = (*ACTIONS_0015, "node_revoked")
END_REASONS_0015: tuple[str, ...] = (
    "superseded",
    "agent_closed",
    "lease_expired",
    "controller_shutdown",
    "controller_restart",
    "protocol_error",
    "internal_error",
    "stream_lost",
)
END_REASONS_0016: tuple[str, ...] = (*END_REASONS_0015, "revoked")
REASONS_0016: tuple[str, ...] = ("revoked", "superseded")

#: Bare constraint keys; Alembic applies ``ck_%(table_name)s_%(constraint_name)s``.
_ACTION_CHECK = "action"
_END_REASON_CHECK = "end_reason"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "revoked_cert",
        sa.Column("serial", sa.String(length=40), nullable=False),
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "serial ~ '^[0-9a-f]{1,40}$'", name=op.f("ck_revoked_cert_serial")
        ),
        sa.CheckConstraint(
            f"reason IN ({_listed(REASONS_0016)})",
            name=op.f("ck_revoked_cert_reason"),
        ),
        sa.PrimaryKeyConstraint("serial", name=op.f("pk_revoked_cert")),
    )
    op.create_index(
        "ix_revoked_cert_node_id", "revoked_cert", ["node_id"], unique=False
    )
    op.create_index(
        "ix_revoked_cert_not_after", "revoked_cert", ["not_after"], unique=False
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0016)})"
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK,
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({_listed(END_REASONS_0016)})",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE node_session SET end_reason = 'protocol_error' "
            "WHERE end_reason = 'revoked'"
        )
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK,
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({_listed(END_REASONS_0015)})",
    )
    op.execute(sa.text("DELETE FROM audit_event WHERE action = 'node_revoked'"))
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0015)})"
    )
    op.drop_index("ix_revoked_cert_not_after", table_name="revoked_cert")
    op.drop_index("ix_revoked_cert_node_id", table_name="revoked_cert")
    op.drop_table("revoked_cert")
