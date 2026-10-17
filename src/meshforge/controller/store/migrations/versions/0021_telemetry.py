"""telemetry schema v1: node clock offset, clock alerts (V0.9 G1, D212/D213)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``node_session`` gains the node's clock offset over the session
  (``clock_offset_ms``, ``clock_offset_bound_ms``, ``clock_consistent``,
  ``clock_exchanges``, ``clock_measured_at``; all NULL until the first Pong);
* ``audit_event.action`` gains ``clock_uncertain`` (an operator alert; the 0020
  list is frozen there; the CHECK is dropped and recreated, the 0016 pattern);
* ``attempt.workload`` (JSONB): a benchmark's own report from its terminal
  status;
* ``run_record``: telemetry schema v1 (``controller/telemetry/schema.py``), one
  row per terminal attempt, no foreign keys (the dataset outlives job rows).

Downgrade drops the table and the columns, deletes the new audit rows and
restores the 0020 CHECK.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

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
    op.add_column(
        "attempt",
        sa.Column("workload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _create_run_record()


def _create_run_record() -> None:
    text, big, flt = sa.String, sa.BigInteger, sa.Float

    def col(name: str, kind: Any, *, null: bool = True) -> sa.Column[Any]:
        return sa.Column(name, kind, nullable=null)

    jsonb = postgresql.JSONB(astext_type=sa.Text())
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "run_record",
        col("schema_version", big(), null=False),
        col("attempt_id", text(40), null=False),
        col("job_id", text(40), null=False),
        col("attempt_no", big(), null=False),
        col("campaign_id", text(64)),
        col("group_id", text(64)),
        col("config_id", text(64)),
        col("repetition", big()),
        col("fingerprint", text(64)),
        col("node_id", text(40), null=False),
        col("submitter_node_id", text(40)),
        col("strategy", text(64), null=False),
        col("placement_mode", text(64), null=False),
        col("policy", text(64)),
        col("planner_node_id", text(40)),
        col("planner_estimate_s", flt()),
        col("workload_family", text(64)),
        col("workload_params", jsonb),
        col("workload_seed", big()),
        col("workload_status", text(64)),
        col("workload_device", text(64)),
        *(col(f"p_{n}", big()) for n in _INT_PARAMS),
        col("p_dtype", text(64)),
        col("env_base", text(64)),
        col("gpu_request", text(64)),
        col("runtime", text(64)),
        col("node_state", text(64)),
        col("node_rtt_ms", flt()),
        col("node_cpu_percent", flt()),
        col("node_ram_free_bytes", big()),
        col("node_ram_total_bytes", big()),
        col("node_gpu_util_percent", flt()),
        col("node_gpu_vram_free_bytes", big()),
        col("node_sample_age_s", flt()),
        col("node_live_attempts", big()),
        col("node_cpu_score", flt()),
        col("node_gpu_score", flt()),
        col("cache_inputs", big()),
        col("cache_warm_inputs", big()),
        col("cache_state", text(64)),
        col("cache_detail", jsonb),
        col("net_source", text(64)),
        col("net_rtt_ms", flt()),
        col("net_bw_bytes_s", flt()),
        col("net_samples", big()),
        *(col(f"{p}_ms", big()) for p in ("queue", "stage", "env", "run")),
        col("compute_ns", big()),
        col("collect_ms", big()),
        col("total_ms", big()),
        col("bytes_in", big()),
        col("bytes_in_cached", big()),
        col("bytes_out", big()),
        col("attempt_state", text(64), null=False),
        col("exit_code", big()),
        col("failure_type", text(64)),
        col("failure_class", text(64), null=False),
        col("clock_offset_ms", flt()),
        col("clock_offset_bound_ms", flt()),
        col("clock_consistent", sa.Boolean()),
        col("attempt_created_at", tz, null=False),
        col("attempt_ended_at", tz),
        sa.Column("recorded_at", tz, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "schema_version >= 1", name=op.f("ck_run_record_schema_version")
        ),
        sa.CheckConstraint(
            f"failure_class IN ({_listed(FAILURE_CLASSES_0021)})",
            name=op.f("ck_run_record_failure_class"),
        ),
        sa.CheckConstraint(
            "strategy IN ('local', 'mode1')", name=op.f("ck_run_record_strategy")
        ),
        sa.CheckConstraint(
            "placement_mode IN ('pinned', 'auto')",
            name=op.f("ck_run_record_placement_mode"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_run_record")),
    )
    op.create_index(
        "ix_run_record_campaign",
        "run_record",
        ["campaign_id", "config_id", "repetition"],
    )
    op.create_index("ix_run_record_recorded_at", "run_record", ["recorded_at"])


_INT_PARAMS: tuple[str, ...] = (
    "n",
    "repeats",
    "size_mb",
    "size",
    "batch",
    "width",
    "depth",
    "seq_len",
)
FAILURE_CLASSES_0021: tuple[str, ...] = (
    "ok",
    "workload",
    "infrastructure",
    "condition_failed",
    "unavailable",
)


def downgrade() -> None:
    op.drop_index("ix_run_record_recorded_at", table_name="run_record")
    op.drop_index("ix_run_record_campaign", table_name="run_record")
    op.drop_table("run_record")
    op.drop_column("attempt", "workload")
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
