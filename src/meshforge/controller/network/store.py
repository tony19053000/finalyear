"""The network matrix in PostgreSQL (V0.6 G4, D111).

* :meth:`NetworkStore.record` - one probe result -> ``network_link``: an upsert
  whose EWMA is computed **in the statement** (``alpha * new + (1 - alpha) *
  old``), so concurrent writers cannot lose a sample. A row that was already
  stale restarts from the new sample (``samples = 1``): averaging with an hour-old
  value would hide a link that changed. A node deleted meanwhile (foreign key)
  drops the sample, logged.
* :func:`read_links` - every row (bounded by :data:`MAX_LINKS`).
* :func:`controller_links` - controller -> node bandwidth **from transfer
  history** (no extra traffic): per node, the median ``bytes / duration`` of its
  latest ``transfer_history`` controller transfers of at least
  ``transfer_min_bytes``, read from a bounded window of the newest events.

The read helpers take an :class:`~sqlalchemy.ext.asyncio.AsyncSession` so the
dispatcher reads them once per tick inside its own transaction.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import ColumnElement, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.logging import get_logger
from meshforge.controller.network.links import LinkRow
from meshforge.controller.store.models.data import TransferEvent
from meshforge.controller.store.models.job import Attempt
from meshforge.controller.store.models.network import NetworkLink

__all__ = [
    "CONTROLLER",
    "MAX_LINKS",
    "TRANSFER_SCAN",
    "ControllerLink",
    "NetworkStore",
    "ProbeSample",
    "controller_links",
    "gpu_busy_nodes",
    "read_links",
]

log = get_logger(__name__)

CONTROLLER: Final = "controller"
"""``transfer_event.source`` of a fetch from the controller's blob store."""
MAX_LINKS: Final = 10_000
TRANSFER_SCAN: Final = 5_000
"""Newest controller transfers looked at per read (bounded, index-backed)."""


@dataclass(frozen=True, slots=True)
class ProbeSample:
    src_node_id: str
    dst_node_id: str
    rtt_ms: float
    bw_bytes_s: float


@dataclass(frozen=True, slots=True)
class ControllerLink:
    """Controller -> ``node_id`` bandwidth from transfer history."""

    node_id: str
    bw_bytes_s: float
    samples: int
    last_at: dt.datetime


async def read_links(db: AsyncSession, *, limit: int = MAX_LINKS) -> list[LinkRow]:
    rows = await db.scalars(
        select(NetworkLink)
        .order_by(NetworkLink.src_node_id, NetworkLink.dst_node_id)
        .limit(limit)
    )
    return [
        LinkRow(
            src_node_id=r.src_node_id,
            dst_node_id=r.dst_node_id,
            rtt_ms=float(r.rtt_ms),
            bw_bytes_s=float(r.bw_bytes_s),
            samples=int(r.samples),
            measured_at=r.measured_at,
        )
        for r in rows
    ]


async def controller_links(
    db: AsyncSession,
    *,
    min_bytes: int,
    per_node: int,
    scan: int = TRANSFER_SCAN,
) -> dict[str, ControllerLink]:
    """See module docstring."""
    newest = (
        select(
            TransferEvent.attempt_id,
            TransferEvent.bytes,
            TransferEvent.duration_ms,
            TransferEvent.recorded_at,
            TransferEvent.id,
        )
        .where(
            TransferEvent.source == CONTROLLER,
            TransferEvent.bytes >= min_bytes,
            TransferEvent.duration_ms > 0,
        )
        .order_by(TransferEvent.id.desc())
        .limit(max(1, scan))
        .subquery()
    )
    ranked = (
        select(
            Attempt.node_id.label("node_id"),
            newest.c.bytes,
            newest.c.duration_ms,
            newest.c.recorded_at,
            func.row_number()
            .over(partition_by=Attempt.node_id, order_by=newest.c.id.desc())
            .label("n"),
        )
        .join(Attempt, Attempt.id == newest.c.attempt_id)
        .subquery()
    )
    rows = await db.execute(
        select(
            ranked.c.node_id, ranked.c.bytes, ranked.c.duration_ms, ranked.c.recorded_at
        ).where(ranked.c.n <= max(1, per_node))
    )
    rates: dict[str, list[float]] = {}
    last: dict[str, dt.datetime] = {}
    for node_id, size, ms, at in rows.all():
        key = str(node_id)
        rates.setdefault(key, []).append(int(size) / (int(ms) / 1000.0))
        if key not in last or at > last[key]:
            last[key] = at
    return {
        node: ControllerLink(
            node_id=node,
            bw_bytes_s=float(statistics.median(values)),
            samples=len(values),
            last_at=last[node],
        )
        for node, values in sorted(rates.items())
    }


async def gpu_busy_nodes(db: AsyncSession, live_states: Sequence[str]) -> set[str]:
    """Nodes with a live attempt holding a GPU (a GPU slot, or GPU indices in
    its start snapshot - an attempt from before V0.6 has no slot)."""
    indices = Attempt.start_snapshot["gpu_indices"]
    held = case(
        (func.jsonb_typeof(indices) == "array", func.jsonb_array_length(indices)),
        else_=0,
    )
    rows = await db.scalars(
        select(Attempt.node_id)
        .where(
            Attempt.state.in_(list(live_states)),
            or_(Attempt.slot == "gpu", held > 0),
        )
        .distinct()
    )
    return {str(n) for n in rows}


class NetworkStore:
    """Writes probe results; reads the matrix for the REST view."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def record(
        self,
        sample: ProbeSample,
        *,
        now: dt.datetime,
        alpha: float,
        stale_after_s: float,
    ) -> bool:
        """Fold one probe into its link (module docstring); False = dropped."""
        cutoff = now - dt.timedelta(seconds=stale_after_s)
        table = NetworkLink.__table__
        insert = pg_insert(NetworkLink).values(
            src_node_id=sample.src_node_id,
            dst_node_id=sample.dst_node_id,
            rtt_ms=sample.rtt_ms,
            bw_bytes_s=sample.bw_bytes_s,
            samples=1,
            measured_at=now,
        )
        fresh = table.c.measured_at >= cutoff

        def blend(column: str) -> ColumnElement[Any]:
            new = getattr(insert.excluded, column)
            old = table.c[column]
            return case((fresh, alpha * new + (1.0 - alpha) * old), else_=new)

        statement = insert.on_conflict_do_update(
            index_elements=["src_node_id", "dst_node_id"],
            set_={
                "rtt_ms": blend("rtt_ms"),
                "bw_bytes_s": blend("bw_bytes_s"),
                "samples": case((fresh, table.c.samples + 1), else_=1),
                "measured_at": insert.excluded.measured_at,
            },
        )
        try:
            async with self._sessions.begin() as db:
                await db.execute(statement)
        except IntegrityError:
            log.warning(
                "controller.network.sample_dropped",
                src_node_id=sample.src_node_id,
                dst_node_id=sample.dst_node_id,
                reason="node_gone",
            )
            return False
        return True

    async def gpu_busy(self, live_states: Sequence[str]) -> set[str]:
        async with self._sessions() as db:
            return await gpu_busy_nodes(db, live_states)

    async def matrix(
        self, *, min_bytes: int, per_node: int
    ) -> tuple[list[LinkRow], dict[str, ControllerLink]]:
        async with self._sessions() as db:
            links = await read_links(db)
            ctl = await controller_links(db, min_bytes=min_bytes, per_node=per_node)
        return links, ctl
