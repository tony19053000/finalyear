"""``ml_model_set`` + ``ml_model``: the ML planner's model registry (V0.15 G3,
D339/D358; migration 0034).

* :class:`MlModelSet` - one row per **training run** (D358): the per-term
  models that ship together. ``set_version`` (``sha256:<hex>``) = the digest
  of the sorted member versions + the feature version, so the same dataset
  and code give the same key. ``status``:

  - ``active`` - the set the planner uses under ``[controller.planner] ml =
    "auto"`` when nothing is ``pinned``; **at most one** (partial unique
    index);
  - ``rejected`` - failed the activation gate (``reason`` says which rule);
    never loaded for planning, never pinnable;
  - ``superseded`` - was active (or pinned) until another set took over; kept
    for rollback (``compute planner ml pin``);
  - ``pinned`` - an operator's pin (``compute planner ml pin``): used instead
    of the active set until unpinned; **at most one**.

  ``gate`` = the activation gate's numbers (per term, per rule; ``regret:
  "pending_g4"`` until G5 wires the D319 regret part, D360); ``inputs`` =
  the data watermark it was trained on, settings, counts, timings.
* :class:`MlModel` - one member per (set, term): ``version`` = the content
  digest of its artefact (the CAS blob ``artefact_digest``; the same bytes);
  ``algorithm = "none"`` = the term has no learned model in this set
  (``reason``: the selection kept a baseline, or too little data) - no
  artefact then. ``params`` = the selected configuration, ``metrics`` = its
  inner cross-validation and gate numbers.

Rows are written only by :class:`~meshforge.controller.ml.registry.
ModelRegistry` (under an advisory lock); a row never changes except a set's
``status`` (+ ``status_changed_at``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import Base

__all__ = [
    "MAX_ML_DIGEST_CHARS",
    "MAX_ML_REASON_CHARS",
    "ML_ALGORITHMS",
    "ML_SET_STATUSES",
    "MlModel",
    "MlModelSet",
]

ML_SET_STATUSES: Final = ("active", "rejected", "superseded", "pinned")
ML_ALGORITHMS: Final = (
    "none",
    "ridge",
    "knn",
    "tree",
    "forest",
    "lightgbm",
    "xgboost",
)
MAX_ML_REASON_CHARS: Final = 512
MAX_ML_DIGEST_CHARS: Final = 80
_STATUSES_SQL: Final = ", ".join(f"'{v}'" for v in ML_SET_STATUSES)
_ALGORITHMS_SQL: Final = ", ".join(f"'{v}'" for v in ML_ALGORITHMS)


class MlModelSet(Base):
    __tablename__ = "ml_model_set"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES_SQL})", name="status"),
        CheckConstraint("jsonb_typeof(gate) = 'object'", name="gate"),
        CheckConstraint("jsonb_typeof(inputs) = 'object'", name="inputs"),
        CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_ML_REASON_CHARS}",
            name="reason",
        ),
        CheckConstraint(
            "status <> 'rejected' OR reason IS NOT NULL", name="rejected_has_reason"
        ),
        CheckConstraint("set_version LIKE 'sha256:%'", name="set_version"),
        CheckConstraint("feature_version >= 1", name="feature_version"),
        Index(
            "ux_ml_model_set_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ux_ml_model_set_pinned",
            "status",
            unique=True,
            postgresql_where=text("status = 'pinned'"),
        ),
        Index("ix_ml_model_set_created_at", "created_at"),
    )

    set_version: Mapped[str] = mapped_column(
        String(MAX_ML_DIGEST_CHARS), primary_key=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    gate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dataset_digest: Mapped[str] = mapped_column(
        String(MAX_ML_DIGEST_CHARS), nullable=False
    )
    feature_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class MlModel(Base):
    __tablename__ = "ml_model"
    __table_args__ = (
        CheckConstraint(f"algorithm IN ({_ALGORITHMS_SQL})", name="algorithm"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params"),
        CheckConstraint("jsonb_typeof(metrics) = 'object'", name="metrics"),
        CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_ML_REASON_CHARS}",
            name="reason",
        ),
        CheckConstraint(
            "(algorithm = 'none') = (artefact_digest IS NULL)",
            name="artefact_iff_learned",
        ),
        CheckConstraint(
            "(artefact_digest IS NULL) = (artefact_bytes IS NULL)",
            name="artefact_bytes",
        ),
        CheckConstraint(
            "artefact_bytes IS NULL OR artefact_bytes > 0",
            name="artefact_bytes_positive",
        ),
        CheckConstraint("version LIKE 'sha256:%'", name="version"),
        Index("ix_ml_model_version", "version"),
    )

    set_version: Mapped[str] = mapped_column(
        String(MAX_ML_DIGEST_CHARS),
        ForeignKey("ml_model_set.set_version", ondelete="CASCADE"),
        primary_key=True,
    )
    term: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[str] = mapped_column(String(MAX_ML_DIGEST_CHARS), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False)
    feature_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dataset_digest: Mapped[str] = mapped_column(
        String(MAX_ML_DIGEST_CHARS), nullable=False
    )
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artefact_digest: Mapped[str | None] = mapped_column(
        String(MAX_ML_DIGEST_CHARS), nullable=True
    )
    artefact_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
