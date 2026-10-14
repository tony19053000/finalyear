"""dashboard: wrong-node audit action, browser sessions (V0.8 G1, D168 N3 /
D180, D170)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``audit_event.action`` gains ``attempt_wrong_node`` (a node reported status
  for another node's attempt; the 0019 list is frozen there; the CHECK is
  dropped and recreated, the 0016 pattern);
* ``api_token.kind`` (``api`` | ``browser_session``, default ``api`` - every
  existing row is an API token): the dashboard's cookie sessions live in the
  same table, so they are listed and revoked with the API tokens.

Downgrade deletes the browser sessions and the new audit rows, drops the
column and restores the 0019 CHECK.

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
TOKEN_KINDS_0020: tuple[str, ...] = ("api", "browser_session")

_ACTION_CHECK = "action"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.add_column(
        "api_token",
        sa.Column(
            "kind",
            sa.String(length=16),
            server_default=sa.text("'api'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "kind", "api_token", f"kind IN ({_listed(TOKEN_KINDS_0020)})"
    )
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
    op.execute(sa.text("DELETE FROM api_token WHERE kind <> 'api'"))
    op.drop_constraint("kind", "api_token", type_="check")
    op.drop_column("api_token", "kind")
