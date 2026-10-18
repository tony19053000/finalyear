"""``cache:cold`` / ``cache:warm`` on the agent (V0.9 G4, D217).

**cold** - before the attempt stages, every blob of its trees (the project
tree's manifest and members, and each named input's) that this node's CAS
holds is evicted, **except** what a lease (another running attempt) or a pin
(a pinned named input) protects - the GC index's own rule, decided inside its
transaction (:meth:`~meshforge.common.gc_index.GcIndex.protected_blobs`), so
the eviction never races a starting attempt and never deletes pinned data.
Done here, on the agent, right before staging, rather than by a controller GC
request: the eviction is then ordered before this attempt's fetch by
construction (a separate command could cross the ``StartAttempt``). Measured:
blobs/bytes evicted, blobs kept (protected).

**warm** - nothing to change here: the controller submitted an unrecorded
warm-up run of the same job on this node first (:mod:`meshforge.controller.
bench.submit`).

**Verified after staging, by what the transfer actually did** (the same
numbers as the run record's cache fields): cold = no blob of any tree was a
cache hit; warm = nothing was fetched. Otherwise the condition fails before
the workload runs. Environments are not evicted: an env hash is known only
inside the runtime's env phase, and a rebuild may need a package download the
offline LAN does not have (documented limitation).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, ClassVar, Literal

from meshforge.agent.conditions.base import ConditionController, ConditionFailedError
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import TreeError, parse_tree

__all__ = ["CacheController"]

log = get_logger(__name__)


class CacheController(ConditionController):
    kind: ClassVar[str] = "cache"

    def __init__(
        self,
        mode: Literal["cold", "warm"],
        *,
        store: CasStore,
        index: GcIndex | None,
        tree_ids: Sequence[str],
    ) -> None:
        super().__init__()
        self.mode = mode
        self._store = store
        self._index = index
        self._trees = [Digest.parse(t) for t in dict.fromkeys(tree_ids) if t]

    def _keys(self) -> set[str]:
        """Hex digests of every blob of the trees this CAS holds the manifest of."""
        keys: set[str] = set()
        for tree_id in self._trees:
            keys.add(tree_id.hex)
            if not self._store.has(tree_id):
                continue
            try:
                with self._store.open_read(tree_id) as fh:
                    tree = parse_tree(fh.read(), expected=tree_id)
            except (OSError, TreeError):
                continue  # an unreadable manifest: staging fetches it again
            keys.update(e.sha256 for e in tree.files())
        return keys

    def _evict(self) -> dict[str, Any]:
        keys = self._keys()
        evicted = evicted_bytes = 0
        protected: set[str] = set()

        def drop() -> None:
            nonlocal evicted, evicted_bytes
            for key in sorted(keys - protected):
                digest = Digest.parse(f"sha256:{key}")
                size = self._store.size(digest)
                if size is None:
                    continue
                if self._store.evict(digest):
                    evicted += 1
                    evicted_bytes += size
                if self._index is not None:
                    self._index.remove("blob", key)

        if self._index is None:
            drop()
        else:
            with self._index.tx():
                protected = self._index.protected_blobs(keys)
                drop()
        kept = sum(1 for k in protected if self._store.has(Digest.parse(f"sha256:{k}")))
        return {
            "evicted_blobs": evicted,
            "evicted_bytes": evicted_bytes,
            "kept_blobs": kept,
        }

    async def apply(self) -> None:
        self.measured = {"mode": self.mode}
        if self.mode == "cold":
            self.measured.update(await asyncio.to_thread(self._evict))

    def after_stage(self, stages: Sequence[Any]) -> None:
        hit = sum(int(s.blobs_hit) for s in stages)
        fetched = sum(int(s.blobs_fetched) for s in stages)
        self.measured.update(
            {
                "trees": len(stages),
                "blobs_hit": hit,
                "bytes_hit": sum(int(s.bytes_hit) for s in stages),
                "blobs_fetched": fetched,
                "bytes_fetched": sum(int(s.bytes_fetched) for s in stages),
                "manifest_hits": sum(1 for s in stages if s.manifest_hit),
            }
        )
        if not stages:
            raise ConditionFailedError(self.kind, "the attempt staged no tree")
        if self.mode == "cold" and (hit or self.measured["manifest_hits"]):
            raise ConditionFailedError(
                self.kind,
                f"{hit} blob(s) were still cached (in use by another attempt or pinned)",
            )
        if self.mode == "warm" and fetched:
            raise ConditionFailedError(
                self.kind,
                f"{fetched} blob(s) had to be fetched: the cache was not warm",
            )
