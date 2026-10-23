"""``planner_eval_report``: stored planner evaluation reports (V0.14 G4, D318; migration 0032).

One row per ``compute planner eval`` / ``POST /v1/planner/eval-reports``:

* ``campaign_ids`` - the campaigns evaluated (a sorted JSON array of ids);
* ``holdout`` - the one workload family evaluated (``--holdout``), or NULL
  for every family (each with its own leave-one-family-out calibration);
* ``leaked`` - made with ``--allow-leak`` (every verdict stamped ``leaked``);
* ``report`` - the report itself (``eval_version`` 1 or 2, the shape of
  :class:`~meshforge.controller.api.planner_eval_models.EvalReportOut`), or
  - V0.15 G6b (D378, start-then-poll, no migration) - a ``run_state``
  document while the evaluation runs or after it failed.

Written only by :class:`~meshforge.controller.planner_eval.store.
EvalReportStore`; a row changes at most once, from ``running`` to ``done``
(the report) or ``failed``; a done row is never changed. ``created_at`` is
the row's (the evaluation's start), never part of the report (the same inputs
give the same report bytes).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import Base

__all__ = ["MAX_HOLDOUT_CHARS", "PlannerEvalReport"]

MAX_HOLDOUT_CHARS: Final = 64
"""A workload family name's bound (``bench_run.family`` is ``String(64)``)."""


class PlannerEvalReport(Base):
    __tablename__ = "planner_eval_report"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(campaign_ids) = 'array'", name="campaign_ids"),
        CheckConstraint("jsonb_typeof(report) = 'object'", name="report"),
        CheckConstraint(
            f"holdout IS NULL OR (length(holdout) BETWEEN 1 AND {MAX_HOLDOUT_CHARS})",
            name="holdout",
        ),
        Index("ix_planner_eval_report_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    campaign_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    holdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    leaked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
