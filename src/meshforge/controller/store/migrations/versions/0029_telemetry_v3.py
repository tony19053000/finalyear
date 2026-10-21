"""telemetry schema v3: Mode 3 run-record columns + chunk_record (V0.10 G8, D271)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``run_record`` gains the v3 columns (``controller/telemetry/schema.py``):
  ``attempt_role``, ``planner_strategy``, ``planner_node_set`` and the
  ``split_*`` columns of a split job; ``strategy`` accepts ``mode3``. All
  nullable: rows written before keep their ``schema_version`` (1 or 2) and
  NULL there - a record is never rewritten.
* ``chunk_record`` (new): one row per chunk lease that ended with a result,
  plus the other leases of a committed chunk - written by the chunk result's
  own transaction (checklist 35). Primary key ``(job_id, chunk_index,
  lease_seq)``; no foreign keys (a record outlives the queue rows).
  ``stderr_tail`` is the try's redacted stderr tail for the API (D261), never
  exported.
* ``bench_run.node_set`` + ``strategy`` ``mode3``: a campaign run forced onto
  a node set (D271); ``(strategy = 'mode3') = (node_set IS NOT NULL)``.
* ``plan_decision.shadow_node_set``: a forced split benchmark job's shadow
  pick when it is a Mode 3 set (D215 extended).

Downgrade drops the new table and columns and narrows ``strategy`` again
(``mode3`` rows become ``mode1``; development only).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
STRATEGIES_0021 = "'local', 'mode1'"
STRATEGIES_0029 = "'local', 'mode1', 'mode3'"
ROLES_0029 = "'run', 'lane', 'combine'"
CHUNK_OUTCOMES_0029 = (
    "'committed', 'lost', 'failed', 'superseded', 'released', 'expired'"
)
FAILURE_CLASSES_0029 = (
    "'ok', 'workload', 'infrastructure', 'condition_failed', 'unavailable'"
)
STDERR_TAIL_CHARS_0029 = 65_536
_SHORT = 64
_ID = 40
_DIGEST = 71
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def _strategy_check(values: str) -> None:
    op.drop_constraint(op.f("ck_run_record_strategy"), "run_record", type_="check")
    op.create_check_constraint(
        op.f("ck_run_record_strategy"), "run_record", f"strategy IN ({values})"
    )


def upgrade() -> None:
    _strategy_check(STRATEGIES_0029)
    columns: list[sa.Column[object]] = [
        sa.Column("attempt_role", sa.String(length=_SHORT), nullable=True),
        sa.Column("planner_strategy", sa.String(length=_SHORT), nullable=True),
        sa.Column("planner_node_set", _JSONB, nullable=True),
        sa.Column("split_node_set", _JSONB, nullable=True),
        sa.Column("split_items", sa.BigInteger(), nullable=True),
        sa.Column("split_chunk_items", sa.BigInteger(), nullable=True),
        sa.Column("split_chunks", sa.BigInteger(), nullable=True),
        sa.Column("split_predicted_s", sa.Float(), nullable=True),
        sa.Column("split_makespan_ms", sa.BigInteger(), nullable=True),
        sa.Column("split_job_ms", sa.BigInteger(), nullable=True),
        sa.Column("split_combine_ms", sa.BigInteger(), nullable=True),
        sa.Column("split_ideal_bound_s", sa.Float(), nullable=True),
    ]
    for column in columns:
        op.add_column("run_record", column)
    op.create_check_constraint(
        op.f("ck_run_record_attempt_role"),
        "run_record",
        f"attempt_role IS NULL OR attempt_role IN ({ROLES_0029})",
    )
    op.create_check_constraint(
        op.f("ck_run_record_planner_strategy"),
        "run_record",
        f"planner_strategy IS NULL OR planner_strategy IN ({STRATEGIES_0029})",
    )
    op.add_column("plan_decision", sa.Column("shadow_node_set", _JSONB, nullable=True))
    op.create_table(
        "chunk_record",
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.String(length=_ID), nullable=False),
        sa.Column("chunk_index", sa.BigInteger(), nullable=False),
        sa.Column("lease_seq", sa.BigInteger(), nullable=False),
        sa.Column("lane_attempt_id", sa.String(length=_ID), nullable=False),
        sa.Column("node_id", sa.String(length=_ID), nullable=False),
        sa.Column("first_item", sa.BigInteger(), nullable=False),
        sa.Column("items", sa.BigInteger(), nullable=False),
        sa.Column("speculative", sa.Boolean(), nullable=False),
        sa.Column("won", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(length=_SHORT), nullable=False),
        sa.Column("failure_type", sa.String(length=_SHORT), nullable=True),
        sa.Column("failure_class", sa.String(length=_SHORT), nullable=False),
        sa.Column("exit_code", sa.BigInteger(), nullable=True),
        sa.Column("result_digest", sa.String(length=_DIGEST), nullable=True),
        sa.Column("bytes_in", sa.BigInteger(), nullable=True),
        sa.Column("bytes_out", sa.BigInteger(), nullable=True),
        sa.Column("stage_ms", sa.BigInteger(), nullable=True),
        sa.Column("env_ms", sa.BigInteger(), nullable=True),
        sa.Column("run_ms", sa.BigInteger(), nullable=True),
        sa.Column("upload_ms", sa.BigInteger(), nullable=True),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "schema_version >= 3", name=op.f("ck_chunk_record_schema_version")
        ),
        sa.CheckConstraint(
            f"outcome IN ({CHUNK_OUTCOMES_0029})", name=op.f("ck_chunk_record_outcome")
        ),
        sa.CheckConstraint(
            f"failure_class IN ({FAILURE_CLASSES_0029})",
            name=op.f("ck_chunk_record_failure_class"),
        ),
        sa.CheckConstraint(
            "chunk_index >= 0 AND lease_seq >= 1", name=op.f("ck_chunk_record_lease")
        ),
        sa.CheckConstraint(
            "items >= 1 AND first_item >= 0", name=op.f("ck_chunk_record_items")
        ),
        sa.CheckConstraint(
            f"stderr_tail IS NULL OR length(stderr_tail) <= {STDERR_TAIL_CHARS_0029}",
            name=op.f("ck_chunk_record_stderr_tail"),
        ),
        sa.PrimaryKeyConstraint(
            "job_id", "chunk_index", "lease_seq", name=op.f("pk_chunk_record")
        ),
    )
    op.create_index(
        op.f("ix_chunk_record_recorded_at"),
        "chunk_record",
        ["recorded_at"],
        unique=False,
    )
    # bench_run: a mode3 run's forced node set
    op.add_column("bench_run", sa.Column("node_set", _JSONB, nullable=True))
    _bench_strategy_check(STRATEGIES_0029)
    op.create_check_constraint(
        op.f("ck_bench_run_node_set"),
        "bench_run",
        "(strategy = 'mode3') = (node_set IS NOT NULL)",
    )


def _bench_strategy_check(values: str) -> None:
    op.drop_constraint(op.f("ck_bench_run_strategy"), "bench_run", type_="check")
    op.create_check_constraint(
        op.f("ck_bench_run_strategy"), "bench_run", f"strategy IN ({values})"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_bench_run_node_set"), "bench_run", type_="check")
    op.execute("DELETE FROM bench_run WHERE strategy = 'mode3'")
    _bench_strategy_check(STRATEGIES_0021)
    op.drop_column("bench_run", "node_set")
    op.drop_index(op.f("ix_chunk_record_recorded_at"), table_name="chunk_record")
    op.drop_table("chunk_record")
    op.drop_column("plan_decision", "shadow_node_set")
    op.drop_constraint(
        op.f("ck_run_record_planner_strategy"), "run_record", type_="check"
    )
    op.drop_constraint(op.f("ck_run_record_attempt_role"), "run_record", type_="check")
    for name in (
        "split_ideal_bound_s",
        "split_combine_ms",
        "split_job_ms",
        "split_makespan_ms",
        "split_predicted_s",
        "split_chunks",
        "split_chunk_items",
        "split_items",
        "split_node_set",
        "planner_node_set",
        "planner_strategy",
        "attempt_role",
    ):
        op.drop_column("run_record", name)
    op.execute("UPDATE run_record SET strategy = 'mode1' WHERE strategy = 'mode3'")
    _strategy_check(STRATEGIES_0021)
