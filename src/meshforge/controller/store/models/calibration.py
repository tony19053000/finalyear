"""``planner_calibration``: versioned planner calibrations (V0.14 G2, D293/D301/D306;
migration 0031).

One row per fitted calibration (:mod:`meshforge.controller.planner.calibration`):

* ``version`` - the identity every calibrated ``plan_decision`` names
  (``plan_decision.calibration_version``) and the replay's ``calibration``
  block carries;
* ``status`` - ``active`` (the one the dispatcher scales estimates by; **at
  most one**, a partial unique index), ``rejected`` (failed the D306 gate -
  ``reason`` says why; never used), ``superseded`` (was active until a newer
  version passed the gate) or ``pinned`` (reserved for an operator pin kept in
  the database; G2's pin is ``[controller.planner] calibration = <version>``
  and changes no row);
* ``factors`` - :func:`~meshforge.controller.planner.calibration.
  calibration_json` (per term: global, per node, per node + family; plus the
  fingerprint -> family map);
* ``fit_inputs`` - what it was fitted on (pairs, held-out count, window,
  shrinkage ``k``, clamp, the version it was judged against);
* ``holdout_error_new`` / ``holdout_error_active`` - mean ``|ln(pred/actual)|``
  of this version and of the version active then, on the same held-out runs.

Rows are written only by :class:`~meshforge.controller.calibration_store.
CalibrationStore` (one transaction under an advisory lock); a row is never
changed except its ``status`` moving ``active`` -> ``superseded``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Identity,
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
    "CALIBRATION_STATUSES",
    "MAX_CALIBRATION_REASON_CHARS",
    "PlannerCalibration",
]

CALIBRATION_STATUSES: Final = ("active", "rejected", "superseded", "pinned")
MAX_CALIBRATION_REASON_CHARS: Final = 512
_STATUSES_SQL: Final = ", ".join(f"'{v}'" for v in CALIBRATION_STATUSES)


class PlannerCalibration(Base):
    __tablename__ = "planner_calibration"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES_SQL})", name="status"),
        CheckConstraint("jsonb_typeof(factors) = 'object'", name="factors"),
        CheckConstraint("jsonb_typeof(fit_inputs) = 'object'", name="fit_inputs"),
        CheckConstraint(
            "holdout_error_new IS NULL OR holdout_error_new >= 0", name="error_new"
        ),
        CheckConstraint(
            "holdout_error_active IS NULL OR holdout_error_active >= 0",
            name="error_active",
        ),
        CheckConstraint(
            f"reason IS NULL OR length(reason) <= {MAX_CALIBRATION_REASON_CHARS}",
            name="reason",
        ),
        CheckConstraint(
            "status <> 'rejected' OR reason IS NOT NULL", name="rejected_has_reason"
        ),
        Index(
            "ux_planner_calibration_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    version: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fit_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    holdout_error_new: Mapped[float | None] = mapped_column(Float, nullable=True)
    holdout_error_active: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
