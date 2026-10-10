"""cert session ends: end reasons cert_superseded + cert_expired (V0.7 G3 fix r1, D155)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``node_session.end_reason`` gains ``cert_superseded`` (a live stream whose
  certificate a re-enrolment superseded, cut by the revocation sync - T-1) and
  ``cert_expired`` (a live stream cut at its certificate's ``not_after`` - T-3).

Downgrade: sessions that ended with either are recorded as ``revoked`` (the
controller cut them over their certificate) and the CHECK goes back to the
0016 list.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
END_REASONS_0016: tuple[str, ...] = (
    "superseded",
    "agent_closed",
    "lease_expired",
    "controller_shutdown",
    "controller_restart",
    "protocol_error",
    "internal_error",
    "stream_lost",
    "revoked",
)
END_REASONS_0017: tuple[str, ...] = (
    *END_REASONS_0016,
    "cert_superseded",
    "cert_expired",
)

#: Bare constraint key; Alembic applies ``ck_%(table_name)s_%(constraint_name)s``.
_END_REASON_CHECK = "end_reason"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK,
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({_listed(END_REASONS_0017)})",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE node_session SET end_reason = 'revoked' "
            "WHERE end_reason IN ('cert_superseded', 'cert_expired')"
        )
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK,
        "node_session",
        f"end_reason IS NULL OR end_reason IN ({_listed(END_REASONS_0016)})",
    )
