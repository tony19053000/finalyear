"""Node registry repository: the only code that reads/writes ``node`` / ``node_session``.

Roadmap 0.3.6 / 0.3.10. The gRPC session layer (:mod:`meshforge.controller.session`)
never sees ORM objects: every method returns small frozen dataclasses
(:class:`NodeRecord`, :class:`SessionRecord`) or nothing. All writes go through
SQLAlchemy so ``updated_at`` is maintained by ``onupdate`` (0.1.7 carry-over).

Wall-clock timestamps in this module are for the database only; liveness is
measured on the event loop's monotonic clock in the session layer.
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import (
    CursorResult,
    Result,
    case,
    cast,
    delete,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.clock_offset import ClockOffset, round_up
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.liveness import NodeState
from meshforge.controller.resources import SnapshotRow
from meshforge.controller.store.base import UInt64
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.node import Node, NodeSession, SessionEndReason
from meshforge.controller.store.models.resource import ResourceSnapshot

__all__ = [
    "NodeRecord",
    "Registry",
    "RegistryError",
    "ScoresWrite",
    "SessionEndReason",
    "SessionRecord",
    "SnapshotRecord",
    "UnknownNodeError",
    "UnknownSessionError",
]

log = get_logger(__name__)


class RegistryError(MeshForgeError):
    """The database refused or lost a registry read/write (typed, retryable).

    Wraps SQLAlchemy/asyncpg/OS errors so callers in the session layer catch one
    :class:`~meshforge.common.errors.MeshForgeError` family and never a raw driver
    exception. ``details["operation"]`` names the registry method.
    """

    default_failure_type = FailureType.NETWORK_ERROR


class UnknownNodeError(MeshForgeError):
    """``node_id`` has no ``node`` row."""

    default_failure_type = FailureType.NOT_FOUND


class UnknownSessionError(MeshForgeError):
    """``session_id`` has no ``node_session`` row."""

    default_failure_type = FailureType.NOT_FOUND


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """Read model of one ``node`` row."""

    node_id: str
    hostname: str
    os: str
    platform: str
    hardware: Mapping[str, Any]
    state: NodeState
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    last_session_id: str | None
    session_epoch: int
    hardware_scores: Mapping[str, Any] | None = None
    """The stored score document (``controller/scores.py``); None = not measured."""
    cpu_slots: int | None = None
    """``Hello.max_cpu_jobs`` (V0.6 D105); None = the agent did not say."""
    gpu_slots: int | None = None
    """``Hello.max_gpu_jobs`` (V0.6 D105); None = the agent did not say."""
    cert_serial: str | None = None
    """V0.7 G2: the node's current certificate (None = never enrolled)."""
    cert_fingerprint: str | None = None
    cert_not_after: dt.datetime | None = None
    public_key_fingerprint: str | None = None
    pending_cert_serial: str | None = None
    """V0.7 G4 (D140): a renewed certificate not yet in use; the node's first
    Hello with it makes it current (:mod:`~.enrollment.renewal`)."""
    pending_cert_not_after: dt.datetime | None = None
    display_name: str = ""
    """V0.17 G3 (Q27): ``Hello.display_name``; empty = none."""


class PeerCertFacts(Protocol):
    """What admission knows about a session's verified client certificate
    (:class:`~meshforge.controller.session.peer_cert.PeerCert`)."""

    @property
    def serial(self) -> str: ...
    @property
    def fingerprint(self) -> str: ...
    @property
    def not_after(self) -> dt.datetime: ...
    @property
    def public_key_fingerprint(self) -> str: ...


def _cert_columns(cert: PeerCertFacts | None, *, coalesce: bool) -> dict[str, Any]:
    """The four certificate columns (all set or none, migration 0015)."""
    if cert is None:
        return {}
    values: dict[str, Any] = {
        "cert_serial": cert.serial,
        "cert_fingerprint": cert.fingerprint,
        "cert_not_after": cert.not_after,
        "public_key_fingerprint": cert.public_key_fingerprint,
    }
    if not coalesce:
        return values
    # All four move together: keep the stored ones when a serial is recorded.
    keep = Node.cert_serial.is_not(None)
    return {
        name: case(
            (keep, getattr(Node, name)), else_=literal(value, getattr(Node, name).type)
        )
        for name, value in values.items()
    }


class ScoresWrite(StrEnum):
    """Outcome of :meth:`Registry.set_hardware_scores`."""

    WRITTEN = "written"
    FENCED = "fenced"
    """A newer session owns the node (D20): the write is stale."""
    OLDER = "older"
    """The stored run is newer than this one (a replay, or a reordered report)."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Read model of one ``node_session`` row."""

    session_id: str
    node_id: str
    session_epoch: int
    started_at: dt.datetime
    ended_at: dt.datetime | None
    end_reason: SessionEndReason | None
    peer_address: str
    agent_version: str
    protocol_version: int
    last_rtt_ms: float | None
    rtt_ewma_ms: float | None
    clock_offset_ms: float | None = None
    """V0.9 G1 (D213): agent wall clock minus controller wall clock (ms)."""
    clock_offset_bound_ms: float | None = None
    clock_consistent: bool | None = None
    clock_exchanges: int | None = None
    clock_measured_at: dt.datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """Read model of one ``resource_snapshot`` row (G5's REST will serve these)."""

    node_id: str
    bucket_start: dt.datetime
    samples: int
    cpu_percent_mean: float
    cpu_percent_max: float
    ram_used_bytes_mean: int
    ram_used_bytes_max: int
    ram_total_bytes: int
    gpu: Mapping[str, Any]
    disks: Mapping[str, Any]


def _snapshot_record(row: ResourceSnapshot) -> SnapshotRecord:
    return SnapshotRecord(
        node_id=row.node_id,
        bucket_start=row.bucket_start,
        samples=row.samples,
        cpu_percent_mean=row.cpu_percent_mean,
        cpu_percent_max=row.cpu_percent_max,
        ram_used_bytes_mean=row.ram_used_bytes_mean,
        ram_used_bytes_max=row.ram_used_bytes_max,
        ram_total_bytes=row.ram_total_bytes,
        gpu=dict(row.gpu),
        disks=dict(row.disks),
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _typed[**P, R](
    method: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """Translate driver errors into :class:`RegistryError`; MeshForge errors pass."""

    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await method(*args, **kwargs)
        except MeshForgeError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise RegistryError(
                f"registry.{method.__name__} failed: {type(exc).__name__}",
                details={"operation": method.__name__, "cause": type(exc).__name__},
            ) from exc

    return wrapper


def _rowcount(result: Result[Any]) -> int:
    """Rows touched by a DML statement (the async session types it as ``Result``)."""
    if not isinstance(result, CursorResult):  # pragma: no cover - dialect contract
        raise RegistryError("DML statement returned no cursor result")
    return int(result.rowcount)


def _node_record(row: Node) -> NodeRecord:
    return NodeRecord(
        node_id=row.node_id,
        hostname=row.hostname,
        os=row.os,
        platform=row.platform,
        hardware=dict(row.hardware),
        state=NodeState(row.state),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        last_session_id=row.last_session_id,
        session_epoch=row.session_epoch,
        hardware_scores=(
            dict(row.hardware_scores) if row.hardware_scores is not None else None
        ),
        cpu_slots=row.cpu_slots,
        gpu_slots=row.gpu_slots,
        cert_serial=row.cert_serial,
        cert_fingerprint=row.cert_fingerprint,
        cert_not_after=row.cert_not_after,
        public_key_fingerprint=row.public_key_fingerprint,
        pending_cert_serial=row.pending_cert_serial,
        pending_cert_not_after=row.pending_cert_not_after,
        display_name=row.display_name or "",
    )


def _session_record(row: NodeSession) -> SessionRecord:
    return SessionRecord(
        session_id=row.session_id,
        node_id=row.node_id,
        session_epoch=row.session_epoch,
        started_at=row.started_at,
        ended_at=row.ended_at,
        end_reason=SessionEndReason(row.end_reason) if row.end_reason else None,
        peer_address=row.peer_address,
        agent_version=row.agent_version,
        protocol_version=row.protocol_version,
        last_rtt_ms=row.last_rtt_ms,
        rtt_ewma_ms=row.rtt_ewma_ms,
        clock_offset_ms=row.clock_offset_ms,
        clock_offset_bound_ms=row.clock_offset_bound_ms,
        clock_consistent=row.clock_consistent,
        clock_exchanges=row.clock_exchanges,
        clock_measured_at=row.clock_measured_at,
    )


def _parse_measured_at(raw: object) -> dt.datetime | None:
    """A stored ``measured_at`` (ISO-8601; ``Z`` or an offset) as an aware
    time, or None when it is missing or not a time. A value without an offset
    is read as UTC."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


class Registry:
    """Async repository over the node registry tables."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    # --------------------------------------------------------------- liveness

    @_typed
    async def ping(self) -> None:
        """Cheapest possible proof that the database answers right now.

        ``SELECT 1`` on a pooled connection: no table, no lock, no write. Callers
        use it to tell "the database is down" apart from "the database is up and
        rejecting this particular statement" - see
        :meth:`meshforge.controller.snapshots.SnapshotFlusher._isolate`. Raises
        :class:`RegistryError` exactly like every other method when the database
        is unreachable.
        """
        async with self._sessions() as db:
            await db.execute(select(literal(1)))

    # ----------------------------------------------------------------- nodes

    @_typed
    async def upsert_node_on_hello(
        self,
        *,
        node_id: str,
        hostname: str,
        os: str,
        platform: str,
        hardware: Mapping[str, Any],
        session_epoch: int,
        session_id: str,
        state: NodeState,
        hardware_scores: Mapping[str, Any] | None = None,
        cpu_slots: int | None = None,
        gpu_slots: int | None = None,
        cert: PeerCertFacts | None = None,
        display_name: str = "",
    ) -> NodeRecord:
        """Insert the node on its first Hello, else refresh it; sets ``state``.

        ``display_name`` (V0.17 G3, Q27): the owner's name for the laptop,
        always written like ``hostname`` (empty clears it).

        ``cert`` (V0.7 G3): the session's verified client certificate. It is
        recorded only on a node with **no** certificate on record (enrolled
        before migration 0015 recorded certificates, or by a fixture without
        the join flow): ``COALESCE`` keeps an existing record, which admission
        has already checked equals the presented serial.

        ``cpu_slots`` / ``gpu_slots`` (V0.6 D105) are the Hello's validated
        ``max_cpu_jobs`` / ``max_gpu_jobs``, always written like the scores:
        None = this agent did not say (the planner's defaults apply).

        ``hardware_scores`` is the validated score document the Hello carried, or
        ``None``: **always written**, so a Hello without scores clears the column
        (D37 - "not measured for this hardware", shown as pending, never the
        previous hardware's numbers). A Hello needs no ordering guard: admission is
        serialised and moves ``last_session_id`` in the same statement.

        ``first_seen_at`` is preserved; ``last_seen_at`` and ``last_session_id`` move
        to now / the new session. The caller decides ``state`` (it knows whether
        the Hello was an enrollment, a reconnect or a superseding stream).

        ``session_epoch`` is the high-water mark of the node (ADR-0002 Updates §1)
        and **never moves backwards**: the UPDATE itself writes
        ``GREATEST(session_epoch, :new)``, so the clamp holds in the database even
        for two writers racing without the session manager's lock (there is no
        read-modify-write to interleave). The session layer rejects a
        lower-or-equal epoch before calling this; the clamp is the second line of
        defence and is logged when it engaged (RETURNING shows the stored value).
        """
        now = _utcnow()
        scores = dict(hardware_scores) if hardware_scores is not None else None
        async with self._sessions() as db, db.begin():
            # UPDATE first (the common case: a known node reconnecting); the clamp
            # is inside the statement. RETURNING gives the row as stored.
            result = await db.execute(
                update(Node)
                .where(Node.node_id == node_id)
                .values(
                    hostname=hostname,
                    display_name=display_name,
                    os=os,
                    platform=platform,
                    hardware=dict(hardware),
                    hardware_scores=scores,
                    cpu_slots=cpu_slots,
                    gpu_slots=gpu_slots,
                    # V0.7 G3 (D138): a revocation committed while this Hello
                    # was admitted stays; the revocation sync cuts the session.
                    state=case(
                        (Node.state == NodeState.REVOKED.value, Node.state),
                        else_=literal(state.value),
                    ),
                    last_seen_at=now,
                    last_session_id=session_id,
                    **_cert_columns(cert, coalesce=True),
                    # The literal must carry the column's type: an untyped Python
                    # int binds as BIGINT and asyncpg refuses epochs >= 2^63.
                    session_epoch=func.greatest(
                        Node.session_epoch, literal(session_epoch, UInt64)
                    ),
                )
                .returning(Node)
                .execution_options(synchronize_session=False)
            )
            row = result.scalars().one_or_none()
            if row is None:
                row = Node(
                    node_id=node_id,
                    hostname=hostname,
                    display_name=display_name,
                    os=os,
                    platform=platform,
                    hardware=dict(hardware),
                    hardware_scores=scores,
                    cpu_slots=cpu_slots,
                    gpu_slots=gpu_slots,
                    state=state.value,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_session_id=session_id,
                    session_epoch=session_epoch,
                    **_cert_columns(cert, coalesce=False),
                )
                db.add(row)
                await db.flush()
            elif row.session_epoch != session_epoch:
                log.warning(
                    "registry.epoch_not_lowered",
                    node_id=node_id,
                    session_id=session_id,
                    hello_epoch=session_epoch,
                    stored_epoch=row.session_epoch,
                )
            return _node_record(row)

    @_typed
    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        """Persist a liveness transition **fenced on the session** (D20).

        ``UPDATE node SET state = :state WHERE node_id = :n AND last_session_id =
        :s``. Returns True when the row was written, False when 0 rows matched:
        a newer session has been admitted for the node since this write was
        queued (admission updates the row directly, not on the old session's
        write chain), so the write is stale and must not land - without the
        fence a backlogged OFFLINE could overwrite a reconnect's ONLINE and a
        connected node would read OFFLINE indefinitely (heartbeats are
        self-loops; nothing rewrites it). A fenced write is logged
        ``registry.state_write_fenced`` at info level and is never an error. A
        ``revoked`` row is never overwritten (V0.7 G3, D138).

        Every session-originated state write goes through here; the only other
        state writers are admission (``upsert_node_on_hello``, which *moves*
        ``last_session_id``) and the start-up sweep (no session is live then).
        """
        fence = [Node.node_id == node_id, Node.last_session_id == session_id]
        if state is not NodeState.REVOKED:
            # V0.7 G3 (D138): revoked is sticky - only a re-enrolment (``invite
            # --replace``) brings a revoked node back, never a session write.
            fence.append(Node.state != NodeState.REVOKED.value)
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(Node).where(*fence).values(state=state.value)
            )
            written = _rowcount(result) == 1
        if not written:
            log.info(
                "registry.state_write_fenced",
                node_id=node_id,
                session_id=session_id,
                state=state.value,
            )
        return written

    @_typed
    async def expire_unbound_scores(self, cutoff: dt.datetime) -> int:
        """Clear score documents stored before V0.7 G5 (no ``node_id``
        binding, D145b) measured before ``cutoff``; returns how many.

        D161 N3: such a document is accepted (``bound_to``) only until the
        node's next Hello re-binds or clears it - a node that never comes back
        would keep it forever.

        D162 (T-7): ``measured_at`` is read as text and parsed here, never
        cast inside the statement - one unparseable legacy value used to fail
        the whole UPDATE, so nothing ever expired. A document whose
        ``measured_at`` is missing or cannot be parsed has no provable age
        and is cleared too (fail closed; the node measures again). Each
        document is cleared by its own guarded UPDATE (still unbound, same
        ``measured_at`` text), so a Hello that writes a bound document at the
        same moment is never undone.
        """
        unbound = (
            Node.hardware_scores.is_not(None),
            Node.hardware_scores["node_id"].astext.is_(None),
        )
        stored_at = Node.hardware_scores["measured_at"].astext
        count = undated = 0
        async with self._sessions() as db, db.begin():
            rows = (
                await db.execute(select(Node.node_id, stored_at).where(*unbound))
            ).all()
            for node_id, raw in rows:
                measured = _parse_measured_at(raw)
                if measured is not None and measured >= cutoff:
                    continue
                result = await db.execute(
                    update(Node)
                    .where(
                        Node.node_id == node_id,
                        *unbound,
                        stored_at.is_not_distinct_from(raw),
                    )
                    .values(hardware_scores=None)
                    .execution_options(synchronize_session=False)
                )
                cleared = _rowcount(result)
                count += cleared
                if measured is None:
                    undated += cleared
        if count:
            log.info("registry.unbound_scores_expired", count=count, undated=undated)
        return count

    @_typed
    async def set_hardware_scores(
        self,
        node_id: str,
        document: Mapping[str, Any],
        *,
        session_id: str,
        measured_at: dt.datetime,
    ) -> ScoresWrite:
        """Store a mid-session score run, **fenced** and **ordered** (D38).

        One statement: ``UPDATE node SET hardware_scores = :doc WHERE node_id = :n
        AND last_session_id = :s AND (hardware_scores IS NULL OR
        (hardware_scores->>'measured_at')::timestamptz <= :measured_at)``.

        * The session fence is D20's rule: a report queued on a superseded
          session's write chain must not overwrite what the newer session's Hello
          stored (it may be the previous hardware's run).
        * The ``measured_at`` guard makes the write idempotent under replay and
          reordering: an equal run rewrites the same document, an older one is
          ignored. ``measured_at`` is stored by ``controller/scores.py`` as
          ISO-8601 with an offset, which PostgreSQL casts exactly.

        When 0 rows match, a second read tells ``FENCED`` from ``OLDER`` for the
        log; neither is an error.
        """
        stored_at = cast(
            Node.hardware_scores["measured_at"].astext, TIMESTAMP(timezone=True)
        )
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(Node)
                .where(
                    Node.node_id == node_id,
                    Node.last_session_id == session_id,
                    or_(Node.hardware_scores.is_(None), stored_at <= measured_at),
                )
                .values(hardware_scores=dict(document))
                .execution_options(synchronize_session=False)
            )
            if _rowcount(result) == 1:
                return ScoresWrite.WRITTEN
            owner = await db.scalar(
                select(Node.last_session_id).where(Node.node_id == node_id)
            )
        outcome = ScoresWrite.FENCED if owner != session_id else ScoresWrite.OLDER
        log.info(
            "registry.scores_write_skipped",
            node_id=node_id,
            session_id=session_id,
            outcome=outcome.value,
            run_id=document.get("run_id"),
        )
        return outcome

    @_typed
    async def touch_node(self, node_id: str) -> None:
        """Move ``last_seen_at`` to now.

        Not called per heartbeat: the session manager *coalesces* heartbeats to
        one call per ``heartbeat_interval_s`` per node and runs it on the
        session's ordered write chain (``session/writer.py``), so the value is the
        wall time the write executed - at most one interval plus queueing after
        the heartbeat it stands for. Liveness is decided in memory; this column
        is a record for the dashboard and for a controller restart.

        **Deliberately not fenced** on ``last_session_id`` (unlike
        :meth:`set_node_state`, D20): a late touch from a superseded session
        writes the wall time it *executed*, which can be a little later than that
        session's last heartbeat - but the node is being heard from through the
        newer session anyway, whose own touches follow within one heartbeat
        interval, and nothing decides liveness from this column. A stale *state*
        write is a lie nothing corrects; a late ``last_seen_at`` is at worst a few
        seconds optimistic in a display field. Within one session the chain
        orders every touch before that session's final state write.
        """
        async with self._sessions() as db, db.begin():
            await db.execute(
                update(Node)
                .where(Node.node_id == node_id)
                .values(last_seen_at=_utcnow())
            )

    @_typed
    async def mark_live_nodes_offline(self) -> int:
        """Controller start-up: every ONLINE/SUSPECT/DRAINING node lost its lease.

        The previous controller process is gone, so no lease can be alive; the
        registry must not claim ONLINE for a node nobody is hearing from. Walks the
        §12.3 arrows (LEASE_MISSED, LEASE_EXPIRED) conceptually; persisted as one
        UPDATE. Returns how many rows changed.
        """
        live_states = [
            NodeState.ONLINE.value,
            NodeState.SUSPECT.value,
            NodeState.DRAINING.value,
        ]
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(Node)
                .where(Node.state.in_(live_states))
                .values(state=NodeState.OFFLINE.value)
            )
            count = _rowcount(result)
        if count:
            log.info("registry.nodes_marked_offline", count=count)
        return count

    @_typed
    async def get_node(self, node_id: str) -> NodeRecord | None:
        async with self._sessions() as db:
            row = await db.get(Node, node_id)
            return None if row is None else _node_record(row)

    @_typed
    async def list_nodes(self, *, limit: int | None = None) -> list[NodeRecord]:
        """Every node, ordered by ``node_id``.

        ``limit`` is applied in SQL (``LIMIT``), not by slicing the result, so the
        bound the API advertises is a real bound on the rows read. ``node_id`` is
        the primary key, so the window is deterministic without a tiebreaker.
        """
        stmt = select(Node).order_by(Node.node_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sessions() as db:
            rows = await db.scalars(stmt)
            return [_node_record(r) for r in rows]

    # -------------------------------------------------------------- sessions

    @_typed
    async def open_session(
        self,
        *,
        session_id: str,
        node_id: str,
        session_epoch: int,
        peer_address: str,
        agent_version: str,
        protocol_version: int,
    ) -> SessionRecord:
        """Insert an open ``node_session`` row (``ended_at IS NULL``)."""
        async with self._sessions() as db, db.begin():
            row = NodeSession(
                session_id=session_id,
                node_id=node_id,
                session_epoch=session_epoch,
                started_at=_utcnow(),
                peer_address=peer_address,
                agent_version=agent_version,
                protocol_version=protocol_version,
            )
            db.add(row)
            await db.flush()
            return _session_record(row)

    @_typed
    async def close_session(
        self, session_id: str, end_reason: SessionEndReason
    ) -> bool:
        """Set ``ended_at``/``end_reason`` on an *open* session.

        Returns False when the session was already closed (first close wins, so a
        race between two closers keeps the first reason). Raises
        :class:`UnknownSessionError` for an unknown id.
        """
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(NodeSession)
                .where(
                    NodeSession.session_id == session_id,
                    NodeSession.ended_at.is_(None),
                )
                .values(ended_at=_utcnow(), end_reason=end_reason.value)
            )
            if _rowcount(result) == 1:
                return True
            exists = await db.get(NodeSession, session_id)
            if exists is None:
                raise UnknownSessionError(
                    f"no session {session_id}", details={"session_id": session_id}
                )
            return False

    @_typed
    async def sweep_open_sessions(self, end_reason: SessionEndReason) -> int:
        """Close every session with ``ended_at IS NULL``; returns how many.

        Called once at controller start-up with ``CONTROLLER_RESTART``: any open
        row belongs to a previous process that did not shut down gracefully.
        """
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(NodeSession)
                .where(NodeSession.ended_at.is_(None))
                .values(ended_at=_utcnow(), end_reason=end_reason.value)
            )
            count = _rowcount(result)
        if count:
            log.info(
                "registry.sessions_swept", count=count, end_reason=end_reason.value
            )
        return count

    @_typed
    async def record_rtt(
        self,
        session_id: str,
        *,
        last_ms: float,
        ewma_ms: float,
        clock: ClockOffset | None = None,
    ) -> None:
        """Store the latest RTT sample and its EWMA on the session row, and
        the node's clock-offset estimate from the same Pong (V0.9 G1, D213)."""
        values: dict[str, Any] = {"last_rtt_ms": last_ms, "rtt_ewma_ms": ewma_ms}
        if clock is not None:
            values.update(
                clock_offset_ms=clock.offset_ms,
                clock_offset_bound_ms=clock.bound_ms,
                clock_consistent=clock.consistent,
                clock_exchanges=clock.exchanges,
                clock_measured_at=func.clock_timestamp(),
            )
        async with self._sessions() as db, db.begin():
            await db.execute(
                update(NodeSession)
                .where(NodeSession.session_id == session_id)
                .values(**values)
            )

    @_typed
    async def record_clock_alert(
        self,
        node_id: str,
        *,
        offset_ms: float,
        bound_ms: float,
        consistent: bool,
        reasons: Sequence[str],
    ) -> int:
        """One ``clock_uncertain`` audit row (an operator alert, D180/D213);
        its id, which the live ``alert`` event carries."""
        async with self._sessions() as db, db.begin():
            row = await audit(
                db,
                AuditAction.CLOCK_UNCERTAIN,
                actor="controller",
                node_id=node_id,
                details={
                    "offset_ms": round(offset_ms, 3),
                    "bound_ms": round_up(bound_ms, 3),
                    "consistent": consistent,
                    "reasons": list(reasons),
                },
            )
            await db.flush()
            return int(row.id)

    @_typed
    async def get_session(self, session_id: str) -> SessionRecord | None:
        async with self._sessions() as db:
            row = await db.get(NodeSession, session_id)
            return None if row is None else _session_record(row)

    @_typed
    async def list_sessions(
        self, node_id: str | None = None, *, limit: int | None = None
    ) -> list[SessionRecord]:
        """Sessions, oldest first.

        ``limit`` returns the ``limit`` **most recent** sessions instead (still
        oldest first in the result), so a caller that wants "the last few" does not
        read a table that grows by one row per reconnect. Without it the behaviour
        is unchanged: every row, oldest first.
        """
        stmt = select(NodeSession)
        if node_id is not None:
            stmt = stmt.where(NodeSession.node_id == node_id)
        if limit is not None:
            # ``session_id`` breaks the tie: two sessions can share a
            # ``started_at`` (same transaction clock, or a coarse timestamp), and
            # ``started_at`` alone leaves which of them falls inside the window to
            # the database. The reversal below therefore also stays stable.
            stmt = stmt.order_by(
                NodeSession.started_at.desc(), NodeSession.session_id.desc()
            ).limit(limit)
        else:
            stmt = stmt.order_by(NodeSession.started_at, NodeSession.session_id)
        async with self._sessions() as db:
            rows = list(await db.scalars(stmt))
        records = [_session_record(r) for r in rows]
        return records[::-1] if limit is not None else records

    # ------------------------------------------------------------- snapshots

    @_typed
    async def upsert_resource_snapshots(self, rows: Sequence[SnapshotRow]) -> int:
        """Write closed downsample buckets; returns how many rows were sent.

        One batched ``INSERT ... ON CONFLICT (node_id, bucket_start) DO UPDATE``, so
        re-flushing a bucket the controller already wrote (a retry after a database
        hiccup) updates the row instead of duplicating it - the table's primary key
        *is* the bucket (roadmap 0.3.11).

        The update is **guarded by ``samples``** (NB-3): after a controller restart a
        replayed report can re-accumulate the same bucket from a partial sample set,
        and an unconditional overwrite would replace a complete 30-sample row with a
        2-sample one. A row is only replaced by one that saw at least as many
        samples, so the history only ever gets better; the row count returned counts
        rows *sent*, not rows changed.

        A row whose node has no ``node`` row is skipped by the foreign key, so the
        statement is ordered after the node exists in practice; the caller treats a
        failure as retryable.
        """
        if not rows:
            return 0
        values = [
            {
                "node_id": row.node_id,
                "bucket_start": row.bucket_start,
                "samples": row.samples,
                "cpu_percent_mean": row.cpu_percent_mean,
                "cpu_percent_max": row.cpu_percent_max,
                "ram_used_bytes_mean": row.ram_used_bytes_mean,
                "ram_used_bytes_max": row.ram_used_bytes_max,
                "ram_total_bytes": row.ram_total_bytes,
                "gpu": row.gpu,
                "disks": row.disks,
            }
            for row in rows
        ]
        statement = pg_insert(ResourceSnapshot).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[ResourceSnapshot.node_id, ResourceSnapshot.bucket_start],
            set_={
                "samples": statement.excluded.samples,
                "cpu_percent_mean": statement.excluded.cpu_percent_mean,
                "cpu_percent_max": statement.excluded.cpu_percent_max,
                "ram_used_bytes_mean": statement.excluded.ram_used_bytes_mean,
                "ram_used_bytes_max": statement.excluded.ram_used_bytes_max,
                "ram_total_bytes": statement.excluded.ram_total_bytes,
                "gpu": statement.excluded.gpu,
                "disks": statement.excluded.disks,
                "updated_at": func.now(),
            },
            # ``ResourceSnapshot.samples`` here is the *existing* row; the guard
            # is emitted as the ON CONFLICT ... WHERE clause.
            where=ResourceSnapshot.samples <= statement.excluded.samples,
        )
        async with self._sessions() as db, db.begin():
            await db.execute(statement)
        return len(values)

    @_typed
    async def delete_snapshots_before(self, cutoff: dt.datetime) -> int:
        """Retention sweep: drop buckets that started before ``cutoff``."""
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                delete(ResourceSnapshot).where(ResourceSnapshot.bucket_start < cutoff)
            )
            count = _rowcount(result)
        if count:
            log.info(
                "registry.snapshots_deleted", count=count, cutoff=cutoff.isoformat()
            )
        return count

    @_typed
    async def list_snapshots(
        self,
        node_id: str,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int | None = None,
    ) -> list[SnapshotRecord]:
        """One node's history, oldest first. ``since`` inclusive, ``until`` exclusive."""
        stmt = (
            select(ResourceSnapshot)
            .where(ResourceSnapshot.node_id == node_id)
            .order_by(ResourceSnapshot.bucket_start)
        )
        if since is not None:
            stmt = stmt.where(ResourceSnapshot.bucket_start >= since)
        if until is not None:
            stmt = stmt.where(ResourceSnapshot.bucket_start < until)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sessions() as db:
            rows = await db.scalars(stmt)
            return [_snapshot_record(r) for r in rows]
