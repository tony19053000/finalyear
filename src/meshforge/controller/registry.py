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
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import CursorResult, Result, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.liveness import NodeState
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.node import Node, NodeSession, SessionEndReason

__all__ = [
    "NodeRecord",
    "Registry",
    "RegistryError",
    "SessionEndReason",
    "SessionRecord",
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

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


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
    )


class Registry:
    """Async repository over the node registry tables."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

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
    ) -> NodeRecord:
        """Insert the node on its first Hello, else refresh it; sets ``state``.

        ``first_seen_at`` is preserved; ``last_seen_at`` and ``last_session_id`` move
        to now / the new session. The caller decides ``state`` (it knows whether
        the Hello was an enrollment, a reconnect or a superseding stream).
        """
        now = _utcnow()
        async with self._sessions() as db, db.begin():
            row = await db.get(Node, node_id)
            if row is None:
                row = Node(
                    node_id=node_id,
                    hostname=hostname,
                    os=os,
                    platform=platform,
                    hardware=dict(hardware),
                    state=state.value,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_session_id=session_id,
                    session_epoch=session_epoch,
                )
                db.add(row)
            else:
                row.hostname = hostname
                row.os = os
                row.platform = platform
                row.hardware = dict(hardware)
                row.state = state.value
                row.last_seen_at = now
                row.last_session_id = session_id
                row.session_epoch = session_epoch
            await db.flush()
            return _node_record(row)

    @_typed
    async def set_node_state(self, node_id: str, state: NodeState) -> None:
        """Persist a liveness transition. Raises :class:`UnknownNodeError`."""
        async with self._sessions() as db, db.begin():
            result = await db.execute(
                update(Node).where(Node.node_id == node_id).values(state=state.value)
            )
            if _rowcount(result) != 1:
                raise UnknownNodeError(
                    f"no node {node_id}", details={"node_id": node_id}
                )

    @_typed
    async def touch_node(self, node_id: str) -> None:
        """Move ``last_seen_at`` to now (called on every heartbeat)."""
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
    async def list_nodes(self) -> list[NodeRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(select(Node).order_by(Node.node_id))
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
        self, session_id: str, *, last_ms: float, ewma_ms: float
    ) -> None:
        """Store the latest RTT sample and its EWMA on the session row."""
        async with self._sessions() as db, db.begin():
            await db.execute(
                update(NodeSession)
                .where(NodeSession.session_id == session_id)
                .values(last_rtt_ms=last_ms, rtt_ewma_ms=ewma_ms)
            )

    @_typed
    async def get_session(self, session_id: str) -> SessionRecord | None:
        async with self._sessions() as db:
            row = await db.get(NodeSession, session_id)
            return None if row is None else _session_record(row)

    @_typed
    async def list_sessions(self, node_id: str | None = None) -> list[SessionRecord]:
        stmt = select(NodeSession).order_by(NodeSession.started_at)
        if node_id is not None:
            stmt = stmt.where(NodeSession.node_id == node_id)
        async with self._sessions() as db:
            rows = await db.scalars(stmt)
            return [_session_record(r) for r in rows]
