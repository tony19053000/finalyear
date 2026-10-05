"""V0.5 G5 fix round 1: the GC index's ``blob_lru.held`` flag (G5-T4).

``held`` is only an optimisation (the victim query re-checks every candidate),
but a stale ``held = 1`` would make a blob unevictable forever and a stale
``held = 0`` brings the per-batch rescan back - so after any sequence of
leases, pins, member recordings and removals it must equal the authoritative
protection expression for every blob.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.common.gc_index import DB_NAME, GcIndex, _held

pytestmark = [pytest.mark.unit]


def hx(n: int) -> str:
    return hashlib.sha256(str(n).encode()).hexdigest()


def _mismatches(index: GcIndex) -> list[tuple[str, int, int]]:
    with index.tx() as db:
        rows = db.execute(
            "SELECT key, held, @HELD FROM blob_lru".replace(
                "@HELD", _held("blob_lru.key")
            )
        ).fetchall()
        mirrored = db.execute(
            "SELECT (SELECT COUNT(*) FROM blob_lru), "
            "(SELECT COUNT(*) FROM item WHERE kind = 'blob')"
        ).fetchone()
    assert mirrored[0] == mirrored[1]
    return [(k, int(h), int(w)) for k, h, w in rows if int(h) != int(w)]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[GcIndex]:
    gc = GcIndex(tmp_path / "gc", wall=lambda: 1000.0)
    gc.open()
    yield gc
    gc.close()


def test_held_matches_the_protection_rules_after_random_operations(
    index: GcIndex,
) -> None:
    rnd = random.Random(5)  # noqa: S311 - a reproducible op sequence, not crypto
    blobs = [hx(n) for n in range(60)]
    trees = [hx(1000 + n) for n in range(6)]
    for key in blobs + trees:
        index.upsert("blob", key, 10)
    members = {t: rnd.sample(blobs, 12) for t in trees}  # overlapping trees
    leases: set[str] = set()
    for step in range(400):
        op = rnd.randrange(8)
        tree = rnd.choice(trees)
        if op == 0:
            index.record_tree(tree, members[tree])
        elif op == 1:
            lease_id = f"l{rnd.randrange(5)}"
            index.lease(lease_id, [("tree", tree), ("blob", rnd.choice(blobs))])
            leases.add(lease_id)
        elif op == 2 and leases:
            index.release(leases.pop())
        elif op == 3:
            index.set_pins("tree", rnd.sample(trees, rnd.randrange(3)))
        elif op == 4:
            index.set_pins("blob", rnd.sample(blobs, rnd.randrange(3)))
        elif op == 5:
            index.remove("blob", tree)  # the manifest went (member list too)
            index.upsert("blob", tree, 10)
        elif op == 6:
            gone = rnd.choice(blobs)
            index.remove("blob", gone)
            index.upsert("blob", gone, 10)  # re-fetched: held recomputed
        else:
            present = {k: 10 for k in blobs + trees if rnd.random() > 0.05}
            index.reconcile("blob", present)
            index.reconcile("blob", dict.fromkeys(blobs + trees, 10))
        assert _mismatches(index) == [], f"step {step} op {op}"


def test_a_blob_in_two_protected_trees_stays_held_until_both_go(
    index: GcIndex,
) -> None:
    shared, a, b = hx(1), hx(2), hx(3)
    for key in (shared, a, b):
        index.upsert("blob", key, 1)
    index.record_tree(a, [shared])
    index.record_tree(b, [shared])
    index.set_pins("tree", [a])
    index.lease("att", [("tree", b)])
    index.set_pins("tree", [])
    # a is unpinned now; shared is still held by b's lease
    assert {v.key for v in index.victims("blob", cutoff=1e9, limit=10)} == {a}
    index.release("att")
    assert {v.key for v in index.victims("blob", cutoff=1e9, limit=10)} == {
        shared,
        a,
        b,
    }
    assert _mismatches(index) == []


def test_members_recorded_after_the_pin_are_held(index: GcIndex) -> None:
    tree, member = hx(10), hx(11)
    index.upsert("blob", tree, 1)
    index.upsert("blob", member, 1)
    index.set_pins("tree", [tree])
    assert [v.key for v in index.victims("blob", cutoff=1e9, limit=10)] == [member]
    index.record_tree(tree, [member])
    assert index.victims("blob", cutoff=1e9, limit=10) == []
    assert _mismatches(index) == []


def test_a_killed_owners_leases_are_unheld_when_reaped(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    keeper = GcIndex(root)
    keeper.open()
    other = GcIndex(root)
    other.open()
    try:
        keeper.upsert("blob", hx(1), 1)
        other.lease("att", [("blob", hx(1))])
        assert keeper.victims("blob", cutoff=1e12, limit=5) == []
        # "kill" the other process: its owner lock goes, its rows stay
        assert other._owner_fh is not None
        other._owner_fh.close()
        other._owner_fh = None
        assert keeper.reap_dead_owners() == 1
        assert [v.key for v in keeper.victims("blob", cutoff=1e12, limit=5)] == [hx(1)]
        assert _mismatches(keeper) == []
    finally:
        keeper.close()
        assert other._conn is not None
        other._conn.close()
        other._conn = None


def test_an_index_from_before_blob_lru_is_migrated(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    root.mkdir()
    raw = sqlite3.connect(root / DB_NAME)
    raw.executescript(
        """
        CREATE TABLE item(kind TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL,
            last_use REAL NOT NULL, PRIMARY KEY(kind, key)) WITHOUT ROWID;
        CREATE TABLE pin(kind TEXT NOT NULL, key TEXT NOT NULL,
            PRIMARY KEY(kind, key)) WITHOUT ROWID;
        """
    )
    raw.executemany(
        "INSERT INTO item VALUES ('blob', ?, 5, ?)",
        [(hx(1), 1.0), (hx(2), 2.0), (hx(3), 3.0)],
    )
    raw.execute("INSERT INTO pin VALUES ('blob', ?)", (hx(1),))
    raw.commit()
    raw.close()
    index = GcIndex(root)
    index.open()
    try:
        assert [v.key for v in index.victims("blob", cutoff=1e12, limit=5)] == [
            hx(2),
            hx(3),
        ]
        assert _mismatches(index) == []
    finally:
        index.close()


def test_a_locked_index_is_never_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    first.upsert("blob", hx(1), 3)
    raw = sqlite3.connect(root / DB_NAME, isolation_level=None)
    raw.execute("BEGIN EXCLUSIVE")
    try:
        blocked = GcIndex(root, busy_timeout_s=0.2)
        with pytest.raises(sqlite3.OperationalError):
            blocked.open()
    finally:
        raw.execute("ROLLBACK")
        raw.close()
    try:
        assert not any(".corrupt-" in p.name for p in root.iterdir())
        assert first.usage("blob") == 3
    finally:
        first.close()
