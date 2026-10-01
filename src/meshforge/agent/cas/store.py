"""The agent's blob cache: a :class:`CasStore` plus per-digest fetch locks.

Location ``<data dir>/cas`` (ARCHITECTURE §23). Wave 1 has no quota (GC and the
``[agent.cas]`` settings block arrive with V0.5 wave 2, D91); the free-disk floor
(default 1 GiB) is enforced on every write, so a large fetch cannot fill the
laptop's disk.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.tree import DEFAULT_MAX_TOTAL_BYTES

__all__ = ["CAS_DIR_NAME", "DEFAULT_FREE_DISK_FLOOR_BYTES", "AgentCas"]

CAS_DIR_NAME: Final = "cas"
DEFAULT_FREE_DISK_FLOOR_BYTES: Final = 1024**3


class AgentCas:
    """One per agent process; safe for concurrent use from its event loop."""

    def __init__(self, store: CasStore) -> None:
        self.store = store
        self._locks: dict[str, list[Any]] = {}
        self._prepared = False

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
