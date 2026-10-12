"""secret: job secrets encrypted at rest (V0.7 G6, roadmap 0.7.10, D144)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``secret`` (owner_user_id -> app_user CASCADE, name, ciphertext, nonce,
  key_id, created_at, updated_at; primary key (owner_user_id, name), so one
  name per owner). The value is never stored: only its AES-256-GCM ciphertext
  bound to owner and name (``controller/secret_box.py``);
* ``audit_event.action`` gains ``secret_set`` and ``secret_deleted`` (the 0018
  list is frozen there; the CHECK is dropped and recreated, the 0016 pattern).

Downgrade drops the table (every stored secret is lost; users set them again)
and the new audit rows.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
ACTIONS_0018: tuple[str, ...] = (
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
)
ACTIONS_0019: tuple[str, ...] = (*ACTIONS_0018, "secret_set", "secret_deleted")
NAME_PATTERN_0019 = "^[A-Z_][A-Z0-9_]{0,63}$"
MAX_CIPHERTEXT_0019 = 64 * 1024 + 16

_ACTION_CHECK = "action"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "secret",
        sa.Column("owner_user_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=16), nullable=False),
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
            f"name ~ '{NAME_PATTERN_0019}'", name=op.f("ck_secret_name")
        ),
        sa.CheckConstraint("octet_length(nonce) = 12", name=op.f("ck_secret_nonce")),
        sa.CheckConstraint(
            f"octet_length(ciphertext) BETWEEN 17 AND {MAX_CIPHERTEXT_0019}",
            name=op.f("ck_secret_ciphertext"),
        ),
        sa.CheckConstraint("key_id ~ '^[0-9a-f]{16}$'", name=op.f("ck_secret_key_id")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["app_user.id"],
            name=op.f("fk_secret_owner_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("owner_user_id", "name", name=op.f("pk_secret")),
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0019)})"
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM audit_event WHERE action IN ('secret_set', 'secret_deleted')"
        )
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0018)})"
    )
    op.drop_table("secret")
