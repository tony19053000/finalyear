"""benchmark campaigns + shadow plan (V0.9 G3, roadmap 0.9.3/0.9.4, D215/D216/D232)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``bench_campaign``: a validated campaign spec (YAML verbatim + canonical
  digest), its seed, state, run count and expansion summary, creator;
* ``bench_run``: one row per planned run (``seq`` order, group/config ids,
  repetition, family, params, workload seed, conditions, strategy, node,
  origin, state, ``job_id`` unique - the benchmark identity of a job);
  ``campaign_id`` -> ``bench_campaign`` CASCADE;
* ``plan_decision`` gains ``shadow_chosen_node_id`` / ``shadow_estimate_s``:
  the planner's would-have-chosen node for a pinned job (D215).

Downgrade drops both tables (every campaign is lost; their run records stay -
``run_record`` has no foreign keys) and the two decision columns.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
CAMPAIGN_STATES_0022: tuple[str, ...] = ("active", "done", "cancelled")
RUN_STATES_0022: tuple[str, ...] = (
    "planned",
    "submitted",
    "done",
    "failed",
    "skipped",
)
STRATEGIES_0022: tuple[str, ...] = ("local", "mode1")
MAX_SPEC_CHARS_0022 = 256 * 1024
MAX_DETAIL_CHARS_0022 = 512


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "bench_campaign",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("spec_yaml", sa.Text(), nullable=False),
        sa.Column("spec_digest", sa.String(length=71), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("summary", jsonb, nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", tz, server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"state IN ({_listed(CAMPAIGN_STATES_0022)})",
            name=op.f("ck_bench_campaign_state"),
        ),
        sa.CheckConstraint(
            f"length(spec_yaml) <= {MAX_SPEC_CHARS_0022}",
            name=op.f("ck_bench_campaign_spec_yaml"),
        ),
        sa.CheckConstraint("run_count >= 1", name=op.f("ck_bench_campaign_run_count")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bench_campaign")),
    )
    op.create_index(
        "ix_bench_campaign_created_at", "bench_campaign", ["created_at"], unique=False
    )
    op.create_table(
        "bench_run",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("family", sa.String(length=64), nullable=False),
        sa.Column("params", jsonb, nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("conditions", jsonb, nullable=False),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("node_id", sa.String(length=40), nullable=False),
        sa.Column("origin_node_id", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.String(length=MAX_DETAIL_CHARS_0022), nullable=True),
        sa.Column("submitted_at", tz, nullable=True),
        sa.Column("ended_at", tz, nullable=True),
        sa.CheckConstraint(
            f"state IN ({_listed(RUN_STATES_0022)})", name=op.f("ck_bench_run_state")
        ),
        sa.CheckConstraint(
            f"strategy IN ({_listed(STRATEGIES_0022)})",
            name=op.f("ck_bench_run_strategy"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_bench_run_seq")),
        sa.CheckConstraint("repetition >= 1", name=op.f("ck_bench_run_repetition")),
        sa.CheckConstraint(
            "(state = 'planned' AND job_id IS NULL) OR state = 'skipped' OR "
            "(state IN ('submitted', 'done', 'failed') AND job_id IS NOT NULL)",
            name=op.f("ck_bench_run_job_link"),
        ),
        sa.CheckConstraint(
            f"detail IS NULL OR length(detail) <= {MAX_DETAIL_CHARS_0022}",
            name=op.f("ck_bench_run_detail"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["bench_campaign.id"],
            name=op.f("fk_bench_run_campaign_id_bench_campaign"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bench_run")),
        sa.UniqueConstraint(
            "campaign_id", "seq", name=op.f("uq_bench_run_campaign_id_seq")
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_bench_run_job_id")),
    )
    op.create_index(
        "ix_bench_run_campaign_state_seq",
        "bench_run",
        ["campaign_id", "state", "seq"],
        unique=False,
    )
    op.add_column(
        "plan_decision",
        sa.Column("shadow_chosen_node_id", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "plan_decision", sa.Column("shadow_estimate_s", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("plan_decision", "shadow_estimate_s")
    op.drop_column("plan_decision", "shadow_chosen_node_id")
    op.drop_index("ix_bench_run_campaign_state_seq", table_name="bench_run")
    op.drop_table("bench_run")
    op.drop_index("ix_bench_campaign_created_at", table_name="bench_campaign")
    op.drop_table("bench_campaign")
