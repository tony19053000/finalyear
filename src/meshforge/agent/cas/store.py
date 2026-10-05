"""The agent's blob cache: a :class:`CasStore` plus per-digest fetch locks.

Location ``<data dir>/cas`` (ARCHITECTURE §23). The free-disk floor (default
1 GiB) is enforced on every write, so a large fetch cannot fill the laptop's
disk; the quota is enforced by GC passes (``[agent.gc]``, V0.5 G5 D91), not by
refusing writes - a staging always completes and the pass then evicts LRU blobs
that no running attempt holds.

Several observers may watch the store (:meth:`AgentCas.add_observer`: the cache
inventory for reports, the GC index for last use).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Final

from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import DEFAULT_MAX_TOTAL_BYTES, Tree

__all__ = ["CAS_DIR_NAME", "DEFAULT_FREE_DISK_FLOOR_BYTES", "AgentCas"]

log = get_logger(__name__)

CAS_DIR_NAME: Final = "cas"
DEFAULT_FREE_DISK_FLOOR_BYTES: Final = 1024**3


class AgentCas:
    """One per agent process; safe for concurrent use from its event loop."""

    def __init__(self, store: CasStore) -> None:
        self.store = store
        self._locks: dict[str, list[Any]] = {}
        self._prepared = False
        self._observers: list[Callable[[bool, Digest, int], None]] = []
        self.on_tree_complete: Callable[[Tree], object] | None = None
        """V0.5 G4 (D100): told after a staging verified a whole tree in the
        cache (the cache inventory then reports ``tree <tree_id>``)."""

    def add_observer(self, observer: Callable[[bool, Digest, int], None]) -> None:
        """Called ``(added, digest, size)`` after every install/eviction (on the
        writing thread; see :attr:`CasStore.on_change`). Observers run in the
        order added; one that raises does not stop the others."""
        self._observers.append(observer)
        self.store.on_change = self._fan_out

    def _fan_out(self, added: bool, digest: Digest, size: int) -> None:
        for observer in list(self._observers):
            try:
                observer(added, digest, size)
            except Exception:  # logged; the write stands
                log.exception("agent.cas.observer_failed", digest=str(digest))

    def tree_staged(self, tree: Tree) -> None:
        if self.on_tree_complete is not None:
            self.on_tree_complete(tree)

    @classmethod
    def at(
        cls,
        data_dir: Path,
        *,
        free_disk_floor_bytes: int = DEFAULT_FREE_DISK_FLOOR_BYTES,
        max_blob_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> AgentCas:
        return cls(
            CasStore(
                data_dir / CAS_DIR_NAME,
                max_blob_bytes=max_blob_bytes,
                free_disk_floor_bytes=free_disk_floor_bytes,
            )
        )

    async def prepare(self) -> None:
        """Create the layout and sweep crash leftovers (once per process)."""
        if not self._prepared:
            await asyncio.to_thread(self.store.prepare)
            self._prepared = True

    @contextlib.asynccontextmanager
    async def holding(self, digest: Digest) -> AsyncIterator[None]:
        """Exclusive per digest across this process; the entry is dropped when
        nobody holds or waits for it (reference counted, so no unbounded dict)."""
        entry = self._locks.get(digest.hex)
        if entry is None:
            entry = self._locks[digest.hex] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(digest.hex, None)
