"""telemetry schema v1: node clock offset, clock alerts (V0.9 G1, D212/D213)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``node_session`` gains the node's clock offset over the session
  (``clock_offset_ms``, ``clock_offset_bound_ms``, ``clock_consistent``,
  ``clock_exchanges``, ``clock_measured_at``; all NULL until the first Pong);
* ``audit_event.action`` gains ``clock_uncertain`` (an operator alert; the 0020
  list is frozen there; the CHECK is dropped and recreated, the 0016 pattern).

Downgrade deletes the new audit rows, restores the 0020 CHECK and drops the
columns.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
ACTIONS_0020: tuple[str, ...] = (
    "invite_created",
    "invite_revoked",
    "enrolled",
    "enroll_refused",
    "node_revoked",
    "cert_renewed",
    "cert_renew_refused",
    "cert_activated",
    "user_created",
    "user_disabled",
    "user_enabled",
    "password_reset",
    "login",
    "login_failed",
    "token_revoked",
    "legacy_token_used",
    "secret_set",
    "secret_deleted",
    "attempt_wrong_node",
)
ACTIONS_0021: tuple[str, ...] = (*ACTIONS_0020, "clock_uncertain")

_ACTION_CHECK = "action"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.add_column(
        "node_session", sa.Column("clock_offset_ms", sa.Float(), nullable=True)
    )
    op.add_column(
        "node_session", sa.Column("clock_offset_bound_ms", sa.Float(), nullable=True)
    )
    op.add_column(
        "node_session", sa.Column("clock_consistent", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "node_session", sa.Column("clock_exchanges", sa.Integer(), nullable=True)
    )
    op.add_column(
        "node_session",
        sa.Column("clock_measured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0021)})"
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM audit_event WHERE action IN ('clock_uncertain')"))
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0020)})"
    )
    for column in (
        "clock_measured_at",
        "clock_exchanges",
        "clock_consistent",
        "clock_offset_bound_ms",
        "clock_offset_ms",
    ):
        op.drop_column("node_session", column)
