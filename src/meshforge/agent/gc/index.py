"""The agent's last-use index, pins and leases for GC (V0.5 G5, D91).

One stdlib ``sqlite3`` database next to the CAS (``<data dir>/cas/gc.sqlite``,
WAL) shared by every process that uses this data dir. Tables:

* ``item(kind, key, size, last_use)`` - what can be evicted: ``blob`` (key = the
  64-hex digest; tree manifests are blobs too) and ``env`` (key =
  ``venv/<hash>``, ``subprocess/<hash>`` or ``image/<hash>``). ``last_use`` is
  wall-clock seconds, updated whenever a staging or a run uses the item.
* ``tree_member(tree, blob)`` + ``tree_known(tree)`` - the member blobs of trees
  that were leased or pinned (recorded from the verified manifest), so a lease
  on one tree protects its manifest and every member with one SQL join.
* ``pin(kind, key)`` - never evicted: trees pinned by the controller (pinned
  named inputs). Base images are never items at all.
* ``lease(owner, lease_id, kind, key)`` + ``owner(owner)`` - what running
  attempts use right now (a ``tree`` or an ``env``). Every process registers an
  owner id and holds an exclusive lock on ``gc-owners/<owner>.lock`` for its
  lifetime; leases of an owner whose lock can be taken (the process died) are
  reaped. So a crashed agent never pins anything forever, and a *second
  process* on the same data dir sees the first one's leases.

**Ordering (the race between eviction and a starting attempt).** Every write is
one ``BEGIN IMMEDIATE`` transaction, so the database serialises them across
threads and processes. An eviction batch selects its victims, removes them from
disk *and* deletes their rows inside one transaction; an attempt takes its lease
in another. Whichever commits first wins: a lease taken first makes the item a
non-victim; an eviction committed first means the attempt then finds the item
gone and fetches/builds it again. A crash mid-batch rolls the rows back while
some files are gone; :meth:`GcIndex.reconcile` at the next start deletes rows
whose files are missing (old or new state per item, never a half-written one -
removal is an ``unlink`` / a directory ``rename``).

Thread-safe: one connection per process behind an ``RLock``; transactions nest
(an eviction's CAS observer deletes the row inside the same transaction).
"""

from __future__ import annotations

import contextlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from meshforge.common.filelock import try_lock, unlock
from meshforge.common.logging import get_logger

__all__ = [
    "DB_NAME",
    "KINDS",
    "GcIndex",
    "Victim",
]

log = get_logger(__name__)

DB_NAME: Final = "gc.sqlite"
OWNERS_DIR: Final = "gc-owners"
KINDS: Final = ("blob", "env")

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS item(
    kind TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL,
    last_use REAL NOT NULL, PRIMARY KEY(kind, key)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS item_lru ON item(kind, last_use);
CREATE TABLE IF NOT EXISTS tree_member(
    tree TEXT NOT NULL, blob TEXT NOT NULL, PRIMARY KEY(tree, blob)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tree_member_blob ON tree_member(blob);
CREATE TABLE IF NOT EXISTS tree_known(tree TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pin(
    kind TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(kind, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS owner(owner TEXT PRIMARY KEY, started REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lease(
    owner TEXT NOT NULL, lease_id TEXT NOT NULL, kind TEXT NOT NULL,
    key TEXT NOT NULL, PRIMARY KEY(owner, lease_id, kind, key)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS lease_key ON lease(kind, key);
"""

# Blobs protected right now: manifests and members of leased or pinned trees.
_BLOB_VICTIMS: Final = """
SELECT key, size FROM item
WHERE kind = 'blob' AND last_use <= :cutoff
  AND key NOT IN (SELECT key FROM lease WHERE kind IN ('tree', 'blob'))
  AND key NOT IN (SELECT key FROM pin WHERE kind = 'tree')
  AND key NOT IN (
    SELECT blob FROM tree_member WHERE tree IN (
      SELECT key FROM lease WHERE kind = 'tree'
      UNION SELECT key FROM pin WHERE kind = 'tree'))
ORDER BY last_use, key LIMIT :limit
"""
_ENV_VICTIMS: Final = """
SELECT key, size FROM item
WHERE kind = 'env' AND last_use <= :cutoff
  AND key NOT IN (SELECT key FROM lease WHERE kind = 'env')
  AND key NOT IN (SELECT key FROM pin WHERE kind = 'env')
ORDER BY last_use, key LIMIT :limit
"""
_UNKNOWN_LEASED_TREES: Final = (
    "SELECT COUNT(*) FROM lease WHERE kind = 'tree' "
    "AND key NOT IN (SELECT tree FROM tree_known)"
)


@dataclass(frozen=True, slots=True)
class Victim:
    kind: str
    key: str
    size: int


class GcIndex:
    """See module docstring. ``open()`` before use, ``close()`` at the end."""

    def __init__(
        self,
        root: Path,
        *,
        wall: Callable[[], float] = time.time,
        busy_timeout_s: float = 30.0,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / DB_NAME
        self._wall = wall
        self._busy_timeout_s = busy_timeout_s
        self._lock = threading.RLock()
        self._depth = 0
        self._conn: sqlite3.Connection | None = None
        self.owner = secrets.token_hex(8)
        self._owner_fh: IO[bytes] | None = None

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Create/open the database and register this process as an owner."""
        with self._lock:
            if self._conn is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            owners = self.root / OWNERS_DIR
            owners.mkdir(exist_ok=True)
            fh = open(owners / f"{self.owner}.lock", "a+b")  # noqa: SIM115 - held for life
            if not try_lock(fh):  # pragma: no cover - a fresh random name
                fh.close()
                raise RuntimeError("GC owner lock unexpectedly held")
            self._owner_fh = fh
            conn = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_s,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
            with self.tx() as db:
                db.execute(
                    "INSERT OR REPLACE INTO owner VALUES (?, ?)",
                    (self.owner, self._wall()),
                )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error), self.tx() as db:
                    db.execute("DELETE FROM lease WHERE owner = ?", (self.owner,))
                    db.execute("DELETE FROM owner WHERE owner = ?", (self.owner,))
                self._conn.close()
                self._conn = None
            if self._owner_fh is not None:
                unlock(self._owner_fh)
                self._owner_fh.close()
                self._owner_fh = None
                with contextlib.suppress(OSError):
                    (self.root / OWNERS_DIR / f"{self.owner}.lock").unlink()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction (nested calls join the outer one)."""
        with self._lock:
            conn = self._conn
            if conn is None:
                raise RuntimeError("GcIndex is not open")
            if self._depth:
                self._depth += 1
                try:
                    yield conn
                finally:
                    self._depth -= 1
                return
            conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._depth = 0

    # ---------------------------------------------------------------- items

    def upsert(self, kind: str, key: str, size: int, *, used: bool = True) -> None:
        """Record an item (``used``: its last use is now; else keep an old one)."""
        now = self._wall()
        with self.tx() as db:
            if used:
                db.execute(
                    "INSERT INTO item VALUES (?, ?, ?, ?) ON CONFLICT(kind, key) "
                    "DO UPDATE SET size = excluded.size, last_use = excluded.last_use",
                    (kind, key, size, now),
                )
            else:
                db.execute(
                    "INSERT INTO item VALUES (?, ?, ?, ?) ON CONFLICT(kind, key) "
                    "DO UPDATE SET size = excluded.size",
                    (kind, key, size, now),
                )

    def remove(self, kind: str, key: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM item WHERE kind = ? AND key = ?", (kind, key))
            if kind == "blob":
                # An evicted manifest: its member list goes with it.
                db.execute("DELETE FROM tree_member WHERE tree = ?", (key,))
                db.execute("DELETE FROM tree_known WHERE tree = ?", (key,))

    def touch(self, kind: str, keys: Iterable[str]) -> None:
        now = self._wall()
        with self.tx() as db:
            db.executemany(
                "UPDATE item SET last_use = ? WHERE kind = ? AND key = ?",
                ((now, kind, key) for key in keys),
            )

    def touch_tree(self, tree: str) -> None:
        """The manifest and every recorded member were used now."""
        now = self._wall()
        with self.tx() as db:
            db.execute(
                "UPDATE item SET last_use = :now WHERE kind = 'blob' AND (key = :t "
                "OR key IN (SELECT blob FROM tree_member WHERE tree = :t))",
                {"now": now, "t": tree},
            )

    def reconcile(self, kind: str, present: Mapping[str, int]) -> tuple[int, int]:
        """Make the ``kind`` rows equal ``present`` (``key -> size`` on disk):
        rows without a file are deleted, unseen files get a row used *now*
        (an unknown item is never the first victim). Returns (added, removed)."""
        with self.tx() as db:
            known: dict[str, int] = dict(
                db.execute("SELECT key, size FROM item WHERE kind = ?", (kind,))
            )
            gone = [key for key in known if key not in present]
            db.executemany(
                "DELETE FROM item WHERE kind = ? AND key = ?",
                ((kind, key) for key in gone),
            )
            now = self._wall()
            new = [(k, s) for k, s in present.items() if k not in known]
            db.executemany(
                "INSERT INTO item VALUES (?, ?, ?, ?)",
                ((kind, key, size, now) for key, size in new),
            )
            db.executemany(
                "UPDATE item SET size = ? WHERE kind = ? AND key = ?",
                (
                    (size, kind, key)
                    for key, size in present.items()
                    if key in known and known[key] != size
                ),
            )
            if kind == "blob" and gone:
                db.executemany(
                    "DELETE FROM tree_member WHERE tree = ?", ((k,) for k in gone)
                )
                db.executemany(
                    "DELETE FROM tree_known WHERE tree = ?", ((k,) for k in gone)
                )
        return len(new), len(gone)

    def usage(self, kind: str) -> int:
        with self.tx() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(size), 0) FROM item WHERE kind = ?", (kind,)
            ).fetchone()
        return int(row[0])

    def last_use(self, kind: str, key: str) -> float | None:
        with self.tx() as db:
            row = db.execute(
                "SELECT last_use FROM item WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        return None if row is None else float(row[0])

    # ---------------------------------------------------------------- trees

    def record_tree(self, tree: str, members: Iterable[str]) -> None:
        """Remember ``tree``'s member blobs (hex) - once per tree."""
        with self.tx() as db:
            if db.execute(
                "SELECT 1 FROM tree_known WHERE tree = ?", (tree,)
            ).fetchone():
                return
            db.executemany(
                "INSERT OR IGNORE INTO tree_member VALUES (?, ?)",
                ((tree, blob) for blob in members),
            )
            db.execute("INSERT INTO tree_known VALUES (?)", (tree,))

    def tree_known(self, tree: str) -> bool:
        with self.tx() as db:
            return (
                db.execute(
                    "SELECT 1 FROM tree_known WHERE tree = ?", (tree,)
                ).fetchone()
                is not None
            )

    # ----------------------------------------------------------------- pins

    def set_pins(self, kind: str, keys: Iterable[str]) -> tuple[int, int]:
        """Replace the pinned ``kind`` keys; returns (pinned, unpinned) counts."""
        wanted = set(keys)
        with self.tx() as db:
            current = {
                k for (k,) in db.execute("SELECT key FROM pin WHERE kind = ?", (kind,))
            }
            db.executemany(
                "DELETE FROM pin WHERE kind = ? AND key = ?",
                ((kind, k) for k in current - wanted),
            )
            db.executemany(
                "INSERT INTO pin VALUES (?, ?)", ((kind, k) for k in wanted - current)
            )
        return len(wanted - current), len(current - wanted)

    def pins(self, kind: str) -> set[str]:
        with self.tx() as db:
            return {
                k for (k,) in db.execute("SELECT key FROM pin WHERE kind = ?", (kind,))
            }

    # --------------------------------------------------------------- leases

    def lease(self, lease_id: str, items: Iterable[tuple[str, str]]) -> None:
        """Hold ``(kind, key)`` items for ``lease_id`` (idempotent); an ``env``
        or ``blob`` item that exists is touched (a lease is a use)."""
        listed = list(items)
        now = self._wall()
        with self.tx() as db:
            db.executemany(
                "INSERT OR IGNORE INTO lease VALUES (?, ?, ?, ?)",
                ((self.owner, lease_id, kind, key) for kind, key in listed),
            )
            db.executemany(
                "UPDATE item SET last_use = ? WHERE kind = ? AND key = ?",
                ((now, kind, key) for kind, key in listed if kind != "tree"),
            )

    def release(self, lease_id: str) -> None:
        with self.tx() as db:
            db.execute(
                "DELETE FROM lease WHERE owner = ? AND lease_id = ?",
                (self.owner, lease_id),
            )

    def leased(self, kind: str) -> set[str]:
        with self.tx() as db:
            return {
                k
                for (k,) in db.execute("SELECT key FROM lease WHERE kind = ?", (kind,))
            }

    def reap_dead_owners(self) -> int:
        """Drop leases of processes that are gone (their owner lock is free)."""
        reaped = 0
        owners_dir = self.root / OWNERS_DIR
        with self.tx() as db:
            owners = [o for (o,) in db.execute("SELECT owner FROM owner")]
            listed = {o for (o,) in db.execute("SELECT DISTINCT owner FROM lease")}
        for owner in set(owners) | listed:
            if owner == self.owner:
                continue
            path = owners_dir / f"{owner}.lock"
            try:
                fh = open(path, "a+b")  # noqa: SIM115 - closed below
            except OSError:
                continue
            try:
                if not try_lock(fh):
                    continue  # alive
                try:
                    with self.tx() as db:
                        db.execute("DELETE FROM lease WHERE owner = ?", (owner,))
                        db.execute("DELETE FROM owner WHERE owner = ?", (owner,))
                    reaped += 1
                finally:
                    unlock(fh)
            finally:
                fh.close()
            with contextlib.suppress(OSError):
                path.unlink()
            log.info("agent.gc.owner_reaped", owner=owner)
        return reaped

    # -------------------------------------------------------------- victims

    def victims(self, kind: str, *, cutoff: float, limit: int) -> list[Victim]:
        """LRU candidates of ``kind`` used at or before ``cutoff`` that no lease
        or pin protects. Call inside :meth:`tx` to act on them atomically. For
        blobs: empty while any leased tree's members are not recorded yet (its
        staging is reading the manifest) - nothing is evicted blind."""
        with self.tx() as db:
            if kind == "blob":
                (unknown,) = db.execute(_UNKNOWN_LEASED_TREES).fetchone()
                if unknown:
                    return []
                sql = _BLOB_VICTIMS
            elif kind == "env":
                sql = _ENV_VICTIMS
            else:
                raise ValueError(f"unknown GC kind {kind!r}")
            rows = db.execute(sql, {"cutoff": cutoff, "limit": limit}).fetchall()
        return [Victim(kind, str(k), int(s)) for k, s in rows]
