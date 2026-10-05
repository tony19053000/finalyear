"""``network_link``: the measured network matrix (V0.6 G4, D111; migration 0014).

One row per **ordered** node pair ``(src, dst)``: what ``src`` measured when it
downloaded from ``dst``'s peer server (``GET /v1/probe``) - so the row is the
``dst -> src`` data direction seen from the fetching side, which is exactly what
the estimator needs (a node fetching a tree from a holder). Values are EWMAs
(``[controller.network] ewma_alpha``, 0.3) over ``samples`` successful probes;
``measured_at`` is the controller's clock at the latest one. A row older than
``stale_after_s`` (1 h) is **stale**: shown, but the estimator falls back.

Controller <-> node links are not stored here: they come from ``transfer_event``
history (no extra traffic, D111). Rows of a node go with it (``ON DELETE
CASCADE``). Failed probes do not write a row (they are counted in memory).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = ["NetworkLink"]


class NetworkLink(Base):
    __tablename__ = "network_link"
    __table_args__ = (
        CheckConstraint("src_node_id <> dst_node_id", name="distinct_nodes"),
        CheckConstraint("rtt_ms >= 0", name="rtt_ms"),
        CheckConstraint("bw_bytes_s > 0", name="bw_bytes_s"),
        CheckConstraint("samples >= 1", name="samples"),
    )

    src_node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("node.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    """The node that ran the probe (the downloader)."""
    dst_node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("node.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    """The node whose peer server answered (the data source)."""
    rtt_ms: Mapped[float] = mapped_column(Float, nullable=False)
    bw_bytes_s: Mapped[float] = mapped_column(Float, nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
