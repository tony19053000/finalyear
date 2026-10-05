"""planner: job queue/policy/retry columns, node slots, attempt slot, plan_decision
(V0.6 G2, D103-D107, D110, D112)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``job``: ``priority`` (-10..10, D106), ``policy`` (D107, NULL = the
  controller's default), ``max_attempts`` / ``retry_backoff_s`` /
  ``on_lost_retry`` (D110/D117, used from V0.6 G3), ``submitter_node_id`` +
  ``submitter_seeded`` (D103/D109: the Local candidate), ``not_before``
  (retry backoff), ``queued_at`` (queue order and aging; existing queued jobs
  get their ``created_at``), ``input_bytes`` and ``fingerprint`` (estimator
  inputs, D108). Partial index on ``queued_at`` for queued jobs.
* ``node``: ``cpu_slots`` / ``gpu_slots`` - the slot counts the latest Hello
  advertised (D105); NULL = an older agent that did not say.
* ``attempt.slot`` (``cpu``/``gpu``) - which slot a live attempt holds.
* ``plan_decision`` (D112).

Downgrade drops all of it (decision records are lost; jobs keep running).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from meshforge.common.manifest import POLICIES
from meshforge.controller.store.base import UINT64_MAX

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICIES = ", ".join(f"'{v}'" for v in POLICIES)


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column(
            "priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column("job", sa.Column("policy", sa.String(length=32), nullable=True))
    op.add_column(
        "job",
        sa.Column(
            "max_attempts",
            sa.SmallInteger(),
            server_default=sa.text("3"),
            nullable=False,
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "retry_backoff_s",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "on_lost_retry",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "job", sa.Column("submitter_node_id", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "job",
        sa.Column(
            "submitter_seeded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "job", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job",
        sa.Column("input_bytes", sa.Numeric(precision=20, scale=0), nullable=True),
    )
    op.add_column("job", sa.Column("fingerprint", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        op.f("ck_job_priority"), "job", "priority >= -10 AND priority <= 10"
    )
    op.create_check_constraint(
        op.f("ck_job_policy"), "job", f"policy IS NULL OR policy IN ({_POLICIES})"
    )
    op.create_check_constraint(
        op.f("ck_job_max_attempts"), "job", "max_attempts >= 1 AND max_attempts <= 10"
    )
    op.create_check_constraint(
        op.f("ck_job_retry_backoff_s"),
        "job",
        "retry_backoff_s >= 0 AND retry_backoff_s <= 300",
    )
    op.create_check_constraint(
        op.f("ck_job_input_bytes"),
        "job",
        f"input_bytes >= 0 AND input_bytes <= {UINT64_MAX}",
    )
    op.execute("UPDATE job SET queued_at = created_at WHERE state = 'queued'")
    op.create_index(
        "ix_job_queued_at",
        "job",
        ["queued_at"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index("ix_job_fingerprint", "job", ["fingerprint"], unique=False)

    op.add_column("node", sa.Column("cpu_slots", sa.Integer(), nullable=True))
    op.add_column("node", sa.Column("gpu_slots", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_node_cpu_slots"),
        "node",
        "cpu_slots IS NULL OR (cpu_slots >= 0 AND cpu_slots <= 64)",
    )
    op.create_check_constraint(
        op.f("ck_node_gpu_slots"),
        "node",
        "gpu_slots IS NULL OR (gpu_slots >= 0 AND gpu_slots <= 64)",
    )

    op.add_column("attempt", sa.Column("slot", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        op.f("ck_attempt_slot"), "attempt", "slot IS NULL OR slot IN ('cpu', 'gpu')"
    )

    op.create_table(
        "plan_decision",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("chosen_node_id", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("reason_key", sa.String(length=64), nullable=False),
        sa.Column(
            "candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('dispatched', 'waiting', 'failed')",
            name=op.f("ck_plan_decision_outcome"),
        ),
        sa.CheckConstraint(
            f"policy IN ({_POLICIES})", name=op.f("ck_plan_decision_policy")
        ),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_plan_decision_attempt_no")),
        sa.CheckConstraint(
            "jsonb_typeof(candidates) = 'array' AND "
            "jsonb_array_length(candidates) <= 64",
            name=op.f("ck_plan_decision_candidates"),
        ),
        sa.CheckConstraint(
            "(outcome = 'dispatched') = (attempt_id IS NOT NULL)",
            name=op.f("ck_plan_decision_dispatched_has_attempt"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_plan_decision_attempt_id_attempt"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_plan_decision_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_decision")),
    )
    op.create_index(
        "ix_plan_decision_job_id", "plan_decision", ["job_id", "id"], unique=False
    )
    op.create_index(
        "ix_plan_decision_attempt_id", "plan_decision", ["attempt_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_plan_decision_attempt_id", table_name="plan_decision")
    op.drop_index("ix_plan_decision_job_id", table_name="plan_decision")
    op.drop_table("plan_decision")
    op.drop_constraint(op.f("ck_attempt_slot"), "attempt", type_="check")
    op.drop_column("attempt", "slot")
    op.drop_constraint(op.f("ck_node_gpu_slots"), "node", type_="check")
    op.drop_constraint(op.f("ck_node_cpu_slots"), "node", type_="check")
    op.drop_column("node", "gpu_slots")
    op.drop_column("node", "cpu_slots")
    op.drop_index("ix_job_fingerprint", table_name="job")
    op.drop_index(
        "ix_job_queued_at",
        table_name="job",
        postgresql_where=sa.text("state = 'queued'"),
    )
    for name in (
        "input_bytes",
        "retry_backoff_s",
        "max_attempts",
        "policy",
        "priority",
    ):
        op.drop_constraint(op.f(f"ck_job_{name}"), "job", type_="check")
    for column in (
        "fingerprint",
        "input_bytes",
        "queued_at",
        "not_before",
        "submitter_seeded",
        "submitter_node_id",
        "on_lost_retry",
        "retry_backoff_s",
        "max_attempts",
        "policy",
        "priority",
    ):
        op.drop_column("job", column)
