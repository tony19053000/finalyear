"""``work_queue``, ``work_item``, ``work_lease`` (V0.10 D262; migration 0025).

The tables live on their own :data:`METADATA` (same naming convention as the
controller's ``Base.metadata``; a unit test holds the two equal) so this
package imports nothing from the controller; Alembic's ``env.py`` compares both
metadata objects with the database (``alembic check`` fails on drift).

* ``work_queue`` - one per owner ``(kind, owner_id)`` (opaque: a Mode 3 job,
  a Mode 4 inference session, ...). Carries the policy
  (:class:`~.core.QueuePolicy`) and counters kept in step under the queue row
  lock: ``item_count``, ``done_count`` (the last commit sees ``done_count =
  item_count`` in its own transaction) and ``speculated_count``.
* ``work_item`` - one row per item, ``idx`` 0..n-1 in the order the owner gave.
  ``lease_seq`` is the newest lease's sequence number; ``done_lease_seq`` /
  ``result_digest`` the winner's.
* ``work_lease`` - every lease ever issued (history for telemetry and fencing).
  The partial unique index ``(item_id, speculative) WHERE state = 'live'`` is
  the database's own guarantee of "at most one ordinary and one speculative
  live lease per item" (:data:`~.core.MAX_LIVE_LEASES`).

Identifiers are ``bigint`` identities, not prefixed ULIDs: these rows are
internal to one controller, created in bulk (10 000 items in one statement)
and referenced from outside only as ``(item_id, seq)`` - which is what fences.
IDs are not secrets (lease ownership is checked by holder, D273).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from meshforge.workqueue.core import (
    MAX_DIGEST_CHARS,
    MAX_FAILURE_KIND_CHARS,
    MAX_NAME_CHARS,
    ItemState,
    LeaseState,
    QueueState,
)

__all__ = [
    "MAX_KIND_CHARS",
    "MAX_OWNER_CHARS",
    "METADATA",
    "NAMING_CONVENTION",
    "work_item",
    "work_lease",
    "work_queue",
]

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Copy of ``meshforge.controller.store.base.NAMING_CONVENTION`` (frozen there;
a unit test fails if they differ)."""

METADATA: Final = MetaData(naming_convention=NAMING_CONVENTION)

MAX_KIND_CHARS: Final = 32
MAX_OWNER_CHARS: Final = 64

_TZ = DateTime(timezone=True)


def _in(values: type[ItemState] | type[LeaseState] | type[QueueState]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


work_queue: Final = Table(
    "work_queue",
    METADATA,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("kind", String(MAX_KIND_CHARS), nullable=False),
    Column("owner_id", String(MAX_OWNER_CHARS), nullable=False),
    Column("state", String(16), nullable=False),
    Column("visibility_timeout_s", Float, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("max_failed_sites", Integer, nullable=True),
    Column("count_expiry", Boolean, nullable=False),
    Column("speculative_max_frac", Float, nullable=False),
    Column("item_count", Integer, nullable=False),
    Column("done_count", Integer, server_default=text("0"), nullable=False),
    Column("speculated_count", Integer, server_default=text("0"), nullable=False),
    Column("created_at", _TZ, server_default=func.now(), nullable=False),
    Column("closed_at", _TZ, nullable=True),
    CheckConstraint(f"state IN ({_in(QueueState)})", name="state"),
    CheckConstraint(
        "visibility_timeout_s > 0 AND visibility_timeout_s <= 86400",
        name="visibility_timeout_s",
    ),
    CheckConstraint("max_attempts BETWEEN 1 AND 100", name="max_attempts"),
    CheckConstraint(
        "max_failed_sites IS NULL OR max_failed_sites BETWEEN 1 AND 100",
        name="max_failed_sites",
    ),
    CheckConstraint(
        "speculative_max_frac >= 0 AND speculative_max_frac <= 1",
        name="speculative_max_frac",
    ),
    CheckConstraint("item_count >= 1", name="item_count"),
    CheckConstraint("done_count BETWEEN 0 AND item_count", name="done_count"),
    CheckConstraint(
        "speculated_count BETWEEN 0 AND item_count", name="speculated_count"
    ),
    UniqueConstraint("kind", "owner_id"),
)

work_item: Final = Table(
    "work_item",
    METADATA,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "queue_id",
        BigInteger,
        ForeignKey("work_queue.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("idx", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempts", Integer, server_default=text("0"), nullable=False),
    Column("lease_seq", Integer, server_default=text("0"), nullable=False),
    Column("speculations", Integer, server_default=text("0"), nullable=False),
    Column("result_digest", String(MAX_DIGEST_CHARS), nullable=True),
    Column("done_lease_seq", Integer, nullable=True),
    Column("failed_kind", String(MAX_FAILURE_KIND_CHARS), nullable=True),
    Column(
        "failed_sites",
        ARRAY(String(MAX_NAME_CHARS)),
        server_default=text("'{}'"),
        nullable=False,
    ),
    Column("done_at", _TZ, nullable=True),
    CheckConstraint(f"state IN ({_in(ItemState)})", name="state"),
    CheckConstraint("idx >= 0", name="idx"),
    CheckConstraint("attempts >= 0", name="attempts"),
    CheckConstraint("lease_seq >= 0", name="lease_seq"),
    CheckConstraint("speculations >= 0", name="speculations"),
    CheckConstraint(
        "(state = 'done') = (result_digest IS NOT NULL AND done_lease_seq IS NOT NULL)",
        name="done_result",
    ),
    CheckConstraint(
        "(state = 'failed') = (failed_kind IS NOT NULL)", name="failed_kind"
    ),
    UniqueConstraint("queue_id", "idx"),
    Index(
        "ix_work_item_pending",
        "queue_id",
        "idx",
        postgresql_where=text("state = 'pending'"),
    ),
    Index("ix_work_item_queue_id_state", "queue_id", "state"),
)

work_lease: Final = Table(
    "work_lease",
    METADATA,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "item_id",
        BigInteger,
        ForeignKey("work_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    Column("holder", String(MAX_NAME_CHARS), nullable=False),
    Column("site", String(MAX_NAME_CHARS), nullable=False),
    Column("state", String(16), nullable=False),
    Column("speculative", Boolean, nullable=False),
    Column("leased_at", _TZ, nullable=False),
    Column("deadline", _TZ, nullable=False),
    Column("ended_at", _TZ, nullable=True),
    CheckConstraint(f"state IN ({_in(LeaseState)})", name="state"),
    CheckConstraint("seq >= 1", name="seq"),
    CheckConstraint("deadline >= leased_at", name="deadline"),
    UniqueConstraint("item_id", "seq"),
    Index(
        "ix_work_lease_live_slot",
        "item_id",
        "speculative",
        unique=True,
        postgresql_where=text("state = 'live'"),
    ),
    Index(
        "ix_work_lease_live_deadline",
        "deadline",
        postgresql_where=text("state = 'live'"),
    ),
    Index(
        "ix_work_lease_live_holder",
        "holder",
        postgresql_where=text("state = 'live'"),
    ),
)
