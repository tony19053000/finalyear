"""node_session.end_reason gains stream_lost (V0.3 G6, D19)

Before this revision a transport drop, a cancelled call and a clean agent half-close
were all recorded as ``agent_closed`` - a telemetry lie, because only the half-close
is the agent saying it is leaving. ``stream_lost`` is the abrupt end (ARCHITECTURE
§12.3, 2026-09-24 amendment).

Same pattern as 0003: the named CHECK (``ck_node_session_end_reason`` through the
metadata naming convention) is dropped and recreated over the widened value list.
The lists are written out here rather than read from ``SessionEndReason`` so this
revision keeps meaning the same thing when a later one adds a value;
``tests/unit/controller/test_end_reason_migration.py`` pins that the upgrade list
equals the model's enum today. The downgrade rewrites ``stream_lost`` rows back to
``agent_closed`` (what they would have been recorded as before) before narrowing,
so it cannot fail on real data.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Bare constraint key; Alembic applies ``ck_%(table_name)s_%(constraint_name)s``.
_END_REASON_CHECK = "end_reason"

#: The enum as of 0003 (for the downgrade).
END_REASONS_0003: tuple[str, ...] = (
    "superseded",
    "agent_closed",
    "lease_expired",
    "controller_shutdown",
    "controller_restart",
    "protocol_error",
    "internal_error",
)
#: The enum as of this revision.
END_REASONS_0004: tuple[str, ...] = (*END_REASONS_0003, "stream_lost")


def _check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"end_reason IS NULL OR end_reason IN ({listed})"


def upgrade() -> None:
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK, "node_session", _check(END_REASONS_0004)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE node_session SET end_reason = 'agent_closed' "
            "WHERE end_reason = 'stream_lost'"
        )
    )
    op.drop_constraint(_END_REASON_CHECK, "node_session", type_="check")
    op.create_check_constraint(
        _END_REASON_CHECK, "node_session", _check(END_REASONS_0003)
    )
