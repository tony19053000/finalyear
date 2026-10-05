"""The GC index's SQLite schema, its SQL and the schema check (V0.5 G5, D91;
split out of :mod:`meshforge.common.gc_index` in V0.6 G1, D119).

Private to :mod:`meshforge.common.gc_index`: the table/trigger definitions,
the "is this blob held?" predicate that the triggers, the re-hold updates and
the victim query share, the corrupt/foreign-file detection and the
protect/unprotect bookkeeping of ``blob_lru.held``. See the index module's
docstring for what the tables mean.
"""

from __future__ import annotations

import functools
import sqlite3
from typing import Final

__all__: list[str] = []

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
CREATE TABLE IF NOT EXISTS blob_lru(
    key TEXT PRIMARY KEY, last_use REAL NOT NULL, held INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS blob_lru_free ON blob_lru(last_use) WHERE held = 0;
"""


# Is the blob ``@KEY`` protected right now? A lease or pin of the blob (or of
# the tree whose manifest it is), or membership of a leased/pinned tree.
# ``@KEY`` / ``@HELD`` are placeholders for column names of this module only.
_HELD_SQL: Final = """(
  EXISTS(SELECT 1 FROM lease WHERE kind IN ('tree', 'blob') AND key = @KEY)
  OR EXISTS(SELECT 1 FROM pin WHERE kind IN ('tree', 'blob') AND key = @KEY)
  OR EXISTS(SELECT 1 FROM tree_member AS m WHERE m.blob = @KEY AND (
    EXISTS(SELECT 1 FROM lease AS l WHERE l.kind = 'tree' AND l.key = m.tree)
    OR EXISTS(SELECT 1 FROM pin AS p WHERE p.kind = 'tree' AND p.key = m.tree))))"""


def _held(key: str) -> str:
    """:data:`_HELD_SQL` for the column ``key`` (e.g. ``"NEW.key"``)."""
    return _HELD_SQL.replace("@KEY", key)


# blob_lru mirrors the blob rows of ``item`` (also rows written by hand).
_TRIGGERS: Final = """
CREATE TRIGGER IF NOT EXISTS item_blob_ins AFTER INSERT ON item
WHEN NEW.kind = 'blob' BEGIN
    INSERT OR REPLACE INTO blob_lru VALUES (NEW.key, NEW.last_use, @HELD);
END;
CREATE TRIGGER IF NOT EXISTS item_blob_use AFTER UPDATE OF last_use ON item
WHEN NEW.kind = 'blob' BEGIN
    UPDATE blob_lru SET last_use = NEW.last_use WHERE key = NEW.key;
END;
CREATE TRIGGER IF NOT EXISTS item_blob_del AFTER DELETE ON item
WHEN OLD.kind = 'blob' BEGIN
    DELETE FROM blob_lru WHERE key = OLD.key;
END;
""".replace("@HELD", _held("NEW.key"))
_SCHEMA_VERSION: Final = 1
_CORRUPT_ERRORS: Final = frozenset({"SQLITE_NOTADB", "SQLITE_CORRUPT"})
_MIRROR: Final = (
    "INSERT INTO blob_lru SELECT key, last_use, @HELD FROM item WHERE kind = 'blob'"
).replace("@HELD", _held("item.key"))
_REHOLD: Final = "UPDATE blob_lru SET held = @HELD WHERE key = ?".replace(
    "@HELD", _held("blob_lru.key")
)
_REHOLD_MEMBERS: Final = (
    "UPDATE blob_lru SET held = @HELD "
    "WHERE key IN (SELECT blob FROM tree_member WHERE tree = ?)"
).replace("@HELD", _held("blob_lru.key"))

# Only blob_lru rows not marked held are walked (partial index); each one is
# still checked against the authoritative lease/pin/member tables here.
_BLOB_VICTIMS: Final = """
SELECT b.key, i.size FROM blob_lru AS b INDEXED BY blob_lru_free
JOIN item AS i ON i.kind = 'blob' AND i.key = b.key
WHERE b.held = 0 AND b.last_use <= :cutoff AND NOT @HELD
ORDER BY b.last_use, b.key LIMIT :limit
""".replace("@HELD", _held("b.key"))
_ENV_VICTIMS: Final = """
SELECT key, size FROM item
WHERE kind = 'env' AND last_use <= :cutoff
  AND key NOT IN (SELECT key FROM lease WHERE kind = 'env')
  AND key NOT IN (SELECT key FROM pin WHERE kind = 'env')
ORDER BY last_use, key LIMIT :limit
"""
_OWNER_LEASES: Final = (
    "SELECT kind, key FROM lease WHERE owner = ?",
    "DELETE FROM lease WHERE owner = ?",
)
_ONE_LEASE: Final = (
    "SELECT kind, key FROM lease WHERE owner = ? AND lease_id = ?",
    "DELETE FROM lease WHERE owner = ? AND lease_id = ?",
)
_UNKNOWN_LEASED_TREES: Final = (
    "SELECT COUNT(*) FROM lease WHERE kind = 'tree' "
    "AND key NOT IN (SELECT tree FROM tree_known)"
)


class _CorruptIndexError(sqlite3.DatabaseError):
    """``PRAGMA quick_check`` found damage."""


def _quick_check(conn: sqlite3.Connection) -> None:
    (check,) = conn.execute("PRAGMA quick_check(1)").fetchone()
    if check != "ok":
        raise _CorruptIndexError(f"quick_check: {str(check)[:100]}")


class _ForeignIndexError(_CorruptIndexError):
    """A valid SQLite file that is not (a known version of) this schema."""


_Shape = tuple[str, str, tuple[tuple[str, str, int, int], ...]]


def _columns(
    conn: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int], ...]:
    # ``table`` is always one of this module's own names (never file input).
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple((str(r[1]), str(r[2]).upper(), int(r[3]), int(r[5])) for r in rows)


@functools.cache
def _reference_schema() -> dict[str, _Shape]:
    """``name -> (type, table, columns)`` of every object this schema creates."""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(_SCHEMA + _TRIGGERS)
        return {
            str(name): (
                str(kind),
                str(table),
                _columns(ref, name) if kind == "table" else (),
            )
            for kind, name, table in ref.execute(
                "SELECT type, name, tbl_name FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*'"
            )
        }
    finally:
        ref.close()


def _check_schema(conn: sqlite3.Connection) -> None:
    """Raise :class:`_ForeignIndexError` unless the file is empty, an index
    from before ``blob_lru`` (``user_version`` 0, a subset of the tables) or
    exactly this schema at :data:`_SCHEMA_VERSION`."""
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version not in (0, _SCHEMA_VERSION):
        raise _ForeignIndexError(f"unknown user_version {version}")
    ref = _reference_schema()
    tables: set[str] = set()
    for kind, name, table in conn.execute(
        "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall():
        want = ref.get(name)
        if want is None or want[:2] != (kind, table):
            raise _ForeignIndexError(f"unexpected {kind} {str(name)[:60]!r}")
        if kind == "table":
            if _columns(conn, name) != want[2]:
                raise _ForeignIndexError(f"table {name!r} has other columns")
            tables.add(name)
    if version == _SCHEMA_VERSION:
        missing = {n for n, w in ref.items() if w[0] == "table"} - tables
        if missing:
            raise _ForeignIndexError(f"missing tables {sorted(missing)}")


def _is_corrupt(exc: sqlite3.DatabaseError) -> bool:
    return isinstance(exc, _CorruptIndexError) or (
        getattr(exc, "sqlite_errorname", "") in _CORRUPT_ERRORS
    )


def _tree_protected(db: sqlite3.Connection, tree: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM lease WHERE kind = 'tree' AND key = :t "
            "UNION ALL SELECT 1 FROM pin WHERE kind = 'tree' AND key = :t LIMIT 1",
            {"t": tree},
        ).fetchone()
        is not None
    )


def _protect(db: sqlite3.Connection, kind: str, key: str) -> None:
    """A lease or pin on ``(kind, key)`` was added: mark what it protects."""
    if kind not in ("tree", "blob"):
        return  # env items are few: their victim query checks directly
    db.execute("UPDATE blob_lru SET held = 1 WHERE key = ? AND held = 0", (key,))
    if kind == "tree":
        db.execute(
            "UPDATE blob_lru SET held = 1 WHERE held = 0 AND key IN "
            "(SELECT blob FROM tree_member WHERE tree = ?)",
            (key,),
        )


def _unprotect(db: sqlite3.Connection, kind: str, key: str) -> None:
    """A lease or pin on ``(kind, key)`` went: recompute what it protected
    (another lease, pin or tree may still protect it)."""
    if kind not in ("tree", "blob"):
        return
    db.execute(_REHOLD, (key,))
    if kind == "tree":
        db.execute(_REHOLD_MEMBERS, (key,))
