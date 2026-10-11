"""rotation + users: pending node certificates, app_user, api_token, job owner (V0.7 G4, D140/D141)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``node``: ``pending_cert_serial`` (unique), ``pending_cert_fingerprint``,
  ``pending_cert_not_after``, ``pending_public_key_fingerprint`` - a renewed
  certificate the agent has not taken into use yet (all four together, only
  next to a current certificate) - and ``cert_renewed_at`` (renewal rate limit);
* ``app_user`` (login name unique, role ``admin`` | ``user``, scrypt hash,
  ``disabled``) and ``api_token`` (SHA-256 of the token only, unique; expiry
  required; ``revoked_at``, ``last_used_at``), cascade-deleted with the user;
* ``job.owner_user_id`` -> ``app_user.id`` (``SET NULL``; NULL for every job
  from before this migration - admins see them, users do not);
* ``audit_event.action`` gains the renewal, user, login and token actions (the
  0016 list is frozen there; the CHECK is dropped and recreated over a
  written-out list, the 0016 pattern).

Downgrade drops all of it: users and tokens are lost (the CLI logs in again
after a re-upgrade), jobs forget their owner, the new audit rows are deleted
(the narrowed CHECK cannot hold them) and every unused pending certificate is
forgotten - it stays CA-valid until it expires, so the downgrade first records
it in ``revoked_cert`` as ``superseded`` (the agent still holds the current
one, which keeps working).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
ACTIONS_0016: tuple[str, ...] = (
    "invite_created",
    "invite_revoked",
    "enrolled",
    "enroll_refused",
    "node_revoked",
)
ACTIONS_0018: tuple[str, ...] = (
    *ACTIONS_0016,
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
ROLES_0018: tuple[str, ...] = ("admin", "user")
USER_NAME_PATTERN = "^[a-z0-9][a-z0-9._-]{0,63}$"
_HEX64 = "'^[0-9a-f]{64}$'"

#: Bare constraint key; Alembic applies ``ck_%(table_name)s_%(constraint_name)s``.
_ACTION_CHECK = "action"

_NODE_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "pending_cert_all_or_none",
        "(pending_cert_serial IS NULL) = (pending_cert_fingerprint IS NULL) AND "
        "(pending_cert_serial IS NULL) = (pending_cert_not_after IS NULL) AND "
        "(pending_cert_serial IS NULL) = (pending_public_key_fingerprint IS NULL)",
    ),
    (
        "pending_cert_serial",
        "pending_cert_serial IS NULL OR pending_cert_serial ~ '^[0-9a-f]{1,40}$'",
    ),
    (
        "pending_cert_fingerprint",
        f"pending_cert_fingerprint IS NULL OR pending_cert_fingerprint ~ {_HEX64}",
    ),
    (
        "pending_public_key_fingerprint",
        "pending_public_key_fingerprint IS NULL OR "
        f"pending_public_key_fingerprint ~ {_HEX64}",
    ),
    (
        "pending_needs_current",
        "pending_cert_serial IS NULL OR cert_serial IS NOT NULL",
    ),
)


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # --- node: the pending (renewed, not yet used) certificate ---------------
    op.add_column(
        "node", sa.Column("pending_cert_serial", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "node",
        sa.Column("pending_cert_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "node",
        sa.Column("pending_cert_not_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "node",
        sa.Column(
            "pending_public_key_fingerprint", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "node",
        sa.Column("cert_renewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_node_pending_cert_serial"), "node", ["pending_cert_serial"]
    )
    for name, condition in _NODE_CHECKS:
        op.create_check_constraint(op.f(f"ck_node_{name}"), "node", condition)

    # --- users and API tokens --------------------------------------------------
    op.create_table(
        "app_user",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("pw_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"name ~ '{USER_NAME_PATTERN}'", name=op.f("ck_app_user_name")
        ),
        sa.CheckConstraint(
            f"role IN ({_listed(ROLES_0018)})", name=op.f("ck_app_user_role")
        ),
        sa.CheckConstraint(
            "length(pw_hash) BETWEEN 16 AND 255", name=op.f("ck_app_user_pw_hash")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("name", name=op.f("uq_app_user_name")),
    )
    op.create_table(
        "api_token",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"token_hash ~ {_HEX64}", name=op.f("ck_api_token_token_hash")
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_api_token_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 64", name=op.f("ck_api_token_name")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_api_token_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_token_token_hash")),
    )
    op.create_index("ix_api_token_user_id", "api_token", ["user_id"], unique=False)
    op.create_index(
        "ix_api_token_expires_at", "api_token", ["expires_at"], unique=False
    )

    # --- job owner -------------------------------------------------------------
    op.add_column(
        "job", sa.Column("owner_user_id", sa.String(length=40), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_job_owner_user_id_app_user"),
        "job",
        "app_user",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_job_owner_user_id", "job", ["owner_user_id"], unique=False)

    # --- audit actions -----------------------------------------------------------
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0018)})"
    )


def downgrade() -> None:
    new_actions = _listed(ACTIONS_0018[len(ACTIONS_0016) :])
    op.execute(
        sa.text(f"DELETE FROM audit_event WHERE action IN ({new_actions})")  # noqa: S608 - frozen literals
    )
    op.drop_constraint(_ACTION_CHECK, "audit_event", type_="check")
    op.create_check_constraint(
        _ACTION_CHECK, "audit_event", f"action IN ({_listed(ACTIONS_0016)})"
    )
    op.drop_index("ix_job_owner_user_id", table_name="job")
    op.drop_constraint(op.f("fk_job_owner_user_id_app_user"), "job", type_="foreignkey")
    op.drop_column("job", "owner_user_id")
    op.drop_index("ix_api_token_expires_at", table_name="api_token")
    op.drop_index("ix_api_token_user_id", table_name="api_token")
    op.drop_table("api_token")
    op.drop_table("app_user")
    # An unused pending certificate is CA-valid until it expires: keep refusing it.
    op.execute(
        sa.text(
            "INSERT INTO revoked_cert (serial, node_id, reason, not_after) "
            "SELECT pending_cert_serial, node_id, 'superseded', pending_cert_not_after "
            "FROM node WHERE pending_cert_serial IS NOT NULL "
            "ON CONFLICT (serial) DO NOTHING"
        )
    )
    for name, _condition in reversed(_NODE_CHECKS):
        op.drop_constraint(op.f(f"ck_node_{name}"), "node", type_="check")
    op.drop_constraint(op.f("uq_node_pending_cert_serial"), "node", type_="unique")
    for column in (
        "cert_renewed_at",
        "pending_public_key_fingerprint",
        "pending_cert_not_after",
        "pending_cert_fingerprint",
        "pending_cert_serial",
    ):
        op.drop_column("node", column)
