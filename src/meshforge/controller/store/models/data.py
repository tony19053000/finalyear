"""Named inputs and transfer telemetry (V0.5 G5, D92/D93; migration 0012).

* ``named_input`` - a dataset/model registered once by ``compute data push``:
  ``(name, version)`` -> ``tree_id``. Versions count up from 1 per name; pushing
  a tree the name already has returns that version (unique ``(name, tree_id)``).
  ``pinned`` inputs are never evicted by GC - on the controller and on every
  agent (``GcRequest.pinned_trees``).
* ``job_input`` - the inputs a job was submitted with, resolved at submit
  (``ref`` as written, the resolved ``name``/``version``/``tree_id``, the
  ``mount`` path). ``compute data rm`` is refused while a non-terminal job
  references the version; the controller's GC keeps those trees.
* ``transfer_event`` - what staging each tree of an attempt cost, one row per
  ``(attempt, tree, source)``: ``source`` is ``controller``, a peer's node id,
  or ``cache`` (the hits: ``units`` = blobs already cached, ``bytes`` = their
  size, ``duration_ms`` = the tree's whole staging, ``failures`` = corrupt
  blobs fetched again, ``resumed_chunks`` = chunks kept from an interrupted
  fetch). Fetch rows: bytes received, request time, units won, failures, why
  the source was dropped. Unique per ``(attempt_id, tree_id, source)`` so a
  replayed terminal report never doubles a row.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base, UInt64, uint64_check
from meshforge.controller.store.models.job import DIGEST_CHECK_SQL, DIGEST_LENGTH

__all__ = [
    "INPUT_NAME_PATTERN",
    "MAX_MOUNT_CHARS",
    "MAX_REF_CHARS",
    "JobInput",
    "NamedInput",
    "TransferEvent",
]

INPUT_NAME_PATTERN: Final = "^[a-z0-9][a-z0-9._-]{0,63}$"
MAX_INPUT_NAME_CHARS: Final = 64
MAX_REF_CHARS: Final = 140
"""``name@sha256:<64 hex>`` = 64 + 1 + 71 = 136."""
MAX_MOUNT_CHARS: Final = 256
MAX_SOURCE_CHARS: Final = 40
MAX_DROPPED_CHARS: Final = 32


class NamedInput(Base):
    __tablename__ = "named_input"
    __table_args__ = (
        PrimaryKeyConstraint("name", "version"),
        UniqueConstraint("name", "tree_id"),
        CheckConstraint(f"name ~ '{INPUT_NAME_PATTERN}'", name="name"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint(DIGEST_CHECK_SQL.format(col="tree_id"), name="tree_id"),
        uint64_check("size", name="size"),
        CheckConstraint("files >= 0", name="files"),
        Index("ix_named_input_tree_id", "tree_id"),
    )

    name: Mapped[str] = mapped_column(String(MAX_INPUT_NAME_CHARS), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    tree_id: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    size: Mapped[int] = mapped_column(UInt64, nullable=False)
    """Total bytes of the tree's files."""
    files: Mapped[int] = mapped_column(Integer, nullable=False)
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JobInput(Base):
    __tablename__ = "job_input"
    __table_args__ = (
        PrimaryKeyConstraint("job_id", "mount"),
        CheckConstraint(DIGEST_CHECK_SQL.format(col="tree_id"), name="tree_id"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_job_input_name_version", "name", "version"),
        Index("ix_job_input_tree_id", "tree_id"),
    )

    job_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH), ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    mount: Mapped[str] = mapped_column(String(MAX_MOUNT_CHARS), nullable=False)
    ref: Mapped[str] = mapped_column(String(MAX_REF_CHARS), nullable=False)
    name: Mapped[str] = mapped_column(String(MAX_INPUT_NAME_CHARS), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    tree_id: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)


class TransferEvent(Base):
    __tablename__ = "transfer_event"
    __table_args__ = (
        UniqueConstraint("attempt_id", "tree_id", "source"),
        CheckConstraint(DIGEST_CHECK_SQL.format(col="tree_id"), name="tree_id"),
        uint64_check("bytes", name="bytes"),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
        CheckConstraint("units >= 0", name="units"),
        CheckConstraint("failures >= 0", name="failures"),
        CheckConstraint("resumed_chunks >= 0", name="resumed_chunks"),
        Index("ix_transfer_event_job_id", "job_id"),
        Index("ix_transfer_event_source_id", "source", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("attempt.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    tree_id: Mapped[str] = mapped_column(String(DIGEST_LENGTH), nullable=False)
    source: Mapped[str] = mapped_column(String(MAX_SOURCE_CHARS), nullable=False)
    """``cache``, ``controller`` or a peer's node id."""
    bytes: Mapped[int] = mapped_column(UInt64, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    resumed_chunks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    dropped: Mapped[str | None] = mapped_column(
        String(MAX_DROPPED_CHARS), nullable=True
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
