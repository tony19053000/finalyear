"""``plan_decision``: what the planner saw and chose (V0.6 G2, D112; migration 0013).

One row per planner run that a user may ask about (``compute explain``):

* ``dispatched`` - written in the **same transaction** that creates the attempt
  (``attempt_id`` set), so a decision without its attempt, or an attempt without
  its decision, cannot exist (D115);
* ``waiting`` - the job stays queued because no node can take it *now*; written
  once per distinct reason set (``reason_key``), not once per dispatch tick;
* ``failed`` - the job failed because no node could ever take it.

V0.14 G1 (D292): ``replay`` makes the row re-runnable - ``plan()`` on it
reproduces the decision exactly (``controller/planner/replay.py``).

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
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.common.manifest import POLICIES
from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = [
    "DECISION_OUTCOMES",
    "MAX_DECISION_CANDIDATES",
    "PLAN_OBJECTIVES",
    "PlanDecision",
]

MAX_DECISION_CANDIDATES: Final = 64
DECISION_OUTCOMES: Final = ("dispatched", "waiting", "failed")
_OUTCOMES_SQL: Final = ", ".join(f"'{v}'" for v in DECISION_OUTCOMES)
PLAN_OBJECTIVES: Final = ("min_time", "min_network", "prefer_local")
"""V0.14 D295: the objectives a decision may rank by (migration 0030)."""
_OBJECTIVES_SQL: Final = ", ".join(f"'{v}'" for v in PLAN_OBJECTIVES)
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
        CheckConstraint(
            "(replay IS NULL) = (replay_version IS NULL) AND "
            "(replay IS NULL OR jsonb_typeof(replay) = 'object') AND "
            "(replay_version IS NULL OR replay_version >= 1)",
            name="replay",
        ),
        CheckConstraint(
            f"objective IS NULL OR objective IN ({_OBJECTIVES_SQL})",
            name="objective",
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
    decisions with the same digest saw exactly the same fabric. Since V0.14
    G1 the dispatcher plans on the snapshot cut to what the planner reads for
    the job (the replay's), so this is the digest of the replay's inputs."""
    reason_key: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 of the sorted ``(node_id, reject_code)`` set: a waiting job's
    decision is written again only when this changes (D104)."""
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    shadow_chosen_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    """V0.9 G3 (D215, migration 0022): for a **pinned** job, the node the planner
    would have chosen for the same request unpinned, on the same snapshot (a
    side-effect-free shadow plan); None = not pinned, or no node feasible."""
    shadow_estimate_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    """The shadow choice's estimated total seconds."""
    shadow_node_set: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """V0.10 G8 (D271, migration 0029): a forced **split** benchmark job's
    shadow pick when it is a Mode 3 set - its nodes, fastest first (the
    head is ``shadow_chosen_node_id``); None = the shadow ran one lane (or
    there is no shadow)."""
    replay: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    """V0.14 G1 (D292, migration 0030): the decision's replay snapshot - the
    planner's exact inputs (``controller/planner/replay.py``), so ``plan()``
    on it gives this decision again, or another policy's on the same
    situation. None = a row from before V0.14, or a replay over
    ``[controller.planner] replay_max_bytes``."""
    replay_version: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """The replay format's version (1); set exactly when ``replay`` is."""
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    """V0.14 D295: the objective the decision ranked by (``min_time`` until
    G3); None = a row from before V0.14."""
    calibration_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """V0.14 D293: the calibration its estimates were scaled by; None =
    uncalibrated (every row until G2)."""
