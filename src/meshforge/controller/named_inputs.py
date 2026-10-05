"""Named, versioned inputs: datasets and models registered once (V0.5 G5, D92).

``compute data push <name> <dir> [--pin]`` uploads the folder as a tree (the
same have/need upload as ``compute run``) and registers it here:
``(name, version) -> tree_id``. Versions count up from 1 per name; pushing a
tree the name already has returns that version (idempotent). A job's manifest
names inputs by ``name@<version>`` or ``name@sha256:<tree id>``; the controller
resolves them at submit (:meth:`NamedInputStore.resolve`) and records the
result in ``job_input``, so a job always runs on exactly the tree it was
submitted with.

* **Pinned** inputs are never evicted by GC - on the controller (its pins are
  recomputed from this table) and on every agent: :class:`PinBroadcaster` keeps
  the pinned tree list in memory and sends it (``GcRequest{set_pins}``) to every
  agent after its Reconcile and to all live agents whenever it changes.
* ``rm`` is refused (``conflict``, naming the jobs) while a non-terminal job
  references that version; the rows of finished jobs stay as history.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.manifest import InputSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JOB_TERMINAL_STATES
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import JobInput, NamedInput
from meshforge.controller.store.models.job import Job

if TYPE_CHECKING:
    from meshforge.controller.session.live import LiveSession
    from meshforge.controller.session.manager import SessionManager

__all__ = [
    "MAX_LISTED",
    "NamedInputError",
    "NamedInputRecord",
    "NamedInputStore",
    "PinBroadcaster",
    "ResolvedInput",
]

log = get_logger(__name__)

MAX_LISTED: Final = 1000
_PUSH_RETRIES: Final = 12
_PUSH_BACKOFF_S: Final = 0.02
_NAME_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_TERMINAL: Final = tuple(s.value for s in JOB_TERMINAL_STATES)


class NamedInputError(MeshForgeError):
    """Unknown input (``not_found``), referenced (``conflict``)."""


@dataclass(frozen=True, slots=True)
class NamedInputRecord:
    name: str
    version: int
    tree_id: str
    size: int
    files: int
    pinned: bool
    created_at: dt.datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "ref": f"{self.name}@{self.version}",
            "tree_id": self.tree_id,
            "size": self.size,
            "files": self.files,
            "pinned": self.pinned,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    ref: str
    mount: str
    name: str
    version: int
    tree_id: str


def _record(row: NamedInput) -> NamedInputRecord:
    return NamedInputRecord(
        name=row.name,
        version=int(row.version),
        tree_id=row.tree_id,
        size=int(row.size),
        files=int(row.files),
        pinned=bool(row.pinned),
        created_at=row.created_at,
    )


class NamedInputStore:
    """See module docstring."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = create_session_factory(engine)

    async def push(
        self, name: str, tree_id: str, *, size: int, files: int, pinned: bool
    ) -> tuple[NamedInputRecord, bool]:
        """Register ``tree_id`` under ``name``: ``(record, created)``. The same
        tree again returns its version (pinned if asked now).

        Pushes to one name are serialised by a transaction-scoped advisory
        lock on the name, so concurrent pushes take versions one after the
        other instead of colliding on ``(name, version)``; a collision that
        still happens (e.g. another writer without the lock) is retried with a
        jittered, growing back-off (G5-T2)."""
        for attempt in range(_PUSH_RETRIES):
            try:
                async with self._sessions() as db, db.begin():
                    await db.execute(_NAME_LOCK, {"key": f"named_input:{name}"})
                    existing = await db.scalar(
                        select(NamedInput).where(
                            NamedInput.name == name, NamedInput.tree_id == tree_id
                        )
                    )
                    if existing is not None:
                        if pinned and not existing.pinned:
                            existing.pinned = True
                        await db.flush()
                        return _record(existing), False
                    top = await db.scalar(
                        select(func.max(NamedInput.version)).where(
                            NamedInput.name == name
                        )
                    )
                    row = NamedInput(
                        name=name,
                        version=int(top or 0) + 1,
                        tree_id=tree_id,
                        size=size,
                        files=files,
                        pinned=pinned,
                    )
                    db.add(row)
                    await db.flush()
                    await db.refresh(row)
                    return _record(row), True
            except IntegrityError:
                # A concurrent push took that version: read again, later.
                delay = _PUSH_BACKOFF_S * (attempt + 1)
                await asyncio.sleep(random.uniform(0, delay))  # noqa: S311 - jitter
                continue
        raise NamedInputError(
            "could not register the input (concurrent pushes kept colliding)",
            failure_type=FailureType.CONFLICT,
        )

    async def list_all(self, *, limit: int = MAX_LISTED) -> list[NamedInputRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(NamedInput)
                .order_by(NamedInput.name, NamedInput.version)
                .limit(limit)
            )
            return [_record(r) for r in rows]

    async def versions(self, name: str) -> list[NamedInputRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(NamedInput)
                .where(NamedInput.name == name)
                .order_by(NamedInput.version)
                .limit(MAX_LISTED)
            )
            return [_record(r) for r in rows]

    async def set_pinned(
        self, name: str, version: int, pinned: bool
    ) -> NamedInputRecord | None:
        async with self._sessions() as db, db.begin():
            row = await db.get(NamedInput, (name, version))
            if row is None:
                return None
            row.pinned = pinned
            await db.flush()
            return _record(row)

    async def references(self, name: str, version: int) -> list[str]:
        """Ids of non-terminal jobs that use ``name@version`` (at most 20)."""
        async with self._sessions() as db:
            rows = await db.scalars(
                select(JobInput.job_id)
                .join(Job, Job.id == JobInput.job_id)
                .where(
                    JobInput.name == name,
                    JobInput.version == version,
                    Job.state.not_in(_TERMINAL),
                )
                .limit(20)
            )
            return list(rows)

    async def remove(self, name: str, version: int) -> NamedInputRecord:
        """Delete one version; refused while a non-terminal job uses it."""
        async with self._sessions() as db, db.begin():
            row = await db.get(NamedInput, (name, version), with_for_update=True)
            if row is None:
                raise NamedInputError(
                    f"no input {name}@{version}", failure_type=FailureType.NOT_FOUND
                )
            users = await db.scalars(
                select(JobInput.job_id)
                .join(Job, Job.id == JobInput.job_id)
                .where(
                    JobInput.name == name,
                    JobInput.version == version,
                    Job.state.not_in(_TERMINAL),
                )
                .limit(20)
            )
            busy = list(users)
            if busy:
                raise NamedInputError(
                    f"{name}@{version} is used by {len(busy)} unfinished job(s): "
                    + ", ".join(busy),
                    failure_type=FailureType.CONFLICT,
                    details={"jobs": busy},
                )
            record = _record(row)
            await db.execute(
                delete(NamedInput).where(
                    NamedInput.name == name, NamedInput.version == version
                )
            )
            return record

    async def pinned_trees(self) -> list[str]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(NamedInput.tree_id)
                .where(NamedInput.pinned.is_(True))
                .distinct()
                .order_by(NamedInput.tree_id)
            )
            return list(rows)

    async def resolve(self, specs: Sequence[InputSpec]) -> list[ResolvedInput]:
        """Each manifest input -> its registered version (``not_found`` names
        the first ref that is not registered)."""
        out: list[ResolvedInput] = []
        async with self._sessions() as db:
            for spec in specs:
                query = select(NamedInput).where(NamedInput.name == spec.name)
                if spec.version is not None:
                    query = query.where(NamedInput.version == spec.version)
                else:
                    query = query.where(NamedInput.tree_id == spec.tree_id)
                row = await db.scalar(query)
                if row is None:
                    raise NamedInputError(
                        f"input {spec.ref} is not registered (compute data ls)",
                        failure_type=FailureType.NOT_FOUND,
                        details={"ref": spec.ref},
                    )
                out.append(
                    ResolvedInput(
                        ref=spec.ref,
                        mount=spec.mount,
                        name=row.name,
                        version=int(row.version),
                        tree_id=row.tree_id,
                    )
                )
        return out

    async def job_inputs(self, job_id: str) -> list[ResolvedInput]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(JobInput)
                .where(JobInput.job_id == job_id)
                .order_by(JobInput.mount)
            )
            return [
                ResolvedInput(
                    ref=r.ref,
                    mount=r.mount,
                    name=r.name,
                    version=int(r.version),
                    tree_id=r.tree_id,
                )
                for r in rows
            ]


class PinBroadcaster:
    """Sends the pinned trees to agents (module docstring)."""

    def __init__(self, store: NamedInputStore, manager: SessionManager) -> None:
        self._store = store
        self._manager = manager
        self.trees: list[str] = []
        self.sent = 0

    async def refresh(self, *, broadcast: bool) -> None:
        """Reload the pinned list; send it to every live agent if asked."""
        self.trees = await self._store.pinned_trees()
        if broadcast:
            for live in self._manager.snapshot():
                self._send(live.node_id)

    def on_reconcile(self, live: LiveSession) -> None:
        """Every session gets the current pins right after its Reconcile."""
        self._send(live.node_id)

    def _send(self, node_id: str) -> None:
        sent = self._manager.send_command(
            node_id,
            "gc",
            agent_pb2.GcRequest(set_pins=True, pinned_trees=self.trees),
        )
        if sent is not None:
            self.sent += 1
