"""ml_model_set + ml_model: the ML planner's model registry (V0.15 G3, D339/D358)

Hand-written to match the models exactly (``alembic check`` in the component
tests fails on drift).

* ``ml_model_set`` - one row per **training run** (D358): the per-term models
  that ship, are gated, activated, pinned and rolled back together.
  ``set_version`` (``sha256:<hex>``, the key) = the digest of the sorted
  member versions + the feature version; ``status`` ``active`` / ``rejected``
  / ``superseded`` / ``pinned``; ``reason`` (required when ``rejected``);
  ``gate`` (the activation gate's numbers, JSON object); ``dataset_digest``
  and ``feature_version`` (what it was trained on); ``inputs`` (the data
  watermark, settings, counts - JSON object); ``status_changed_at``.
* ``ux_ml_model_set_active`` / ``ux_ml_model_set_pinned`` - partial unique
  indexes: **at most one** ``active`` and at most one ``pinned`` set,
  whatever two writers do at once.
* ``ml_model`` - one row per (set, term): ``version`` (content digest of the
  artefact; for ``algorithm = 'none'`` the digest of the member's descriptor
  - a term the set has no learned model for, with its ``reason``),
  ``algorithm``, ``params`` / ``metrics`` (JSON objects), ``artefact_digest``
  + ``artefact_bytes`` (the CAS blob; NULL exactly when ``none``).
  ``ix_ml_model_version`` finds the sets a model version is in.

No existing row changes. Downgrade drops both tables (development only).

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies (a later migration that adds a value must not change this one).
STATUSES_0034 = "'active', 'rejected', 'superseded', 'pinned'"
ALGORITHMS_0034 = "'none', 'ridge', 'knn', 'tree', 'forest', 'lightgbm', 'xgboost'"
MAX_REASON_0034 = 512
DIGEST_0034 = 80
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "ml_model_set",
        sa.Column("set_version", sa.String(length=DIGEST_0034), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("gate", _JSONB, nullable=False),
        sa.Column("dataset_digest", sa.String(length=DIGEST_0034), nullable=False),
        sa.Column("feature_version", sa.BigInteger(), nullable=False),
        sa.Column("inputs", _JSONB, nullable=False),
        sa.CheckConstraint(
            f"status IN ({STATUSES_0034})", name=op.f("ck_ml_model_set_status")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(gate) = 'object'", name=op.f("ck_ml_model_set_gate")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(inputs) = 'object'", name=op.f("ck_ml_model_set_inputs")
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_REASON_0034}",
            name=op.f("ck_ml_model_set_reason"),
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR reason IS NOT NULL",
            name=op.f("ck_ml_model_set_rejected_has_reason"),
        ),
        sa.CheckConstraint(
            "set_version LIKE 'sha256:%'", name=op.f("ck_ml_model_set_set_version")
        ),
        sa.CheckConstraint(
            "feature_version >= 1", name=op.f("ck_ml_model_set_feature_version")
        ),
        sa.PrimaryKeyConstraint("set_version", name=op.f("pk_ml_model_set")),
    )
    op.create_index(
        "ux_ml_model_set_active",
        "ml_model_set",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ux_ml_model_set_pinned",
        "ml_model_set",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'pinned'"),
    )
    op.create_index("ix_ml_model_set_created_at", "ml_model_set", ["created_at"])
    op.create_table(
        "ml_model",
        sa.Column("set_version", sa.String(length=DIGEST_0034), nullable=False),
        sa.Column("term", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=DIGEST_0034), nullable=False),
        sa.Column("algorithm", sa.String(length=16), nullable=False),
        sa.Column("feature_version", sa.BigInteger(), nullable=False),
        sa.Column("dataset_digest", sa.String(length=DIGEST_0034), nullable=False),
        sa.Column("params", _JSONB, nullable=False),
        sa.Column("metrics", _JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("artefact_digest", sa.String(length=DIGEST_0034), nullable=True),
        sa.Column("artefact_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"algorithm IN ({ALGORITHMS_0034})", name=op.f("ck_ml_model_algorithm")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(params) = 'object'", name=op.f("ck_ml_model_params")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metrics) = 'object'", name=op.f("ck_ml_model_metrics")
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_REASON_0034}",
            name=op.f("ck_ml_model_reason"),
        ),
        sa.CheckConstraint(
            "(algorithm = 'none') = (artefact_digest IS NULL)",
            name=op.f("ck_ml_model_artefact_iff_learned"),
        ),
        sa.CheckConstraint(
            "(artefact_digest IS NULL) = (artefact_bytes IS NULL)",
            name=op.f("ck_ml_model_artefact_bytes"),
        ),
        sa.CheckConstraint(
            "artefact_bytes IS NULL OR artefact_bytes > 0",
            name=op.f("ck_ml_model_artefact_bytes_positive"),
        ),
        sa.CheckConstraint("version LIKE 'sha256:%'", name=op.f("ck_ml_model_version")),
        sa.ForeignKeyConstraint(
            ["set_version"],
            ["ml_model_set.set_version"],
            name=op.f("fk_ml_model_set_version_ml_model_set"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("set_version", "term", name=op.f("pk_ml_model")),
    )
    op.create_index("ix_ml_model_version", "ml_model", ["version"])


def downgrade() -> None:
    op.drop_index("ix_ml_model_version", table_name="ml_model")
    op.drop_table("ml_model")
    op.drop_index("ix_ml_model_set_created_at", table_name="ml_model_set")
    op.drop_index("ux_ml_model_set_pinned", table_name="ml_model_set")
    op.drop_index("ux_ml_model_set_active", table_name="ml_model_set")
    op.drop_table("ml_model_set")
