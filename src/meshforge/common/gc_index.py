"""The last-use index, pins and leases for GC of a CAS (V0.5 G5, D91).

Used by the agent (``<data dir>/gc/gc.sqlite``) and the controller
(``<data dir>/controller/gc/gc.sqlite``) alike - one module, no
controller<->agent import. One stdlib ``sqlite3`` database (WAL) next to the
CAS, shared by every process that uses that data dir. Tables:

* ``item(kind, key, size, last_use)`` - what can be evicted: ``blob`` (key = the
  64-hex digest; tree manifests are blobs too) and ``env`` (key =
  ``venv/<hash>``, ``subprocess/<hash>`` or ``image/<hash>``). ``last_use`` is
  wall-clock seconds, updated whenever a staging or a run uses the item.
* ``tree_member(tree, blob)`` + ``tree_known(tree)`` - the member blobs of trees
  that were leased or pinned (recorded from the verified manifest), so a lease
  on one tree protects its manifest and every member with one SQL join.
* ``pin(kind, key)`` - never evicted: ``tree`` pins protect the manifest and
  every member, ``blob`` pins one blob. On an agent: pinned named inputs (from
  the controller). On the controller: everything a non-terminal job or a pinned
  named input references, re-computed from PostgreSQL before every pass.
  Base images are never items at all.
* ``blob_lru(key, last_use, held)`` - the eviction order of blobs, kept by
  triggers on ``item``; ``held`` = 1 while a lease or pin protects the blob
  (directly or as a member of a leased/pinned tree). Victim selection walks a
  partial index over ``held = 0`` rows only, so its cost does not grow with the
  size of pinned or leased trees (a 100 000-file pinned dataset is never
  re-scanned per batch). ``held`` is set when a protection is added and
  recomputed for the affected blobs when one is removed; the victim query still
  re-checks every candidate against ``lease``/``pin``/``tree_member`` in its
  transaction, so ``held`` only ever makes selection faster, never less safe.
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

**An unusable index file** - corrupt (not a SQLite database, or failing
``quick_check`` at open) or foreign (a valid SQLite file whose tables, columns
or ``user_version`` are not this schema's) - is renamed aside
(``gc.sqlite.corrupt-<time>-<rand>``, logged ``gc.index_quarantined``) and a
fresh index is created: it is derived data. The next pass's disk reconcile
re-adds every item (last use = now, so the LRU history and ``min_age_s``
restart); tree members are re-recorded when a tree is staged or pinned again;
pins come back from their source (an agent: the controller re-sends the pinned
trees after every Reconcile; the controller: recomputed from PostgreSQL before
every pass). A locked database is never treated as corrupt.

**Never while another process lives on it.** Its leases are in that file:
moving it aside would let this process evict blobs a running attempt there
uses. So when any *other* owner lock in ``gc-owners/`` is held, ``open()``
raises :class:`GcIndexBusyError` instead (logged ``gc.index_unusable_in_use``)
and the caller runs **without GC** (no eviction by this process) until its next
start, which quarantines once the other process is gone. Opens are serialised
by ``gc.open.lock`` (never deleted) and an owner takes its lock only *after* its
connection is open, inside that guard - so every process that can hold leases
in the current file holds an owner lock when the check runs.

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

from meshforge.common._gc_schema import (
    _BLOB_VICTIMS,
    _ENV_VICTIMS,
    _MIRROR,
    _ONE_LEASE,
    _OWNER_LEASES,
    _REHOLD,
    _SCHEMA,
    _SCHEMA_VERSION,
    _TRIGGERS,
    _UNKNOWN_LEASED_TREES,
    _check_schema,
    _held,  # noqa: F401 - re-exported: tests build the reference predicate
    _is_corrupt,
    _protect,
    _quick_check,
    _tree_protected,
    _unprotect,
)
from meshforge.common.filelock import locked, try_lock, unlock
from meshforge.common.logging import get_logger

__all__ = [
    "DB_NAME",
    "KINDS",
    "CollectResult",
    "GcIndex",
    "GcIndexBusyError",
    "Victim",
    "collect_kind",
]

log = get_logger(__name__)

DB_NAME: Final = "gc.sqlite"
OWNERS_DIR: Final = "gc-owners"
OPEN_GUARD: Final = "gc.open.lock"
KINDS: Final = ("blob", "env")


class GcIndexBusyError(sqlite3.DatabaseError):
    """The index file is unusable (corrupt or foreign) but another live process
    has it open, so it is not moved aside (that process's leases are in it).
    GC must stay off in this process (never evict); a later start retries."""


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
        """Create/open the database and register this process as an owner.

        Raises :class:`GcIndexBusyError` when the file is unusable while
        another live process has it open (see module docstring)."""
        with self._lock:
            if self._conn is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            owners = self.root / OWNERS_DIR
            owners.mkdir(exist_ok=True)
            with locked(self.root / OPEN_GUARD, timeout_s=self._busy_timeout_s):
                conn = self._connect_or_rebuild(owners)
                lock_path = owners / f"{self.owner}.lock"
                fh: IO[bytes] | None = None
                try:
                    fh = _take_owner_lock(lock_path)
                    self._owner_fh = fh
                    self._conn = conn
                    with self.tx() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO owner VALUES (?, ?)",
                            (self.owner, self._wall()),
                        )
                except BaseException:
                    conn.close()
                    self._conn = None
                    self._owner_fh = None
                    if fh is not None:
                        unlock(fh)
                        fh.close()
                        with contextlib.suppress(OSError):
                            lock_path.unlink()
                    raise

    def _connect_or_rebuild(self, owners: Path) -> sqlite3.Connection:
        """Connect; an unusable file is quarantined and recreated - unless
        another live owner has it open (:class:`GcIndexBusyError`)."""
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise  # e.g. locked: never quarantined
            live = self._live_other_owners(owners)
            if live:
                cause = getattr(exc, "sqlite_errorname", None) or type(exc).__name__
                log.warning(
                    "gc.index_unusable_in_use",
                    path=str(self.path),
                    cause=cause,
                    live_owners=live,
                    detail="GC index unusable but another live process has it "
                    "open: not moved aside; GC is off in this process",
                )
                raise GcIndexBusyError(
                    f"GC index unusable ({cause}) while {live} other live "
                    "process(es) use it; GC stays off until a restart"
                ) from exc
            self._quarantine(exc)
            return self._connect()

    def _live_other_owners(self, owners: Path) -> int:
        """How many other owner locks are held right now (a lock file that
        cannot even be opened counts as live: never guess "dead")."""
        live = 0
        for path in sorted(owners.glob("*.lock")):
            if path.stem == self.owner:
                continue
            try:
                fh = open(path, "a+b")  # noqa: SIM115 - closed below
            except OSError:
                live += 1
                continue
            try:
                if try_lock(fh):
                    unlock(fh)  # dead: its leases are reaped anyway
                else:
                    live += 1
            finally:
                fh.close()
        return live

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_s,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _quick_check(conn)
            _check_schema(conn)
            conn.executescript(_SCHEMA + _TRIGGERS)
            (version,) = conn.execute("PRAGMA user_version").fetchone()
            if version < _SCHEMA_VERSION:
                # An index from before blob_lru: mirror the blob rows once.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM blob_lru")
                conn.execute(_MIRROR)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                conn.execute("COMMIT")
        except BaseException:
            conn.close()
            raise
        return conn

    def _quarantine(self, exc: sqlite3.DatabaseError) -> None:
        """Move a corrupt index (and its WAL/SHM files) aside; it is rebuilt."""
        stamp = f"{int(self._wall())}-{secrets.token_hex(3)}"
        aside = self.root / f"{DB_NAME}.corrupt-{stamp}"
        self.path.rename(aside)
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                (self.root / f"{DB_NAME}{suffix}").rename(
                    self.root / f"{aside.name}{suffix}"
                )
        log.warning(
            "gc.index_quarantined",
            path=str(aside),
            cause=getattr(exc, "sqlite_errorname", None) or type(exc).__name__,
            error=str(exc)[:200],
            detail="corrupt GC index moved aside; a fresh one is rebuilt from disk",
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error), self.tx() as db:
                    self._drop_leases(db, self.owner)
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
                self._forget_tree(db, key)

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
            if kind == "blob":
                for key in gone:
                    self._forget_tree(db, key)
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
            if _tree_protected(db, tree):
                _protect(db, "tree", tree)

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
            for key in wanted - current:
                _protect(db, kind, key)
            for key in current - wanted:
                _unprotect(db, kind, key)
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
            for kind, key in dict.fromkeys(listed):
                _protect(db, kind, key)

    def release(self, lease_id: str) -> None:
        with self.tx() as db:
            self._drop_leases(db, self.owner, lease_id)

    @staticmethod
    def _drop_leases(
        db: sqlite3.Connection, owner: str, lease_id: str | None = None
    ) -> None:
        """Delete ``owner``'s leases (one ``lease_id`` or all) and recompute
        what they protected."""
        if lease_id is None:
            params: tuple[str, ...] = (owner,)
            select, delete = _OWNER_LEASES
        else:
            params = (owner, lease_id)
            select, delete = _ONE_LEASE
        held = set(db.execute(select, params))
        db.execute(delete, params)
        for kind, key in held:
            _unprotect(db, kind, key)

    @staticmethod
    def _forget_tree(db: sqlite3.Connection, tree: str) -> None:
        """Drop ``tree``'s member list (its manifest is gone)."""
        members: list[tuple[str]] = []
        if _tree_protected(db, tree):
            members = db.execute(
                "SELECT blob FROM tree_member WHERE tree = ?", (tree,)
            ).fetchall()
        db.execute("DELETE FROM tree_member WHERE tree = ?", (tree,))
        db.execute("DELETE FROM tree_known WHERE tree = ?", (tree,))
        db.executemany(_REHOLD, members)

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
                        self._drop_leases(db, owner)
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


def _take_owner_lock(path: Path) -> IO[bytes]:
    fh = open(path, "a+b")  # noqa: SIM115 - held for the owner's life
    if not try_lock(fh):  # pragma: no cover - a fresh random name
        fh.close()
        raise RuntimeError("GC owner lock unexpectedly held")
    return fh


@dataclass(slots=True)
class CollectResult:
    evicted: int = 0
    evicted_bytes: int = 0
    skipped: int = 0
    usage: int = 0
    over_quota: bool = False


def collect_kind(
    index: GcIndex,
    kind: str,
    quota: int,
    *,
    cutoff: Callable[[], float],
    batch: int,
    evict: Callable[[Victim], bool],
    after_batch: Callable[[], None] | None = None,
) -> CollectResult:
    """Evict LRU ``kind`` victims until usage <= ``quota`` (0 = no quota).

    Each batch is selected **and** removed inside one index transaction
    (``evict`` runs there; True = removed, False = leave it for now). Stops when
    nothing evictable is left; ``over_quota`` then says the rest is protected.
    ``after_batch`` runs outside the transaction (slow deletions of things
    already renamed aside)."""
    result = CollectResult(usage=index.usage(kind))
    skip: set[str] = set()
    while quota and result.usage > quota:
        progressed = False
        with index.tx():
            victims = [
                v
                for v in index.victims(kind, cutoff=cutoff(), limit=batch + len(skip))
                if v.key not in skip
            ]
            for victim in victims:
                if result.usage <= quota:
                    break
                if evict(victim):
                    result.usage -= victim.size
                    result.evicted += 1
                    result.evicted_bytes += victim.size
                    progressed = True
                else:
                    skip.add(victim.key)
                    result.skipped += 1
        if after_batch is not None:
            after_batch()
        if not victims or not progressed:
            break
    result.usage = max(0, result.usage)
    result.over_quota = bool(quota) and result.usage > quota
    return result
