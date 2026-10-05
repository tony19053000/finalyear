"""V0.5 G5 tester round 2 - the ``blob_lru.held`` flag (fix T4) and the corrupt
index quarantine (fix T1), on real SQLite files and real processes.

Safety (critical): no victim is ever protected by a lease, a pin or a leased/
pinned tree - whatever sequence of operations ran, whatever ``held`` says.
Liveness (leak): ``held = 1`` must mean "something still protects it"; a blob
whose last protection went must become evictable again.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from meshforge.common.gc_index import DB_NAME, GcIndex

pytestmark = [pytest.mark.component, pytest.mark.timeout(240)]

BLOBS = [f"{i:064x}" for i in range(1, 25)]
TREES = [f"{0xA000 + i:064x}" for i in range(6)]


def _protected(db: sqlite3.Connection, key: str) -> bool:
    """Independent re-statement of "protected" (not the module's SQL)."""
    leased = {
        k for (k,) in db.execute("SELECT key FROM lease WHERE kind IN ('tree','blob')")
    }
    pinned = {
        k for (k,) in db.execute("SELECT key FROM pin WHERE kind IN ('tree','blob')")
    }
    base = leased | pinned
    if key in base:
        return True
    for (tree,) in db.execute("SELECT tree FROM tree_member WHERE blob = ?", (key,)):
        if tree in base:
            return True
    return False


def _check(idx: GcIndex, label: str) -> None:
    with idx.tx() as db:
        rows = db.execute("SELECT key, held FROM blob_lru").fetchall()
        items = {k for (k,) in db.execute("SELECT key FROM item WHERE kind='blob'")}
        assert {k for k, _ in rows} == items, f"{label}: blob_lru != item"
        for key, held in rows:
            prot = _protected(db, key)
            assert not (held and not prot), f"{label}: LEAK held but unprotected {key}"
        unknown = db.execute(
            "SELECT COUNT(*) FROM lease WHERE kind='tree' "
            "AND key NOT IN (SELECT tree FROM tree_known)"
        ).fetchone()[0]
        victims = idx.victims("blob", cutoff=time.time() + 10, limit=10_000)
        for v in victims:
            assert not _protected(db, v.key), (
                f"{label}: CRITICAL victim protected {v.key}"
            )
        if not unknown:
            free = {k for k, _ in rows if not _protected(db, k)}
            assert {v.key for v in victims} == free, f"{label}: evictable set drifted"


@pytest.mark.parametrize("seed", range(12))
def test_random_op_sequences_never_drift_the_held_flag(
    tmp_path: Path, seed: int
) -> None:
    rnd = random.Random(seed)  # noqa: S311 - test determinism
    root = tmp_path / "gc"
    a = GcIndex(root)
    a.open()
    b = GcIndex(root)  # a second live owner on the same dir
    b.open()
    members: dict[str, list[str]] = {
        t: rnd.sample(BLOBS, rnd.randint(0, 8)) for t in TREES
    }
    leases: list[tuple[GcIndex, str]] = []
    n = 0
    for step in range(250):
        op = rnd.choice(
            [
                "up",
                "up",
                "rm",
                "rec",
                "lease_t",
                "lease_b",
                "rel",
                "pin_t",
                "pin_b",
                "unpin",
                "forget",
                "touch",
                "reap",
                "reopen",
                "reconcile",
            ]
        )
        who = rnd.choice([a, b])
        if op == "up":
            who.upsert("blob", rnd.choice(BLOBS + TREES), 10)
        elif op == "rm":
            who.remove("blob", rnd.choice(BLOBS + TREES))
        elif op == "rec":
            t = rnd.choice(TREES)
            who.record_tree(t, members[t])
        elif op in ("lease_t", "lease_b"):
            n += 1
            lid = f"l{n}"
            items = [
                ("tree", rnd.choice(TREES))
                if op == "lease_t"
                else ("blob", rnd.choice(BLOBS))
                for _ in range(rnd.randint(1, 3))
            ]
            who.lease(lid, items)
            leases.append((who, lid))
        elif op == "rel" and leases:
            w, lid = leases.pop(rnd.randrange(len(leases)))
            w.release(lid)
        elif op == "pin_t":
            cur = who.pins("tree")
            who.set_pins("tree", cur | {rnd.choice(TREES)})
        elif op == "pin_b":
            cur = who.pins("blob")
            who.set_pins("blob", cur | {rnd.choice(BLOBS)})
        elif op == "unpin":
            kind = rnd.choice(["tree", "blob"])
            cur = who.pins(kind)
            if cur:
                who.set_pins(kind, cur - {rnd.choice(sorted(cur))})
        elif op == "forget":
            t = rnd.choice(TREES)
            who.remove("blob", t)
        elif op == "touch":
            who.touch_tree(rnd.choice(TREES))
        elif op == "reap":
            who.reap_dead_owners()
        elif op == "reconcile":
            present = dict.fromkeys(rnd.sample(BLOBS + TREES, 15), 10)
            who.reconcile("blob", present)
        elif op == "reopen":
            # an owner leaves cleanly (drops its leases) and comes back
            leases = [(w, lid) for w, lid in leases if w is not who]
            who.close()
            who.open()
        _check(a, f"seed={seed} step={step} op={op}")
    a.close()
    b.close()


def test_a_blob_whose_last_protection_goes_becomes_evictable_again(
    tmp_path: Path,
) -> None:
    """Overlapping trees sharing blobs: leak check across every release order."""
    idx = GcIndex(tmp_path / "gc")
    idx.open()
    t1, t2 = TREES[0], TREES[1]
    shared, only1, only2 = BLOBS[0], BLOBS[1], BLOBS[2]
    for k in (t1, t2, shared, only1, only2):
        idx.upsert("blob", k, 5)
    idx.lease("a", [("tree", t1)])
    idx.lease("b", [("tree", t2)])
    idx.record_tree(t1, [shared, only1])
    idx.record_tree(t2, [shared, only2])
    idx.set_pins("tree", {t1})
    far = time.time() + 100

    def free() -> set[str]:
        return {v.key for v in idx.victims("blob", cutoff=far, limit=100)}

    assert free() == set()
    idx.release("a")  # t1 still pinned, t2 leased
    assert free() == set()
    idx.set_pins("tree", set())  # t1 free; shared still held by t2
    assert free() == {t1, only1}
    idx.release("b")
    assert free() == {t1, t2, shared, only1, only2}
    idx.close()


def test_forgetting_a_leased_manifest_keeps_leak_free_and_safe(tmp_path: Path) -> None:
    idx = GcIndex(tmp_path / "gc")
    idx.open()
    t, m = TREES[0], BLOBS[0]
    idx.upsert("blob", t, 1)
    idx.upsert("blob", m, 1)
    idx.lease("a", [("tree", t)])
    idx.record_tree(t, [m])
    idx.remove("blob", t)  # manifest evicted/removed while leased: members forgotten
    idx.upsert("blob", t, 1)  # re-staged
    _check(idx, "after forget")
    idx.release("a")
    _check(idx, "after release")
    assert {v.key for v in idx.victims("blob", cutoff=time.time() + 5, limit=9)} == {
        t,
        m,
    }
    idx.close()


def test_failed_transaction_rolls_held_back_with_the_rows(tmp_path: Path) -> None:
    idx = GcIndex(tmp_path / "gc")
    idx.open()
    k = BLOBS[0]
    idx.upsert("blob", k, 1)

    def crash() -> None:
        with idx.tx():
            idx.lease("a", [("blob", k)])
            raise RuntimeError("crash between steps")

    with pytest.raises(RuntimeError):
        crash()
    _check(idx, "after rollback")
    assert idx.leased("blob") == set()
    assert [v.key for v in idx.victims("blob", cutoff=time.time() + 5, limit=9)] == [k]
    idx.close()


KILLED = """
import sys, time
from pathlib import Path
from meshforge.common.gc_index import GcIndex
idx = GcIndex(Path(sys.argv[1]))
idx.open()
idx.lease("att", [("tree", sys.argv[2]), ("blob", sys.argv[3])])
idx.record_tree(sys.argv[2], [sys.argv[3], sys.argv[4]])
print("ready", flush=True)
time.sleep(600)
"""


@pytest.mark.asyncio
async def test_sigkilled_owner_is_reaped_and_nothing_stays_held_forever(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    idx = GcIndex(root)
    idx.open()
    t, m1, m2 = TREES[0], BLOBS[0], BLOBS[1]
    for k in (t, m1, m2):
        idx.upsert("blob", k, 1)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        KILLED,
        str(root),
        t,
        m1,
        m2,
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    assert child.stdout is not None
    assert (await child.stdout.readline()).strip() == b"ready"
    far = time.time() + 100
    assert idx.victims("blob", cutoff=far, limit=9) == []  # protected
    child.send_signal(signal.SIGKILL)
    await child.wait()
    assert idx.reap_dead_owners() == 1
    _check(idx, "after reap")
    assert {v.key for v in idx.victims("blob", cutoff=far, limit=9)} == {t, m1, m2}
    idx.close()


def test_migration_of_an_old_format_index_keeps_every_protection(
    tmp_path: Path,
) -> None:
    """An index written by the pre-T4 code (no blob_lru, user_version 0) with
    leases, pins and recorded trees opens, migrates once and still protects."""
    root = tmp_path / "gc"
    root.mkdir()
    (root / "gc-owners").mkdir()
    old = sqlite3.connect(root / DB_NAME, isolation_level=None)
    old.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE item(kind TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL,
            last_use REAL NOT NULL, PRIMARY KEY(kind, key)) WITHOUT ROWID;
        CREATE INDEX item_lru ON item(kind, last_use);
        CREATE TABLE tree_member(tree TEXT NOT NULL, blob TEXT NOT NULL,
            PRIMARY KEY(tree, blob)) WITHOUT ROWID;
        CREATE INDEX tree_member_blob ON tree_member(blob);
        CREATE TABLE tree_known(tree TEXT PRIMARY KEY) WITHOUT ROWID;
        CREATE TABLE pin(kind TEXT NOT NULL, key TEXT NOT NULL,
            PRIMARY KEY(kind, key)) WITHOUT ROWID;
        CREATE TABLE owner(owner TEXT PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE lease(owner TEXT NOT NULL, lease_id TEXT NOT NULL, kind TEXT NOT NULL,
            key TEXT NOT NULL, PRIMARY KEY(owner, lease_id, kind, key)) WITHOUT ROWID;
        CREATE INDEX lease_key ON lease(kind, key);
        """
    )
    t1, t2, leased_blob, pinned_blob, free = (
        TREES[0],
        TREES[1],
        BLOBS[0],
        BLOBS[1],
        BLOBS[2],
    )
    m1, m2 = BLOBS[3], BLOBS[4]
    for k in (t1, t2, leased_blob, pinned_blob, free, m1, m2):
        old.execute("INSERT INTO item VALUES('blob', ?, 1, 1.0)", (k,))
    old.execute("INSERT INTO item VALUES('env', 'venv/x', 1, 1.0)")
    for tr, ms in ((t1, [m1]), (t2, [m2])):
        old.execute("INSERT INTO tree_known VALUES(?)", (tr,))
        for m in ms:
            old.execute("INSERT INTO tree_member VALUES(?, ?)", (tr, m))
    old.execute("INSERT INTO pin VALUES('tree', ?)", (t2,))
    old.execute("INSERT INTO pin VALUES('blob', ?)", (pinned_blob,))
    # a LIVE old owner (this process holds its lock) with a tree + blob lease
    from meshforge.common.filelock import try_lock

    fh = open(root / "gc-owners" / "oldowner.lock", "a+b")  # noqa: SIM115 - held on purpose
    assert try_lock(fh)
    old.execute("INSERT INTO owner VALUES('oldowner', 1.0)")
    old.execute("INSERT INTO lease VALUES('oldowner', 'L', 'tree', ?)", (t1,))
    old.execute("INSERT INTO lease VALUES('oldowner', 'L', 'blob', ?)", (leased_blob,))
    old.close()

    idx = GcIndex(root)
    idx.open()
    _check(idx, "after migration")
    got = {v.key for v in idx.victims("blob", cutoff=time.time() + 5, limit=99)}
    assert got == {free}, got
    # the migration is a one-off and idempotent across reopen
    idx.close()
    idx2 = GcIndex(root)
    idx2.open()
    _check(idx2, "after reopen")
    assert {v.key for v in idx2.victims("blob", cutoff=time.time() + 5, limit=99)} == {
        free
    }
    fh.close()
    assert idx2.reap_dead_owners() == 1
    assert {v.key for v in idx2.victims("blob", cutoff=time.time() + 5, limit=99)} == {
        free,
        t1,
        leased_blob,
        m1,
    }
    idx2.close()


def test_two_processes_migrating_the_same_old_index_at_once(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    for k in BLOBS[:5]:
        first.upsert("blob", k, 1)
    first.lease("x", [("blob", BLOBS[0])])
    with first.tx() as db:  # downgrade to the old format
        db.execute("DROP TABLE blob_lru")
    first._conn.execute("PRAGMA user_version = 0")  # type: ignore[union-attr]
    # drop triggers too: the old code had none
    for trg in ("item_blob_ins", "item_blob_use", "item_blob_del"):
        first._conn.execute(f"DROP TRIGGER {trg}")  # type: ignore[union-attr]
    second, third = GcIndex(root), GcIndex(root)
    second.open()
    third.open()
    _check(second, "second")
    _check(third, "third")
    assert BLOBS[0] not in {
        v.key for v in third.victims("blob", cutoff=time.time() + 9, limit=9)
    }
    for i in (first, second, third):
        i.close()


# ------------------------------------------------------- T1: quarantine


def _corrupt_variants(path: Path) -> dict[str, bytes]:
    return {
        "garbage": os.urandom(4096),
        "text": b"this is not a database\n" * 50,
        "header_only_truncated": path.read_bytes()[:100],
        "zeroed_header": b"\0" * 100 + path.read_bytes()[100:],
    }


@pytest.mark.parametrize(
    "variant", ["garbage", "text", "header_only_truncated", "zeroed_header"]
)
def test_corrupt_index_is_quarantined_and_rebuilt(tmp_path: Path, variant: str) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    for k in BLOBS[:10]:
        first.upsert("blob", k, 1)
    first.close()
    path = root / DB_NAME
    for suffix in ("-wal", "-shm"):
        (root / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
    path.write_bytes(_corrupt_variants(path)[variant])
    idx = GcIndex(root)
    idx.open()
    _check(idx, variant)
    assert idx.usage("blob") == 0  # derived data: reconcile re-adds it
    assert any(p.name.startswith(f"{DB_NAME}.corrupt-") for p in root.iterdir())
    idx.reconcile("blob", dict.fromkeys(BLOBS[:3], 1))
    idx.close()


def test_page_level_corruption_is_found_by_quick_check(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    for i in range(3000):
        first.upsert("blob", f"{i:064x}", 1)
    first.close()
    path = root / DB_NAME
    for suffix in ("-wal", "-shm"):
        (root / f"{DB_NAME}{suffix}").unlink(missing_ok=True)
    data = bytearray(path.read_bytes())
    for off in range(8192, min(len(data), 8192 + 4096 * 6), 7):
        data[off] ^= 0xFF
    path.write_bytes(bytes(data))
    idx = GcIndex(root)
    idx.open()  # quarantined + rebuilt, not fatal
    _check(idx, "page corruption")
    idx.close()


def test_a_locked_database_is_never_quarantined_and_open_cleans_up(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    first.upsert("blob", BLOBS[0], 1)
    holder = sqlite3.connect(root / DB_NAME, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # a writer holds the lock
    second = GcIndex(root, busy_timeout_s=0.3)
    with pytest.raises(sqlite3.OperationalError):
        second.open()
    holder.execute("ROLLBACK")
    holder.close()
    assert not [p for p in root.iterdir() if ".corrupt-" in p.name]
    # no owner lock leaked by the failed open (only first's remains)
    assert len(list((root / "gc-owners").iterdir())) == 1
    second.open()  # the same object can open once the lock is gone
    assert second.usage("blob") == 1  # data intact, not rebuilt
    first.close()
    second.close()


def test_exclusive_locked_corrupt_file_is_not_mistaken_for_locked(
    tmp_path: Path,
) -> None:
    """A rollback-journal DB held EXCLUSIVE by another process while the file
    header is garbage: whatever happens, the quarantine must never rename a
    healthy file away from a live holder."""
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    first.upsert("blob", BLOBS[0], 1)
    first.lease("a", [("blob", BLOBS[0])])
    holder = sqlite3.connect(root / DB_NAME, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    second = GcIndex(root, busy_timeout_s=0.3)
    with pytest.raises(sqlite3.OperationalError):
        second.open()
    holder.execute("ROLLBACK")
    holder.close()
    assert (root / DB_NAME).exists()
    assert not [p for p in root.iterdir() if ".corrupt-" in p.name]
    assert first.leased("blob") == {BLOBS[0]}
    first.close()


@pytest.mark.xfail(
    strict=True,
    reason="G5-R2-2: a valid SQLite gc.sqlite with a clashing 'item' table makes "
    "open() fail forever (OperationalError is neither corrupt nor retried)",
)
def test_a_foreign_sqlite_file_with_a_clashing_schema_does_not_brick_the_node(
    tmp_path: Path,
) -> None:
    """gc.sqlite is a *valid* SQLite file, but an ``item`` table of another
    shape (e.g. another tool's file): open must not fail forever."""
    root = tmp_path / "gc"
    root.mkdir()
    foreign = sqlite3.connect(root / DB_NAME)
    foreign.execute("CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT)")
    foreign.commit()
    foreign.close()
    idx = GcIndex(root)
    idx.open()
    idx.upsert("blob", BLOBS[0], 1)
    idx.close()


@pytest.mark.xfail(
    strict=True,
    reason="G5-R2-1: quarantining a corrupt index while another live process "
    "holds it silently drops that process's leases; GC then evicts its blobs",
)
@pytest.mark.asyncio
async def test_quarantine_does_not_orphan_a_live_owners_leases(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    live = GcIndex(root)
    live.open()
    live.upsert("blob", BLOBS[0], 1)
    live.lease("running-attempt", [("blob", BLOBS[0])])
    # the file is damaged underneath while 'live' keeps its connection
    path = root / DB_NAME
    live._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # type: ignore[union-attr]
    with path.open("r+b") as fh:
        fh.write(os.urandom(8192))
    second = GcIndex(root)
    try:
        try:
            second.open()
        except sqlite3.DatabaseError:
            return  # refusing to start is an acceptable, safe outcome
        # if it started, the live attempt's blob must still be protected
        second.reconcile("blob", {BLOBS[0]: 1})
        victims = second.victims("blob", cutoff=time.time() + 10, limit=9)
        assert BLOBS[0] not in {v.key for v in victims}
    finally:
        for index in (live, second):
            with contextlib.suppress(Exception):
                index.close()


# ------------------------------------------- multi-process held-flag soak

SOAK = """
import random, sys, time
from pathlib import Path
from meshforge.common.gc_index import GcIndex
seed, root = int(sys.argv[1]), Path(sys.argv[2])
rnd = random.Random(seed)
BLOBS = [f"{i:064x}" for i in range(1, 25)]
TREES = [f"{0xA000 + i:064x}" for i in range(6)]
MEM = {t: random.Random(i).sample(BLOBS, 6) for i, t in enumerate(TREES)}
idx = GcIndex(root)
idx.open()
mine = []
end = time.monotonic() + float(sys.argv[3])
n = 0
while time.monotonic() < end:
    op = rnd.random()
    if op < 0.25:
        idx.upsert("blob", rnd.choice(BLOBS + TREES), 5)
    elif op < 0.5:
        n += 1
        t = rnd.choice(TREES)
        idx.lease(f"{seed}-{n}", [("tree", t)])
        mine.append(f"{seed}-{n}")
        idx.record_tree(t, MEM[t])
    elif op < 0.7 and mine:
        idx.release(mine.pop(rnd.randrange(len(mine))))
    elif op < 0.8:
        n += 1
        idx.lease(f"{seed}-{n}", [("blob", rnd.choice(BLOBS))])
        mine.append(f"{seed}-{n}")
    elif op < 0.9:
        idx.remove("blob", rnd.choice(BLOBS + TREES))
    else:
        cur = idx.pins("tree")
        idx.set_pins("tree", cur ^ {rnd.choice(TREES)})
print("ok", flush=True)
if sys.argv[4] == "clean":
    idx.close()
else:
    time.sleep(600)
"""


@pytest.mark.asyncio
async def test_multi_process_soak_leaves_no_held_leak_and_never_evicts_protected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    idx = GcIndex(root)
    idx.open()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    children = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            SOAK,
            str(s),
            str(root),
            "4",
            "clean" if s % 2 else "hang",
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )
        for s in range(4)
    ]
    # while they run, the observer keeps checking safety through the real API
    deadline = time.monotonic() + 4.5
    while time.monotonic() < deadline:
        _check(idx, "soak")
        await asyncio.sleep(0.05)
    for child in children:
        assert child.stdout is not None
        assert (await asyncio.wait_for(child.stdout.readline(), 30)).strip() == b"ok"
    for s, child in enumerate(children):
        if s % 2 == 0:
            child.send_signal(signal.SIGKILL)
        await child.wait()
    idx.reap_dead_owners()
    _check(idx, "after reap")
    with idx.tx() as db:
        # pins by the soak remain (a pin is not a lease); everything else is free
        pinned = {k for (k,) in db.execute("SELECT key FROM pin WHERE kind='tree'")}
        assert db.execute("SELECT COUNT(*) FROM lease").fetchone()[0] == 0
        held = {k for (k,) in db.execute("SELECT key FROM blob_lru WHERE held=1")}
        for k in held:
            assert _protected(db, k), f"leaked held flag {k}"
    free = {v.key for v in idx.victims("blob", cutoff=time.time() + 9, limit=999)}
    with idx.tx() as db:
        all_items = {k for (k,) in db.execute("SELECT key FROM item WHERE kind='blob'")}
        assert free == {k for k in all_items if not _protected(db, k)}
    del pinned
    idx.close()
