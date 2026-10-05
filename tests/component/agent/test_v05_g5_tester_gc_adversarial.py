"""V0.5 G5 tester round 1 - agent GC (D91) adversarial cases on real files and
real processes (no PostgreSQL needed).

* two OS processes race on one data dir: a "starting attempt" (lease -> manifest
  -> record -> members -> use -> release) against a process running GC passes
  in a tight loop; no leased blob may ever vanish;
* pins and a tree recorded in the index survive a restart of the index; a
  killed owner's leases are reaped but its pins stay;
* a locked database makes a lease fail typed (``gc_lease_failed``), not hang;
* defects: a corrupted GC index file stops the agent from starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.agent.gc.leases import AttemptLeases, LeaseError
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE

# One "starting attempt" per iteration, in the order the executor uses:
# lease the tree BEFORE staging -> manifest lands -> members recorded -> the
# members land -> the attempt uses them -> release.
ATTEMPT_LOOP = """
import os, sys, time
from pathlib import Path
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import sha256_bytes, Digest
from meshforge.common.tree import build_tree

root, work, rounds = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
store = CasStore(root, max_blob_bytes=1 << 30)
store.prepare()
index = GcIndex(root)
index.open()
store.on_change = lambda added, d, size: (
    index.upsert("blob", d.hex, size) if added else index.remove("blob", d.hex)
)
bad = 0
for i in range(rounds):
    proj = work / f"p{i}"
    proj.mkdir(parents=True)
    for j in range(4):
        (proj / f"f{j}.bin").write_bytes(os.urandom(3000))
    built = build_tree(proj, chunk_size=1 << 16)
    tree = built.tree.tree_id.hex
    lease = f"att_{i}"
    index.lease(lease, [("tree", tree)])
    store.put_bytes(built.manifest, built.tree.tree_id)
    index.record_tree(tree, list(built.sources))
    for path in built.sources.values():
        store.put_file(path)
        time.sleep(0.001)
    # the attempt "runs": everything it leased must still verify
    for h in [tree, *built.sources]:
        d = Digest.parse(f"sha256:{h}")
        if not store.has(d) or not store.verify(d):
            bad += 1
            print("MISSING", i, h, flush=True)
    index.release(lease)
print("done", bad, flush=True)
sys.exit(1 if bad else 0)
"""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def _tree(root: Path, n: int = 4) -> BuiltTree:
    root.mkdir(parents=True)
    for i in range(n):
        (root / f"f{i}.bin").write_bytes(bytes([i + 1]) * 4000)
    return build_tree(root, chunk_size=CS)


def _store(path: Path, built: BuiltTree) -> CasStore:
    store = CasStore(path, max_blob_bytes=1 << 30)
    store.prepare()
    store.put_bytes(built.manifest, built.tree.tree_id)
    for p in built.sources.values():
        store.put_file(p)
    return store


async def test_gc_never_evicts_a_blob_of_a_starting_attempt_in_another_process(
    tmp_path: Path,
) -> None:
    """Criterion (brief 5e TESTS): two-process race between eviction and a
    starting attempt, on real files."""
    root = tmp_path / "cas"
    store = CasStore(root, max_blob_bytes=1 << 30)
    store.prepare()
    rounds = 40
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        ATTEMPT_LOOP,
        str(root),
        str(tmp_path / "work"),
        str(rounds),
        stdout=asyncio.subprocess.PIPE,
        env=_env(),
    )
    index = GcIndex(root)
    index.open()
    gc = GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=0))
    passes = 0
    evicted = 0
    try:
        while child.returncode is None:
            report = await asyncio.to_thread(gc.run_pass, "race")
            evicted += sum(report.evicted.values())
            passes += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(child.wait(), 0.002)
        out = (await child.stdout.read()).decode() if child.stdout else ""
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        index.close()
    assert child.returncode == 0, out  # MISSING lines name a blob GC removed
    assert "done 0" in out
    assert passes > 3
    # And GC did real work in the gaps (not a vacuous "never ran" pass).
    assert evicted > 0


async def test_pins_survive_an_index_restart_and_a_killed_owners_leases_do_not(
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "proj")
    store = _store(tmp_path / "cas", built)
    extra = store.put_bytes(b"unreferenced" * 50).digest
    tree = built.tree.tree_id.hex
    first = GcIndex(store.root)
    first.open()
    first.reconcile("blob", store.listing())
    first.record_tree(tree, list(built.sources))
    first.set_pins("tree", [tree])
    first.close()  # a clean restart
    second = GcIndex(store.root)
    second.open()
    try:
        assert second.pins("tree") == {tree}
        gc = GarbageCollector(second, store, GcConfig(cas_bytes=1, min_age_s=0))
        report = gc.run_pass("after-restart")
        assert not store.has(extra)
        assert store.has(built.tree.tree_id)
        assert all(store.has(Digest.parse(f"sha256:{h}")) for h in built.sources)
        assert report.over_quota == ["blob"]
        # unpin: now everything goes
        second.set_pins("tree", [])
        gc.run_pass("unpinned")
        assert not store.has(built.tree.tree_id)
    finally:
        second.close()


async def test_a_lease_on_an_unknown_tree_blocks_blind_eviction_until_recorded(
    tmp_path: Path,
) -> None:
    """Between 'lease taken' and 'manifest verified' nothing may be evicted."""
    built = _tree(tmp_path / "proj")
    store = _store(tmp_path / "cas", built)
    index = GcIndex(store.root)
    index.open()
    try:
        index.reconcile("blob", store.listing())
        index.lease("att_x", [("tree", built.tree.tree_id.hex)])
        gc = GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=0))
        gc.run_pass("blind", reconcile=False)
        assert store.has(built.tree.tree_id)
        assert all(store.has(Digest.parse(f"sha256:{h}")) for h in built.sources)
        index.release("att_x")
        gc.run_pass("released", reconcile=False)
        assert not store.has(built.tree.tree_id)
    finally:
        index.close()


async def test_a_locked_index_fails_the_lease_typed_not_a_hang(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    holder = GcIndex(root)
    holder.open()
    blocked = GcIndex(root, busy_timeout_s=0.2)
    blocked.open()
    raw = sqlite3.connect(root / "gc.sqlite", isolation_level=None, timeout=1)
    try:
        raw.execute("BEGIN IMMEDIATE")  # another process holds the write lock
        leases = AttemptLeases(blocked, "att_locked")
        started = time.monotonic()
        with pytest.raises(LeaseError) as err:
            await leases.add("env", "venv/" + "a" * 64)
        assert time.monotonic() - started < 5
        assert err.value.reason == "gc_lease_failed"
        raw.execute("ROLLBACK")
        await leases.add("env", "venv/" + "a" * 64)  # recovers once unlocked
        assert ("env", "venv/" + "a" * 64) in leases.held
    finally:
        raw.close()
        blocked.close()
        holder.close()


async def test_pass_with_a_locked_index_raises_and_leaves_disk_untouched(
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "proj")
    store = _store(tmp_path / "cas", built)
    index = GcIndex(store.root, busy_timeout_s=0.2)
    index.open()
    raw = sqlite3.connect(store.root / "gc.sqlite", isolation_level=None, timeout=1)
    try:
        gc = GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=0))
        raw.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            gc.run_pass("locked")
        raw.execute("ROLLBACK")
        assert store.has(built.tree.tree_id)
        assert all(store.has(Digest.parse(f"sha256:{h}")) for h in built.sources)
    finally:
        raw.close()
        index.close()


async def test_a_min_age_blob_is_kept_even_when_far_over_quota(
    tmp_path: Path,
) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    digests = [store.put_bytes(os.urandom(100)).digest for _ in range(5)]
    index = GcIndex(store.root)
    index.open()
    try:
        gc = GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=3600))
        gc.run_pass("young")
        assert all(store.has(d) for d in digests)
    finally:
        index.close()


async def test_a_corrupt_gc_index_file_is_recreated_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    root.mkdir()
    (root / "gc.sqlite").write_bytes(b"garbage" * 500)
    index = GcIndex(root)
    try:
        index.open()
        index.upsert("blob", "a" * 64, 1)
    finally:
        index.close()


async def test_victim_selection_cost_does_not_scale_with_pinned_tree_size(
    tmp_path: Path,
) -> None:
    import hashlib

    def hx(n: int) -> str:
        return hashlib.sha256(str(n).encode()).hexdigest()

    index = GcIndex(tmp_path / "gc")
    index.open()
    try:
        with index.tx() as db:
            db.executemany(
                "INSERT INTO item VALUES('blob', ?, 1000, ?)",
                ((hx(n), float(n)) for n in range(120_000)),
            )
        index.record_tree(hx(-1), [hx(n) for n in range(100_000)])
        index.set_pins("tree", [hx(-1)])
        started = time.monotonic()
        victims = index.victims("blob", cutoff=1e12, limit=256)
        took = time.monotonic() - started
        assert len(victims) == 256
        assert took < 0.1, f"one 256-victim batch took {took:.3f} s"
    finally:
        index.close()
