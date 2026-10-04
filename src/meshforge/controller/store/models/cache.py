"""``cache_location``: which node holds which cached thing (V0.5 D90/D100/D101).

One row per ``(node_id, kind, key)``, maintained from the agents' cache reports
(:mod:`meshforge.controller.cache_index`): a paged snapshot after every accepted
Hello replaces a node's rows, deltas then add and remove single rows. Kinds:

* ``blob`` - a CAS blob, key ``sha256:<64 hex>``;
* ``tree`` - a complete tree (manifest + every member blob), key = tree_id;
* ``env`` - a built environment, key = env hash (64 hex);
* ``image`` - a container image the agent built, key ``sha256:<64 hex>`` image id.

The dispatcher's peer listing (D100) reads ``kind = 'tree' AND key = :tree_id``
through the ``(kind, key)`` index: one row per holder, no per-blob join. Rows of
a node go with it (``ON DELETE CASCADE``). ``last_seen`` is the controller's
clock when the row was last (re)reported.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base, UInt64, uint64_check

__all__ = [
    "CACHE_KINDS",
    "KEY_MAX_LENGTH",
    "KIND_CHECK",
    "KIND_CHECK_NAME",
    "SIZE_CHECK_NAME",
    "CacheLocation",
]

CACHE_KINDS: Final = ("blob", "tree", "env", "image")
KEY_MAX_LENGTH: Final = 80
"""``sha256:`` + 64 hex = 71; room for a future algorithm prefix."""
KIND_CHECK_NAME: Final = "kind"
SIZE_CHECK_NAME: Final = "size"
KIND_CHECK: Final = "kind IN (" + ", ".join(f"'{k}'" for k in CACHE_KINDS) + ")"


class CacheLocation(Base):
    """One cached item on one node."""

    __tablename__ = "cache_location"
    __table_args__ = (
        CheckConstraint(KIND_CHECK, name=KIND_CHECK_NAME),
        uint64_check("size", name=SIZE_CHECK_NAME),
        Index("ix_cache_location_kind_key", "kind", "key"),
    )

    node_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("node.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    key: Mapped[str] = mapped_column(String(KEY_MAX_LENGTH), primary_key=True)
    size: Mapped[int] = mapped_column(UInt64, nullable=False)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
