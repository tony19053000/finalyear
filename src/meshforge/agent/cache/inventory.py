"""What this node caches, kept current for the cache reports (V0.5 D90/D100/D101).

:class:`CacheInventory` is the agent-side mirror of the controller's
``cache_location`` rows for this node. Four kinds:

* ``blob`` - every blob in the agent CAS. Learned once by a scan at start-up,
  then from the CAS's ``on_change`` observer (a blob installed or evicted), so
  the CAS is never re-listed.
* ``tree`` - a tree whose manifest **and every member blob** are in the CAS
  (D100). Added after a successful staging (:meth:`tree_complete`) and
  **withdrawn as soon as any member - or the manifest - is evicted**. The
  complete trees are remembered in ``<cas>/trees.idx`` (one tree id per line,
  rewritten atomically), so an agent restart re-checks them fully (manifest
  parsed and verified, every member present) instead of forgetting them.
* ``env`` - built environments (``envs/{venv,subprocess}/<hash>`` with their
  marker), from a periodic scan.
* ``image`` - Docker images labelled ``meshforge.env_hash``, from a periodic
  listing (only where the Docker runtime is available).

Every change also lands in a *pending delta* (net: an add then a remove of the
same item cancels). :meth:`snapshot` returns the whole state **and** clears the
pending delta atomically, so a delta never repeats what a snapshot already said.

Thread-safe: the CAS observer runs on worker threads; everything is behind one
lock and every method is short.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import Tree, TreeError, TreeLimits, parse_tree

__all__ = [
    "TREES_INDEX_NAME",
    "CacheInventory",
    "Item",
    "scan_cas",
    "scan_envs",
]

log = get_logger(__name__)

TREES_INDEX_NAME: Final = "trees.idx"
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_ENV_KINDS: Final = ("venv", "subprocess")
_ENV_MARKER: Final = ".meshforge-env.json"
_MAX_MARKER_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class Item:
    kind: str
    """``blob`` | ``tree`` | ``env`` | ``image``."""
    key: str
    size: int


def scan_cas(store: CasStore) -> dict[str, int]:
    """Every blob in ``store``: hex -> size (blocking; run in a thread)."""
    out: dict[str, int] = {}
    algo = store.root / "sha256"
    if not algo.is_dir():
        return out
    for path in algo.glob("*/*/*"):
        if _HEX_RE.fullmatch(path.name):
            with contextlib.suppress(OSError):
                out[path.name] = path.stat().st_size
    return out


def scan_envs(root: Path) -> dict[str, int]:
    """Built environments under ``root``: env hash -> bytes (blocking)."""
    out: dict[str, int] = {}
    for kind in _ENV_KINDS:
        folder = root / kind
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not _HEX_RE.fullmatch(entry.name):
                continue
            try:
                with (entry / _ENV_MARKER).open("rb") as fh:
                    data = json.loads(fh.read(_MAX_MARKER_BYTES))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("env_hash") != entry.name:
                continue
            summary = data.get("summary")
            size = summary.get("bytes", 0) if isinstance(summary, dict) else 0
            out[entry.name] = int(size) if isinstance(size, int) and size >= 0 else 0
    return out


class CacheInventory:
    """See module docstring."""

    def __init__(self, store: CasStore, *, max_trees: int = 10_000) -> None:
        self._store = store
        self._max_trees = max_trees
        self._lock = threading.Lock()
        self._blobs: dict[str, int] = {}
        self._trees: dict[str, frozenset[str]] = {}
        """Complete tree hex -> member blob hexes (manifest not included)."""
        self._tree_sizes: dict[str, int] = {}
        self._trees_of: dict[str, set[str]] = {}
        """Blob hex -> complete trees it belongs to (manifest included)."""
        self._envs: dict[str, int] = {}
        self._images: dict[str, int] = {}
        self._pending: dict[tuple[str, str], tuple[bool, int]] = {}
        self._trees_dirty = False
        self.loaded = False

    # ------------------------------------------------------------- loading

    @property
    def trees_index(self) -> Path:
        return self._store.root / TREES_INDEX_NAME

    def load(self, *, limits: TreeLimits | None = None) -> None:
        """Scan the CAS and re-check the remembered trees (blocking, at start).

        Nothing loaded here is a delta: the first snapshot says it all.
        """
        blobs = scan_cas(self._store)
        listed: list[str] = []
        with contextlib.suppress(OSError):
            text = self.trees_index.read_text("ascii", errors="replace")
            listed = [ln for ln in text.splitlines() if _HEX_RE.fullmatch(ln)]
        trees: dict[str, tuple[frozenset[str], int]] = {}
        for tree_hex in listed[: self._max_trees]:
            checked = self._check_tree(tree_hex, blobs, limits)
            if checked is not None:
                trees[tree_hex] = checked
        with self._lock:
            self._blobs = blobs
            for tree_hex, (members, size) in trees.items():
                self._add_tree_locked(tree_hex, members, size, pending=False)
            self._pending.clear()
            self._trees_dirty = len(trees) != len(listed)
            self.loaded = True

    def _check_tree(
        self, tree_hex: str, blobs: Mapping[str, int], limits: TreeLimits | None
    ) -> tuple[frozenset[str], int] | None:
        if tree_hex not in blobs:
            return None
        try:
            data = self._store.path_for(Digest(tree_hex)).read_bytes()
            tree = parse_tree(data, expected=Digest(tree_hex), limits=limits)
        except (OSError, TreeError):
            return None
        members = frozenset(e.sha256 for e in tree.files())
        if not all(m in blobs for m in members):
            return None
        return members, tree.total_bytes + tree.manifest_bytes

    # ------------------------------------------------------------- changes

    def on_cas_change(self, added: bool, digest: Digest, size: int) -> None:
        """The CAS observer (any thread)."""
        with self._lock:
            if added:
                self._blobs[digest.hex] = size
                self._mark("blob", digest.hex, True, size)
                return
            if self._blobs.pop(digest.hex, None) is None:
                return
            self._mark("blob", str(digest), False, size)
            for tree_hex in list(self._trees_of.get(digest.hex, ())):
                self._drop_tree_locked(tree_hex)

    def tree_complete(self, tree: Tree) -> bool:
        """A staging verified ``tree`` (manifest + every member in the CAS).

        Re-checked here against the blob set (an eviction may have raced);
        False = not complete after all, nothing reported.
        """
        tree_hex = tree.tree_id.hex
        members = frozenset(e.sha256 for e in tree.files())
        with self._lock:
            if tree_hex in self._trees:
                return True
            if tree_hex not in self._blobs or not all(
                m in self._blobs for m in members
            ):
                return False
            if len(self._trees) >= self._max_trees:
                return False
            self._add_tree_locked(
                tree_hex, members, tree.total_bytes + tree.manifest_bytes, pending=True
            )
            self._trees_dirty = True
            return True

    def set_envs(self, envs: Mapping[str, int]) -> None:
        self._replace_kind("env", self._envs, envs)

    def set_images(self, images: Mapping[str, int]) -> None:
        self._replace_kind("image", self._images, images)

    def _replace_kind(
        self, kind: str, current: dict[str, int], new: Mapping[str, int]
    ) -> None:
        with self._lock:
            for key in [k for k in current if k not in new]:
                size = current.pop(key)
                self._mark(kind, key, False, size)
            for key, size in new.items():
                if current.get(key) != size:
                    current[key] = size
                    self._mark(kind, key, True, size)

    # ------------------------------------------------------------ internals

    def _add_tree_locked(
        self, tree_hex: str, members: frozenset[str], size: int, *, pending: bool
    ) -> None:
        self._trees[tree_hex] = members
        self._tree_sizes[tree_hex] = size
        for member in (*members, tree_hex):
            self._trees_of.setdefault(member, set()).add(tree_hex)
        if pending:
            self._mark("tree", tree_hex, True, size)

    def _drop_tree_locked(self, tree_hex: str) -> None:
        members = self._trees.pop(tree_hex, None)
        if members is None:
            return
        size = self._tree_sizes.pop(tree_hex, 0)
        for member in (*members, tree_hex):
            owners = self._trees_of.get(member)
            if owners is not None:
                owners.discard(tree_hex)
                if not owners:
                    del self._trees_of[member]
        self._mark("tree", tree_hex, False, size)
        self._trees_dirty = True

    def _mark(self, kind: str, key: str, added: bool, size: int) -> None:
        # Blob/tree keys travel as "sha256:<hex>"; env keys are bare hex.
        if kind in ("blob", "tree") and not key.startswith("sha256:"):
            key = f"sha256:{key}"
        previous = self._pending.get((kind, key))
        if previous is not None and previous[0] != added:
            del self._pending[(kind, key)]  # net zero since the last report
            return
        self._pending[(kind, key)] = (added, size)

    # --------------------------------------------------------------- output

    def snapshot(self) -> list[Item]:
        """Everything held now; clears the pending delta (atomically)."""
        with self._lock:
            self._pending.clear()
            items = [Item("blob", f"sha256:{h}", s) for h, s in self._blobs.items()]
            items += [
                Item("tree", f"sha256:{h}", self._tree_sizes.get(h, 0))
                for h in self._trees
            ]
            items += [Item("env", h, s) for h, s in self._envs.items()]
            items += [Item("image", k, s) for k, s in self._images.items()]
        return items

    def take_delta(self) -> tuple[list[Item], list[Item]]:
        """``(added, removed)`` since the last snapshot/delta; clears it."""
        with self._lock:
            pending, self._pending = self._pending, {}
        added = [Item(k, key, s) for (k, key), (a, s) in pending.items() if a]
        removed = [Item(k, key, s) for (k, key), (a, s) in pending.items() if not a]
        return added, removed

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def has_tree(self, tree_id: Digest) -> bool:
        with self._lock:
            return tree_id.hex in self._trees

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "blob": len(self._blobs),
                "tree": len(self._trees),
                "env": len(self._envs),
                "image": len(self._images),
            }

    def save_trees(self) -> None:
        """Rewrite ``trees.idx`` if it changed (blocking; atomic)."""
        with self._lock:
            if not self._trees_dirty:
                return
            text = "".join(f"{h}\n" for h in sorted(self._trees))
            self._trees_dirty = False
        try:
            atomic_write_bytes(self.trees_index, text.encode("ascii"))
        except OSError as exc:
            with self._lock:
                self._trees_dirty = True
            log.warning("agent.cache.trees_index_write_failed", error=str(exc)[:200])

    def known_trees(self) -> Iterable[str]:
        with self._lock:
            return list(self._trees)
