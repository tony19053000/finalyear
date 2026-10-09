"""Node revocation (roadmap 0.7.7a, V0.7 G3, D138).

``meshforge-controller node revoke <id>``, ``POST /v1/nodes/{id}/revoke`` and
``compute node revoke <id>`` take a node out of the fabric:

* **The DB flag** (:meth:`RevocationStore.revoke_node`, one transaction): the
  node row goes ``revoked`` (sticky: no session write can turn it back - see
  :meth:`~.registry.Registry.set_node_state`), its current certificate serial
  joins ``revoked_cert`` and an ``audit_event`` row ``node_revoked`` names who
  did it. Idempotent: revoking a revoked node changes nothing.
* **The live kill** (:meth:`Revocations.sync`): every revoked node that still
  has a live or lingering session is cut at once - the stream ends
  ``PERMISSION_DENIED`` (``node_revoked``), the node goes ``REVOKED`` and its
  attempts take the V0.6 node-loss path (``LOST``, retried elsewhere). The
  REST path runs it right after the commit (measured well under one
  heartbeat); a revocation written by the controller CLI (another process) is
  found by the periodic sync every ``[controller.revocation] poll_interval_s``.
* **Every plane refuses the certificate**: the in-memory serial set
  (:attr:`Revocations.serials`) is what the session interceptor and the HTTPS
  API check on every call; the agents get it in every ``HelloAck`` and as a
  ``RevocationUpdate`` whenever it changes (re-sent on a missing Ack, D113)
  and their peer servers refuse those serials. A revoked node gets no new
  fetch, upload, peer or probe token: they are minted only for ONLINE nodes
  with a live session.

``superseded`` serials (a node re-enrolled and got a new certificate, the old
one still CA-valid) are in the same set (:func:`supersede`, written by the
enrolment transaction). There is no un-revoke: the admin makes a code with
``fabric invite --replace <node_id>`` and the laptop re-joins (new serial).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.ids import validate_node_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.liveness import NodeState
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.node import Node
from meshforge.controller.store.models.revocation import (
    RevocationReason,
    RevokedCert,
)

__all__ = [
    "MAX_SENT_SERIALS",
    "NodeNotFoundError",
    "RevocationError",
    "RevocationSnapshot",
    "RevocationStore",
    "Revocations",
    "RevokeOutcome",
    "supersede",
]

log = get_logger(__name__)

MAX_SENT_SERIALS: Final = 10_000
"""Most serials one ``RevocationUpdate`` / ``HelloAck`` carries (the newest;
expired certificates are never sent - the CA check refuses them anyway)."""


class RevocationError(MeshForgeError):
    """The database refused or lost a revocation read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class NodeNotFoundError(MeshForgeError):
    """No node with that id (or not a node id at all)."""

    default_failure_type = FailureType.NOT_FOUND
    exit_code = ExitCode.FAILURE


@dataclass(frozen=True, slots=True)
class RevokeOutcome:
    node_id: str
    serial: str | None
    """The certificate serial added to the revocation set (None: the node never
    enrolled - an ``insecure-dev`` or pre-0015 node)."""
    already: bool
    """The node was already revoked: nothing changed (no new audit row)."""
    previous_state: str
    session_killed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "state": NodeState.REVOKED.value,
            "serial": self.serial,
            "already_revoked": self.already,
            "previous_state": self.previous_state,
            "session_killed": self.session_killed,
        }


@dataclass(frozen=True, slots=True)
class RevocationSnapshot:
    nodes: frozenset[str]
    """Every node in state ``revoked``."""
    serials: frozenset[str]
    """Every revoked or superseded serial whose certificate has not expired."""
    newest: tuple[str, ...]
    """The same serials, newest first, at most :data:`MAX_SENT_SERIALS`."""


async def supersede(
    db: AsyncSession, *, node_id: str, serial: str, not_after: dt.datetime
) -> None:
    """In the caller's transaction: ``serial`` (a node's previous certificate,
    replaced by a re-enrolment) joins the revocation set as ``superseded``."""
    await db.execute(
        pg_insert(RevokedCert)
        .values(
            serial=serial,
            node_id=node_id,
            reason=RevocationReason.SUPERSEDED.value,
            not_after=not_after,
        )
        .on_conflict_do_nothing(index_elements=[RevokedCert.serial])
    )


def _hint(exc: BaseException) -> str:
    return f": {type(exc).__name__} (is the database running and migrated?)"


class RevocationStore:
    """The revocation rows (``node.state``, ``revoked_cert``, audit)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    async def revoke_node(
        self, node_id: str, *, actor: str, source: str | None = None
    ) -> RevokeOutcome:
        """See module docstring ("The DB flag"). Unknown node -> not_found."""
        try:
            validate_node_id(node_id)
        except MeshForgeError:
            raise NodeNotFoundError(
                f"{node_id[:64]!r} is not a node id (node_...)",
                details={"reason": "bad_id"},
            ) from None
        try:
            async with self._sessions() as db, db.begin():
                row = await db.scalar(
                    select(Node).where(Node.node_id == node_id).with_for_update()
                )
                if row is None:
                    raise NodeNotFoundError(
                        f"no node {node_id}", details={"reason": "unknown"}
                    )
                previous = row.state
                serial = row.cert_serial
                if previous == NodeState.REVOKED.value:
                    return RevokeOutcome(node_id, serial, True, previous)
                row.state = NodeState.REVOKED.value
                if serial is not None and row.cert_not_after is not None:
                    await db.execute(
                        pg_insert(RevokedCert)
                        .values(
                            serial=serial,
                            node_id=node_id,
                            reason=RevocationReason.REVOKED.value,
                            not_after=row.cert_not_after,
                        )
                        .on_conflict_do_update(
                            index_elements=[RevokedCert.serial],
                            set_={"reason": RevocationReason.REVOKED.value},
                        )
                    )
                await audit(
                    db,
                    AuditAction.NODE_REVOKED,
                    actor=actor,
                    node_id=node_id,
                    source=source,
                    details={"serial": serial, "previous_state": previous},
                )
        except SQLAlchemyError as exc:
            raise RevocationError(
                f"cannot revoke the node{_hint(exc)}",
                details={"cause": type(exc).__name__},
            ) from exc
        log.warning(
            "controller.node.revoked",
            node_id=node_id,
            actor=actor,
            serial=serial,
            previous_state=previous,
        )
        return RevokeOutcome(node_id, serial, False, previous)

    async def snapshot(self) -> RevocationSnapshot:
        """Revoked nodes and every unexpired revoked/superseded serial."""
        try:
            async with self._sessions() as db:
                nodes = (
                    await db.scalars(
                        select(Node.node_id).where(
                            Node.state == NodeState.REVOKED.value
                        )
                    )
                ).all()
                serials = (
                    await db.scalars(
                        select(RevokedCert.serial)
                        .where(RevokedCert.not_after > func.now())
                        .order_by(RevokedCert.revoked_at.desc(), RevokedCert.serial)
                    )
                ).all()
        except SQLAlchemyError as exc:
            raise RevocationError(
                f"cannot read the revocation set{_hint(exc)}",
                details={"cause": type(exc).__name__},
            ) from exc
        return RevocationSnapshot(
            nodes=frozenset(nodes),
            serials=frozenset(serials),
            newest=tuple(serials[:MAX_SENT_SERIALS]),
        )


class _Sessions(Protocol):
    """What :class:`Revocations` needs of the session manager."""

    def has_session(self, node_id: str) -> bool: ...

    async def revoke_node(self, node_id: str) -> bool: ...

    def broadcast(self, payload_name: str, payload: Any) -> int: ...


class Revocations:
    """The controller's in-memory revocation state and its enforcement (see
    module docstring). One per runtime; event-loop owned."""

    def __init__(self, store: RevocationStore, sessions: _Sessions) -> None:
        self._store = store
        self._sessions = sessions
        self._serials: frozenset[str] = frozenset()
        self._newest: tuple[str, ...] = ()
        self._nodes: frozenset[str] = frozenset()
        self._lock = asyncio.Lock()
        self.kills = 0
        self.broadcasts = 0
        self.sync_failures = 0

    # ------------------------------------------------------------ read side

    @property
    def serials(self) -> frozenset[str]:
        """Every refused serial (the interceptor's / HTTPS policy's callable)."""
        return self._serials

    def revoked_serials(self) -> frozenset[str]:
        return self._serials

    def sent_serials(self) -> tuple[str, ...]:
        """What a ``HelloAck`` / ``RevocationUpdate`` carries (newest first)."""
        return self._newest

    def is_revoked(self, node_id: str) -> bool:
        return node_id in self._nodes

    def update_message(self) -> agent_pb2.RevocationUpdate:
        return agent_pb2.RevocationUpdate(serials=list(self._newest))

    # ------------------------------------------------------------ actions

    async def revoke(
        self, node_id: str, *, actor: str, source: str | None = None
    ) -> RevokeOutcome:
        """Revoke ``node_id`` and enforce it now (the REST path)."""
        outcome = await self._store.revoke_node(node_id, actor=actor, source=source)
        killed = await self.sync()
        return RevokeOutcome(
            outcome.node_id,
            outcome.serial,
            outcome.already,
            outcome.previous_state,
            session_killed=node_id in killed,
        )

    async def sync(self) -> frozenset[str]:
        """Read the set, cut every revoked node's session, tell the agents if
        the serials changed. Returns the nodes whose session was cut."""
        async with self._lock:
            snap = await self._store.snapshot()
            changed = snap.serials != self._serials or snap.newest != self._newest
            # The serials first: a reconnect is refused at its handshake call
            # before the old stream is even cut.
            self._serials, self._newest, self._nodes = (
                snap.serials,
                snap.newest,
                snap.nodes,
            )
            killed: set[str] = set()
            for node_id in sorted(snap.nodes):
                if self._sessions.has_session(node_id) and (
                    await self._sessions.revoke_node(node_id)
                ):
                    killed.add(node_id)
                    self.kills += 1
            if changed:
                sent = self._sessions.broadcast(
                    "revocation_update", self.update_message()
                )
                self.broadcasts += 1
                log.info(
                    "controller.revocation.updated",
                    serials=len(snap.serials),
                    sent_to=sent,
                    truncated=len(snap.serials) > len(snap.newest),
                )
            return frozenset(killed)

    async def run(self, interval_s: float) -> None:
        """Sync every ``interval_s`` (a revocation the controller CLI wrote in
        another process, a re-enrolment's superseded serial). A database error
        keeps the last set (logged); never raises but CancelledError."""
        while True:
            try:
                await self.sync()
            except MeshForgeError as exc:
                self.sync_failures += 1
                log.warning(
                    "controller.revocation.sync_failed",
                    failure_type=exc.failure_type.value,
                    cause=exc.details.get("cause"),
                )
            await asyncio.sleep(interval_s)
