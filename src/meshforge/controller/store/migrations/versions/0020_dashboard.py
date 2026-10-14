"""dashboard: wrong-node audit action (V0.8 G1, D168 N3 / D180)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``audit_event.action`` gains ``attempt_wrong_node`` (a node reported status
  for another node's attempt; the 0019 list is frozen there; the CHECK is
  dropped and recreated, the 0016 pattern).

Downgrade deletes the new audit rows and restores the 0019 CHECK.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
ACTIONS_0019: tuple[str, ...] = (
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
)
ACTIONS_0020: tuple[str, ...] = (*ACTIONS_0019, "attempt_wrong_node")

_ACTION_CHECK = "action"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0020)})"
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM audit_event WHERE action IN ('attempt_wrong_node')")
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0019)})"
    )
