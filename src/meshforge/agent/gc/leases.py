"""What a running attempt holds, so GC never evicts it (V0.5 G5, D91).

An :class:`AttemptLeases` is created per attempt by the executor and made
*current* for the attempt's task (a context variable, inherited by every step
it awaits). Holders announce what they use:

* the executor leases each tree **before** staging it and records the tree's
  members as soon as the manifest is verified (:meth:`AttemptLeases.tree`);
* the environment builder and the Docker runtime call :func:`lease_env` with
  the env hash **before** checking whether the env exists - so an env cannot
  be evicted between "it is ready" and the job using it.

Leases live in the GC index (one SQLite transaction each), so another process
on the same data dir sees them; they are released when the attempt ends (and
reaped if the process dies). A lease that cannot be recorded fails the attempt
(``internal_error``, reason ``gc_lease_failed``): running unprotected could see
the job's environment removed under it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Final

from meshforge.agent.envs.errors import EnvError
from meshforge.common.errors import FailureType
from meshforge.common.gc_index import GcIndex
from meshforge.common.logging import get_logger
from meshforge.common.tree import Tree

__all__ = ["AttemptLeases", "LeaseError", "current_leases", "lease_env"]

log = get_logger(__name__)

_CURRENT: Final[ContextVar[AttemptLeases | None]] = ContextVar(
    "meshforge_gc_leases", default=None
)


class LeaseError(EnvError):
    """A lease could not be recorded (an ``EnvError`` so the runtimes report it
    typed: ``internal_error`` / ``gc_lease_failed``)."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message, reason="gc_lease_failed", failure_type=FailureType.INTERNAL_ERROR
        )


class AttemptLeases:
    """The leases of one attempt (``index`` None = GC off: every call no-ops)."""

    def __init__(self, index: GcIndex | None, lease_id: str) -> None:
        self._index = index
        self.lease_id = lease_id
        self.held: set[tuple[str, str]] = set()

    async def _call(self, what: str, fn: Callable[..., object], *args: object) -> None:
        if self._index is None:
            return
        try:
            await asyncio.to_thread(fn, *args)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            raise LeaseError(
                f"the GC index could not record a lease ({what}): {type(exc).__name__}"
            ) from None

    async def add(self, kind: str, key: str) -> None:
        if (kind, key) in self.held or self._index is None:
            return
        await self._call("add", self._index.lease, self.lease_id, [(kind, key)])
        self.held.add((kind, key))

    async def tree(self, tree: Tree) -> None:
        """Record the verified tree's members (its lease then protects them)."""
        if self._index is None:
            return
        members = frozenset(entry.sha256 for entry in tree.files())
        await self._call("tree", self._index.record_tree, tree.tree_id.hex, members)

    async def touch_tree(self, tree_hex: str) -> None:
        if self._index is None:
            return
        await self._call("touch", self._index.touch_tree, tree_hex)

    async def release(self) -> None:
        if self._index is None or not self.held:
            return
        try:
            await asyncio.to_thread(self._index.release, self.lease_id)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            # Kept until this process ends (its owner is then reaped).
            log.warning(
                "agent.gc.release_failed",
                attempt_id=self.lease_id,
                cause=type(exc).__name__,
            )
        else:
            self.held.clear()

    @contextlib.contextmanager
    def current(self) -> Iterator[None]:
        """Make these the current task's leases for the block."""
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)


def current_leases() -> AttemptLeases | None:
    return _CURRENT.get()


async def lease_env(kind: str, digest: str) -> None:
    """Lease env ``<kind>/<digest>`` for the current attempt (no-op outside one)."""
    leases = _CURRENT.get()
    if leases is not None:
        await leases.add("env", f"{kind}/{digest}")
