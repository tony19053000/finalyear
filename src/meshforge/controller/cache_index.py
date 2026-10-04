"""The cache location index: who holds which blob / tree / env / image (V0.5 D90,
D100, D101).

Agents report what they cache with ``CacheReport`` (a report: buffered, replayed
after a reconnect, acked). After every accepted Hello an agent sends a paged
**snapshot** (``snapshot=true`` on every page, ``snapshot_last`` on the final
one), then **deltas**. ``seq`` is monotonic per agent process (never reset by a
reconnect), so a page or delta replayed from an earlier session of the same
process is recognisably old.

Per node, :class:`CacheIndex` keeps one small state machine for the node's
*current* session (:func:`decide`, pure and unit-tested):

* ``await_snapshot`` - every new session starts here. Deltas are dropped (they
  are old replays; the snapshot to come supersedes them).
* a snapshot page is the **first page of a new snapshot** only as the
  session's first report or right after a boundary (a delta or a snapshot's
  last page, seq contiguous): the node's rows are replaced by it
  (``collecting``, or ``live`` when it is also ``snapshot_last``); contiguous
  pages are added; a page after a gap makes the node ``stale`` (fix round 1).
* ``live`` - a delta with ``seq == next`` is applied; ``seq < next`` is a
  duplicate (ignored); ``seq > next`` is a **gap**: the node goes ``stale`` and
  every delta is dropped until the next snapshot (the agent resnapshots at every
  Hello and every ``[agent.cache] snapshot_interval_s``), so a lost delta never
  leaves a silently wrong index for longer than that.
* a database write that fails also makes the node ``stale``.

Writes go on the session's :class:`~meshforge.controller.session.writer
.WriteChain` (ordered, never awaited by the reader) and are **fenced** on the
session: each transaction first takes ``FOR SHARE`` on the node row with
``last_session_id = :session``, so a write queued by a superseded session can
never land after the new session's snapshot (the new Hello's row update waits
for it, or it finds another session and does nothing).

Reports are hostile input: at most :data:`MAX_ITEMS` items per message, known
kinds only, keys of the exact shape of their kind, no ``removed`` on a snapshot
page; an invalid report is dropped (logged, counted) and still acked - the agent
must not replay it forever. The node's index then goes ``stale``.
"""

from __future__ import annotations

import enum
import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.live import LiveSession
from meshforge.controller.session.writer import WriteKind
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.cache import CacheLocation
from meshforge.controller.store.models.node import Node

__all__ = [
    "MAX_ITEMS",
    "CacheIndex",
    "CacheLocationStore",
    "CacheRow",
    "Decision",
    "NodeCacheState",
    "Phase",
    "decide",
    "validate_report",
]

log = get_logger(__name__)

MAX_ITEMS: Final = 1024
"""Items (added + removed) one CacheReport may carry (D101)."""
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_KINDS: Final[dict[int, str]] = {
    agent_pb2.CacheItem.KIND_BLOB: "blob",
    agent_pb2.CacheItem.KIND_TREE: "tree",
    agent_pb2.CacheItem.KIND_ENV: "env",
    agent_pb2.CacheItem.KIND_IMAGE: "image",
}
_INSERT_BATCH: Final = 1024


@dataclass(frozen=True, slots=True)
class CacheRow:
    kind: str
    key: str
    size: int


def _row(item: agent_pb2.CacheItem) -> CacheRow | str:
    kind = _KINDS.get(item.kind)
    if kind is None:
        return "unknown_kind"
    key = item.key
    pattern = _HEX_RE if kind == "env" else _DIGEST_RE
    if not pattern.fullmatch(key):
        return "bad_key"
    return CacheRow(kind, key, int(item.size))


def validate_report(
    report: agent_pb2.CacheReport,
) -> tuple[tuple[CacheRow, ...], tuple[CacheRow, ...]] | str:
    """``(added, removed)`` rows, or the reason the report is refused."""
    if len(report.added) + len(report.removed) > MAX_ITEMS:
        return "too_many_items"
    if report.seq == 0:
        return "no_seq"
    if report.snapshot_last and not report.snapshot:
        return "last_without_snapshot"
    if report.snapshot and report.removed:
        return "removed_in_snapshot"
    if report.HasField("page") and not report.snapshot:
        return "page_without_snapshot"
    added: list[CacheRow] = []
    removed: list[CacheRow] = []
    for items, out in ((report.added, added), (report.removed, removed)):
        for item in items:
            row = _row(item)
            if isinstance(row, str):
                return row
            out.append(row)
    return tuple(added), tuple(removed)


# ============================================================ state machine


class Phase(enum.StrEnum):
    AWAIT_SNAPSHOT = "await_snapshot"
    COLLECTING = "collecting"
    LIVE = "live"
    STALE = "stale"


class Decision(enum.StrEnum):
    REPLACE = "replace"
    """A new snapshot starts: the node's rows become this page."""
    ADD_PAGE = "add_page"
    DELTA = "delta"
    DUPLICATE = "duplicate"
    EARLY = "early"
    """A delta before the session's snapshot completed (an old replay)."""
    GAP = "gap"
    STALE = "stale"
    """Dropped: the node's index is stale until the next snapshot."""
    UNPAGED = "unpaged"
    """A multi-page snapshot without ``page`` (an older agent): the node is
    stale until a snapshot it can trust (logged as a warning)."""


@dataclass(slots=True)
class NodeCacheState:
    session_id: str
    phase: Phase = Phase.AWAIT_SNAPSHOT
    last_seq: int = 0
    """Highest ``seq`` seen on this session (0 = none yet)."""
    boundary: bool = True
    """The message at ``last_seq`` ended something (a delta or a snapshot's last
    page): a snapshot page right after it is the first page of a new snapshot."""


def decide(
    state: NodeCacheState,
    *,
    seq: int,
    snapshot: bool,
    last: bool,
    page: int | None = None,
    page_missing: bool = False,
) -> Decision:
    """Apply one report's sequencing to ``state`` (mutated); return what to do.

    Snapshot pages must be **contiguous** (G4 fix round 1): a page is the first
    of a new snapshot only as the session's first report or right after a
    boundary (a delta or a snapshot's last page); any other non-contiguous page
    means a page was lost, and the node goes ``stale`` until a snapshot starts
    cleanly - it never goes ``live`` with part of a snapshot.

    ``page`` (``CacheReport.page``, fix round 2): when known, only page 0 starts
    a snapshot and a page > 0 only continues one - a session whose first report
    is the tail of a snapshot goes ``stale``, never ``live``. ``None`` (callers
    without the field) keeps the seq-only rule above.

    ``page_missing`` (fix round 3): a snapshot page from an agent that sends no
    ``page`` at all. It is accepted only as a whole single-page snapshot (the
    last page, right where a snapshot may start); any other unpaged page makes
    the node ``stale`` - its earlier pages cannot be told apart from a lost head.
    """
    if state.last_seq and seq <= state.last_seq:
        return Decision.DUPLICATE
    first = state.last_seq == 0
    contiguous = first or seq == state.last_seq + 1
    after_boundary = contiguous and state.boundary
    state.last_seq = seq
    state.boundary = (not snapshot) or last
    if snapshot and page_missing:
        if last and (first or after_boundary):
            state.phase = Phase.LIVE
            return Decision.REPLACE
        state.phase = Phase.STALE
        return Decision.UNPAGED
    if snapshot and page is not None:
        if page == 0:
            state.phase = Phase.LIVE if last else Phase.COLLECTING
            return Decision.REPLACE
        if state.phase is Phase.COLLECTING and contiguous and not first:
            if last:
                state.phase = Phase.LIVE
            return Decision.ADD_PAGE
        state.phase = Phase.STALE
        return Decision.GAP
    if snapshot:
        if state.phase is Phase.COLLECTING and contiguous and not first:
            if last:
                state.phase = Phase.LIVE
            return Decision.ADD_PAGE
        if first or after_boundary:
            state.phase = Phase.LIVE if last else Phase.COLLECTING
            return Decision.REPLACE
        state.phase = Phase.STALE
        return Decision.GAP
    if state.phase is Phase.AWAIT_SNAPSHOT:
        return Decision.EARLY  # an old replay: the snapshot to come supersedes it
    if state.phase is Phase.COLLECTING:
        state.phase = Phase.STALE  # a snapshot must not be interleaved
        return Decision.GAP
    if state.phase is Phase.STALE:
        return Decision.STALE
    if not contiguous:
        state.phase = Phase.STALE
        return Decision.GAP
    return Decision.DELTA


# ==================================================================== store


class CacheLocationStore:
    """Fenced writes and the reads of ``cache_location`` (see module docstring)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    @staticmethod
    async def _fence(db: AsyncSession, node_id: str, session_id: str) -> bool:
        owner = await db.scalar(
            select(Node.node_id)
            .where(Node.node_id == node_id, Node.last_session_id == session_id)
            .with_for_update(read=True)
        )
        return owner is not None

    @staticmethod
    async def _upsert(db: AsyncSession, node_id: str, rows: Sequence[CacheRow]) -> None:
        for start in range(0, len(rows), _INSERT_BATCH):
            batch = rows[start : start + _INSERT_BATCH]
            stmt = pg_insert(CacheLocation).values(
                [
                    {"node_id": node_id, "kind": r.kind, "key": r.key, "size": r.size}
                    for r in batch
                ]
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["node_id", "kind", "key"],
                    set_={"size": stmt.excluded.size, "last_seen": func.now()},
                )
            )

    async def apply(
        self,
        node_id: str,
        session_id: str,
        *,
        replace: bool,
        added: Sequence[CacheRow],
        removed: Sequence[CacheRow],
    ) -> bool:
        """One report in one transaction; False = fenced (another session owns
        the node now), nothing written."""
        async with self._sessions() as db, db.begin():
            if not await self._fence(db, node_id, session_id):
                return False
            if replace:
                await db.execute(
                    delete(CacheLocation).where(CacheLocation.node_id == node_id)
                )
            elif removed:
                await db.execute(
                    delete(CacheLocation).where(
                        CacheLocation.node_id == node_id,
                        tuple_(CacheLocation.kind, CacheLocation.key).in_(
                            [(r.kind, r.key) for r in removed]
                        ),
                    )
                )
            if added:
                # Duplicates inside one page would make ON CONFLICT fail.
                unique = list({(r.kind, r.key): r for r in added}.values())
                await self._upsert(db, node_id, unique)
        return True

    async def holders(
        self, kind: str, key: str, *, among: Iterable[str]
    ) -> list[tuple[str, float]]:
        """``(node_id, last_seen epoch s)`` of the nodes in ``among`` holding
        ``kind``/``key`` - one index lookup (D100)."""
        candidates = list(among)
        if not candidates:
            return []
        async with self._sessions() as db:
            rows = await db.execute(
                select(CacheLocation.node_id, CacheLocation.last_seen).where(
                    CacheLocation.kind == kind,
                    CacheLocation.key == key,
                    CacheLocation.node_id.in_(candidates),
                )
            )
            return [(str(n), seen.timestamp()) for n, seen in rows.all()]

    async def summary(self, node_id: str) -> dict[str, tuple[int, int]]:
        """``kind -> (count, bytes)`` of one node."""
        async with self._sessions() as db:
            rows = await db.execute(
                select(
                    CacheLocation.kind,
                    func.count(),
                    func.coalesce(func.sum(CacheLocation.size), 0),
                )
                .where(CacheLocation.node_id == node_id)
                .group_by(CacheLocation.kind)
            )
            return {str(k): (int(c), int(b)) for k, c, b in rows.all()}


# ==================================================================== index


@dataclass(slots=True)
class CacheIndexStats:
    reports: int = 0
    applied: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    write_failures: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


class CacheIndex:
    """The report handler (registered on the session dispatch table)."""

    def __init__(self, store: CacheLocationStore) -> None:
        self.store = store
        self._states: dict[str, NodeCacheState] = {}
        self.stats = CacheIndexStats()

    def state(self, node_id: str) -> NodeCacheState | None:
        return self._states.get(node_id)

    def forget(self, node_id: str) -> None:
        self._states.pop(node_id, None)

    async def on_message(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        self.on_report(live, msg.cache_report)

    def on_report(self, live: LiveSession, report: agent_pb2.CacheReport) -> None:
        """Synchronous: decide, then enqueue the fenced write (never awaits)."""
        self.stats.reports += 1
        state = self._states.get(live.node_id)
        if state is None or state.session_id != live.session_id:
            state = self._states[live.node_id] = NodeCacheState(live.session_id)
        checked = validate_report(report)
        if isinstance(checked, str):
            state.phase = Phase.STALE
            if report.seq > state.last_seq:
                # Its seq is used up; what it ended is unknown.
                state.last_seq, state.boundary = report.seq, False
            self._drop(live, checked, report)
            return
        added, removed = checked
        decision = decide(
            state,
            seq=report.seq,
            snapshot=report.snapshot,
            last=report.snapshot_last,
            page=report.page if report.snapshot else None,
            page_missing=report.snapshot and not report.HasField("page"),
        )
        if decision in (Decision.REPLACE, Decision.ADD_PAGE, Decision.DELTA):
            self.stats.applied += 1
            live.writer.enqueue(
                "cache_report",
                WriteKind.CACHE,
                functools.partial(
                    self._write,
                    live.node_id,
                    live.session_id,
                    replace=decision is Decision.REPLACE,
                    added=added,
                    removed=removed,
                ),
            )
            if report.snapshot_last:
                log.info(
                    "controller.cache.snapshot_done",
                    node_id=live.node_id,
                    session_id=live.session_id,
                    seq=report.seq,
                )
            return
        self._drop(live, decision.value, report)

    def _drop(
        self, live: LiveSession, reason: str, report: agent_pb2.CacheReport
    ) -> None:
        self.stats.drop(reason)
        if reason in (Decision.DUPLICATE.value, Decision.EARLY.value):
            log.debug(
                "controller.cache.report_dropped",
                node_id=live.node_id,
                session_id=live.session_id,
                reason=reason,
                seq=report.seq,
            )
            return
        log.warning(
            "controller.cache.report_dropped",
            node_id=live.node_id,
            session_id=live.session_id,
            reason=reason,
            seq=report.seq,
        )

    async def _write(
        self,
        node_id: str,
        session_id: str,
        *,
        replace: bool,
        added: Sequence[CacheRow],
        removed: Sequence[CacheRow],
    ) -> None:
        try:
            written = await self.store.apply(
                node_id, session_id, replace=replace, added=added, removed=removed
            )
        except Exception:
            self.stats.write_failures += 1
            state = self._states.get(node_id)
            if state is not None and state.session_id == session_id:
                state.phase = Phase.STALE
            raise  # logged by the write chain (registry.write_failed)
        if not written:
            log.info(
                "controller.cache.write_fenced", node_id=node_id, session_id=session_id
            )
