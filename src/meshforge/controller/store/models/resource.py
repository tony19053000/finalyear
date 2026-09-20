"""``resource_snapshot``: the downsampled usage history (ARCHITECTURE §19, 0.3.11).

One row per ``(node_id, bucket_start)``, written by the controller's snapshot
flusher from :class:`~meshforge.controller.resources.Downsampler` - the agent never
writes history, it only reports live samples. A bucket is ``controller.resources
.bucket_s`` long (default 60 s) and is written only once it is **closed**, so a row
is always complete.

The primary key is the bucket itself, which makes a re-flush idempotent: the writer
upserts, so replaying the same bucket (a retry after a database hiccup) updates the
row instead of duplicating it.

Per-GPU and per-disk aggregates live in two JSONB columns rather than child tables:
they are read as a unit ("show me this node's history"), their shape is the node's
hardware and not a fabric entity, and at four nodes a join table would be cost
without a query to serve. ``gpu`` is keyed by device index, ``disks`` by mount point.

Retention (``controller.resources.retention_days``, default 7) is a periodic delete
over the ``bucket_start`` index - not a partitioned table, which is premature at
this size (brief §5d D11).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import REAL, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import (
    ID_MAX_LENGTH,
    Base,
    TimestampMixin,
    UInt64,
    uint64_check,
)

__all__ = [
    "CPU_MAX_CHECK_NAME",
    "CPU_MEAN_CHECK_NAME",
    "RAM_MAX_CHECK_NAME",
    "RAM_MEAN_CHECK_NAME",
    "RAM_TOTAL_CHECK_NAME",
    "SAMPLES_CHECK_NAME",
    "ResourceSnapshot",
]

SAMPLES_CHECK_NAME: Final = "samples"
CPU_MEAN_CHECK_NAME: Final = "cpu_percent_mean"
CPU_MAX_CHECK_NAME: Final = "cpu_percent_max"
RAM_MEAN_CHECK_NAME: Final = "ram_used_bytes_mean"
RAM_MAX_CHECK_NAME: Final = "ram_used_bytes_max"
RAM_TOTAL_CHECK_NAME: Final = "ram_total_bytes"

SAMPLES_CHECK: Final = "samples > 0"
CPU_MEAN_CHECK: Final = "cpu_percent_mean >= 0 AND cpu_percent_mean <= 100"
CPU_MAX_CHECK: Final = "cpu_percent_max >= 0 AND cpu_percent_max <= 100"


class ResourceSnapshot(TimestampMixin, Base):
    """One closed downsample bucket of one node's resource usage."""

    __tablename__ = "resource_snapshot"
    __table_args__ = (
        CheckConstraint(SAMPLES_CHECK, name=SAMPLES_CHECK_NAME),
        CheckConstraint(CPU_MEAN_CHECK, name=CPU_MEAN_CHECK_NAME),
        CheckConstraint(CPU_MAX_CHECK, name=CPU_MAX_CHECK_NAME),
        uint64_check("ram_used_bytes_mean", name=RAM_MEAN_CHECK_NAME),
        uint64_check("ram_used_bytes_max", name=RAM_MAX_CHECK_NAME),
        uint64_check("ram_total_bytes", name=RAM_TOTAL_CHECK_NAME),
        Index("ix_resource_snapshot_bucket_start", "bucket_start"),
    )

    node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("node.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    bucket_start: Mapped[dt.datetime] = mapped_column(primary_key=True)
    """Inclusive start of the bucket, UTC, aligned to ``bucket_s``."""
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_percent_mean: Mapped[float] = mapped_column(REAL, nullable=False)
    cpu_percent_max: Mapped[float] = mapped_column(REAL, nullable=False)
    ram_used_bytes_mean: Mapped[int] = mapped_column(UInt64, nullable=False)
    ram_used_bytes_max: Mapped[int] = mapped_column(UInt64, nullable=False)
    ram_total_bytes: Mapped[int] = mapped_column(UInt64, nullable=False)
    gpu: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """Per device index: ``util_mean/util_max/vram_used_mean/vram_used_max/vram_total``."""
    disks: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """Per mount point: ``free_min/free_mean/total``."""
