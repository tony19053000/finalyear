"""campaign runner + owner guard (V0.9 G5 lane B, roadmap 0.9.7, D219/D246)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``bench_campaign``: state gains ``paused``; throttle columns
  ``max_concurrent_per_node`` (default 1), ``cooldown_s`` (default 5),
  ``max_infra_retries`` (default 2); ``started_at`` (set = the runner owns the
  campaign) and ``finished_reason`` (``complete`` / ``budget`` / ``cancelled``).
* ``bench_run``: ``tries`` (infrastructure retries spent, default 0),
  ``generation`` (how often the run was requeued - infra retry or owner
  pre-emption; the job's Idempotency-Key suffix, so no generation is submitted
  twice) and ``last_failure`` (short text).
* ``job.failure_type`` / ``attempt.failure_type`` CHECKs widened by
  ``owner_preempted`` (a benchmark attempt stopped by the owner guard).

Downgrade: paused campaigns become ``active``, ``owner_preempted`` becomes
``cancelled``, the columns are dropped (development only).

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
CAMPAIGN_STATES_0022: tuple[str, ...] = ("active", "done", "cancelled")
CAMPAIGN_STATES_0024: tuple[str, ...] = ("active", "paused", "done", "cancelled")
FINISHED_REASONS_0024: tuple[str, ...] = ("complete", "budget", "cancelled")
LAST_FAILURE_CHARS_0024 = 128
_BASE = (
    "'user_error', 'oom', 'vram_oom', 'env_error', 'disk_full', 'node_lost', "
    "'gpu_session_lost', 'transfer_error', 'integrity_error', 'fenced', 'cancelled', "
    "'timeout', 'deadline_exceeded', 'resource_busy', 'no_feasible_plan', "
    "'network_error', 'config_error', 'invalid_input', 'unauthorized', 'not_found', "
    "'conflict', 'internal_error', 'output_error'"
)
FAILURE_TYPES_0008 = _BASE
FAILURE_TYPES_0024 = _BASE + ", 'owner_preempted'"


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _failure_checks(values: str) -> None:
    for table in ("job", "attempt"):
        op.drop_constraint(op.f(f"ck_{table}_failure_type"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_failure_type"),
            table,
            f"failure_type IS NULL OR failure_type IN ({values})",
        )


def _campaign_state(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_bench_campaign_state"), "bench_campaign", type_="check")
    op.create_check_constraint(
        op.f("ck_bench_campaign_state"),
        "bench_campaign",
        f"state IN ({_listed(values)})",
    )


def upgrade() -> None:
    _failure_checks(FAILURE_TYPES_0024)
    _campaign_state(CAMPAIGN_STATES_0024)
    tz = sa.DateTime(timezone=True)
    op.add_column(
        "bench_campaign",
        sa.Column(
            "max_concurrent_per_node",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.add_column(
        "bench_campaign",
        sa.Column(
            "cooldown_s", sa.Float(), server_default=sa.text("5"), nullable=False
        ),
    )
    op.add_column(
        "bench_campaign",
        sa.Column(
            "max_infra_retries",
            sa.Integer(),
            server_default=sa.text("2"),
            nullable=False,
        ),
    )
    op.add_column("bench_campaign", sa.Column("started_at", tz, nullable=True))
    op.add_column(
        "bench_campaign",
        sa.Column("finished_reason", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_bench_campaign_max_concurrent_per_node"),
        "bench_campaign",
        "max_concurrent_per_node BETWEEN 1 AND 64",
    )
    op.create_check_constraint(
        op.f("ck_bench_campaign_cooldown_s"),
        "bench_campaign",
        "cooldown_s >= 0 AND cooldown_s <= 86400",
    )
    op.create_check_constraint(
        op.f("ck_bench_campaign_max_infra_retries"),
        "bench_campaign",
        "max_infra_retries BETWEEN 0 AND 10",
    )
    op.create_check_constraint(
        op.f("ck_bench_campaign_finished_reason"),
        "bench_campaign",
        f"finished_reason IS NULL OR finished_reason IN "
        f"({_listed(FINISHED_REASONS_0024)})",
    )
    op.add_column(
        "bench_run",
        sa.Column("tries", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "bench_run",
        sa.Column(
            "generation", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "bench_run",
        sa.Column(
            "last_failure", sa.String(length=LAST_FAILURE_CHARS_0024), nullable=True
        ),
    )
    op.create_check_constraint(op.f("ck_bench_run_tries"), "bench_run", "tries >= 0")
    op.create_check_constraint(
        op.f("ck_bench_run_generation"), "bench_run", "generation >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_bench_run_generation"), "bench_run", type_="check")
    op.drop_constraint(op.f("ck_bench_run_tries"), "bench_run", type_="check")
    op.drop_column("bench_run", "last_failure")
    op.drop_column("bench_run", "generation")
    op.drop_column("bench_run", "tries")
    for name in (
        "finished_reason",
        "max_infra_retries",
        "cooldown_s",
        "max_concurrent_per_node",
    ):
        op.drop_constraint(
            op.f(f"ck_bench_campaign_{name}"), "bench_campaign", type_="check"
        )
    op.drop_column("bench_campaign", "finished_reason")
    op.drop_column("bench_campaign", "started_at")
    op.drop_column("bench_campaign", "max_infra_retries")
    op.drop_column("bench_campaign", "cooldown_s")
    op.drop_column("bench_campaign", "max_concurrent_per_node")
    op.execute("UPDATE bench_campaign SET state = 'active' WHERE state = 'paused'")
    _campaign_state(CAMPAIGN_STATES_0022)
    op.execute(
        "UPDATE job SET failure_type = 'cancelled' "
        "WHERE failure_type = 'owner_preempted'"
    )
    op.execute(
        "UPDATE attempt SET failure_type = 'cancelled' "
        "WHERE failure_type = 'owner_preempted'"
    )
    _failure_checks(FAILURE_TYPES_0008)
