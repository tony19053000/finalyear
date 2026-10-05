"""``plan_decision``: what the planner saw and chose (V0.6 G2, D112; migration 0013).

One row per planner run that a user may ask about (``compute explain``):

* ``dispatched`` - written in the **same transaction** that creates the attempt
  (``attempt_id`` set), so a decision without its attempt, or an attempt without
  its decision, cannot exist (D115);
* ``waiting`` - the job stays queued because no node can take it *now*; written
  once per distinct reason set (``reason_key``), not once per dispatch tick;
* ``failed`` - the job failed because no node could ever take it.

``candidates`` is a bounded JSON list (at most :data:`MAX_DECISION_CANDIDATES`
entries): per node ``{node_id, mode, feasible, reject_code, reject_text,
estimate, score, rank}``. Actual phase timings are **not** copied here; they
are joined through ``attempt_id`` (``attempt_phase``), so a decision and its
outcome are one query apart - V0.15's training rows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.common.manifest import POLICIES
from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = [
    "DECISION_OUTCOMES",
    "MAX_DECISION_CANDIDATES",
    "PlanDecision",
]

MAX_DECISION_CANDIDATES: Final = 64
DECISION_OUTCOMES: Final = ("dispatched", "waiting", "failed")
_OUTCOMES_SQL: Final = ", ".join(f"'{v}'" for v in DECISION_OUTCOMES)
_POLICIES_SQL: Final = ", ".join(f"'{v}'" for v in POLICIES)


class PlanDecision(Base):
    __tablename__ = "plan_decision"
    __table_args__ = (
        CheckConstraint(f"outcome IN ({_OUTCOMES_SQL})", name="outcome"),
        CheckConstraint(f"policy IN ({_POLICIES_SQL})", name="policy"),
        CheckConstraint("attempt_no >= 1", name="attempt_no"),
        CheckConstraint(
            "jsonb_typeof(candidates) = 'array' AND "
            f"jsonb_array_length(candidates) <= {MAX_DECISION_CANDIDATES}",
            name="candidates",
        ),
        CheckConstraint(
            "(outcome = 'dispatched') = (attempt_id IS NOT NULL)",
            name="dispatched_has_attempt",
        ),
        Index("ix_plan_decision_job_id", "job_id", "id"),
        Index("ix_plan_decision_attempt_id", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    """The fencing token of the attempt this decision placed (or would place)."""
    attempt_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("attempt.id", ondelete="CASCADE"),
        nullable=True,
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    chosen_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of the canonical planner input (request + snapshot): two
    decisions with the same digest saw exactly the same fabric."""
    reason_key: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of the sorted ``(node_id, reject_code)`` set: a waiting job's
    decision is written again only when this changes (D104)."""
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
