"""V0.5 G5 D91: the agent GC index, collector, leases and pins.

Real files, a real SQLite index, a real CAS; the wall clock is injected so the
LRU order is exact. No sleeps.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from meshforge.agent.cache.inventory import CacheInventory
from meshforge.agent.cas import AgentCas
from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.agent.gc.leases import AttemptLeases, lease_env
from meshforge.agent.gc.service import GcService
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.filelock import locked
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.tree import Tree, build_tree

pytestmark = [pytest.mark.unit]


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Rig:
    def __init__(self, tmp_path: Path, **config: int | float) -> None:
        self.clock = Clock()
        self.cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
        self.cas.store.prepare()
        self.envs = tmp_path / "envs"
        self.index = GcIndex(self.cas.store.root, wall=self.clock)
        self.index.open()
        self.cas.add_observer(self._observe)
        cfg = {"min_age_s": 0.0, **config}
        self.gc = GarbageCollector(
            self.index,
            self.cas.store,
            GcConfig(**cfg),  # type: ignore[arg-type]
            envs_root=self.envs,
            wall=self.clock,
        )

    def _observe(self, added: bool, digest: Digest, size: int) -> None:
        if added:
            self.index.upsert("blob", digest.hex, size)
        else:
            self.index.remove("blob", digest.hex)

    def put(self, data: bytes) -> Digest:
        self.clock.now += 1
        return self.cas.store.put_bytes(data).digest

    def has(self, digest: Digest) -> bool:
        return self.cas.store.has(digest)

    def env(self, kind: str, size: int) -> str:
        self.clock.now += 1
        digest = sha256_bytes(f"{kind}{size}{self.clock.now}".encode()).hex
        folder = self.envs / kind / digest
        folder.mkdir(parents=True)
        (folder / "lib.py").write_bytes(b"x" * size)
        marker = {"env_hash": digest, "kind": kind, "summary": {"bytes": size}}
        (folder / ".meshforge-env.json").write_text(json.dumps(marker))
        self.index.upsert("env", f"{kind}/{digest}", size)
        return digest


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(**config: int | float) -> Rig:
        rig = Rig(tmp_path, **config)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.index.close()


def _stage_tree(rig: Rig, root: Path, files: dict[str, bytes]) -> Tree:
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    built = build_tree(root, chunk_size=MIN_CHUNK_SIZE)
    rig.clock.now += 1
    rig.cas.store.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        rig.cas.store.put_file(path)
    return built.tree


def test_quota_max_accepted_and_max_plus_one_evicts_the_oldest(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    old, mid, new = (rig.put(bytes([i]) * 100) for i in range(3))
    rig.gc.config = GcConfig(cas_bytes=300, min_age_s=0)
    report = rig.gc.run_pass("test")
    assert report.evicted == {} and all(map(rig.has, (old, mid, new)))  # max
    rig.gc.config = GcConfig(cas_bytes=299, min_age_s=0)
    report = rig.gc.run_pass("test")  # max + 1 over
    assert report.evicted == {"blob": 1} and report.evicted_bytes == {"blob": 100}
    assert not rig.has(old) and rig.has(mid) and rig.has(new)
    assert report.usage["blob"] == 200
    assert rig.index.usage("blob") == 200


def test_a_use_refreshes_the_lru_order(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=200)
    a, b, c = (rig.put(bytes([i]) * 100) for i in range(3))
    rig.clock.now += 1
    rig.index.touch("blob", [a.hex])  # a used again: b is now the oldest
    rig.gc.run_pass("test")
    assert rig.has(a) and not rig.has(b) and rig.has(c)


def test_min_age_protects_recent_items(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1, min_age_s=60)
    a = rig.put(b"a" * 50)
    rig.clock.now += 30
    report = rig.gc.run_pass("test")
    assert rig.has(a) and report.over_quota == ["blob"]
    rig.clock.now += 31
    rig.gc.run_pass("test")
    assert not rig.has(a)


def test_a_leased_tree_keeps_its_manifest_and_members(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1)
    tree = _stage_tree(rig, tmp_path / "p", {"a": b"A" * 70, "b": b"B" * 80})
    other = rig.put(b"other" * 10)
    rig.index.lease("att_1", [("tree", tree.tree_id.hex)])
    # Members not recorded yet (staging reads the manifest): nothing is evicted.
    report = rig.gc.run_pass("test")
    assert report.evicted == {} and rig.has(other)
    rig.index.record_tree(tree.tree_id.hex, {e.sha256 for e in tree.files()})
    report = rig.gc.run_pass("test")
    assert not rig.has(other)  # the only unprotected blob
    assert rig.has(tree.tree_id)
    assert all(rig.has(e.digest) for e in tree.files())
    assert report.over_quota == ["blob"]
    rig.index.release("att_1")
    rig.gc.run_pass("test")
    assert not rig.has(tree.tree_id)
    assert not any(rig.has(e.digest) for e in tree.files())


def test_a_pinned_tree_survives_until_unpinned(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1)
    tree = _stage_tree(rig, tmp_path / "p", {"a": b"A" * 70})
    rig.index.set_pins("tree", {tree.tree_id.hex})
    rig.index.record_tree(tree.tree_id.hex, {e.sha256 for e in tree.files()})
    rig.gc.run_pass("test")
    assert rig.has(tree.tree_id) and all(rig.has(e.digest) for e in tree.files())
    rig.index.set_pins("tree", set())
    rig.gc.run_pass("test")
    assert not rig.has(tree.tree_id)


def test_env_dirs_are_evicted_lru_but_never_a_leased_or_busy_one(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(envs_bytes=100)
    leased = rig.env("venv", 100)
    busy = rig.env("subprocess", 100)
    old = rig.env("venv", 100)
    young = rig.env("venv", 100)
    rig.index.lease("att_1", [("env", f"venv/{leased}")])
    rig.clock.now += 1
    lock = rig.envs / "locks" / f"{busy}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = threading.Event()
    done = threading.Event()

    def hold() -> None:
        with locked(lock, timeout_s=1):
            held.set()
            done.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        report = rig.gc.run_pass("test")
    finally:
        done.set()
        holder.join()
    assert (rig.envs / "venv" / leased).is_dir()
    assert (rig.envs / "subprocess" / busy).is_dir()  # its build lock was held
    assert not (rig.envs / "venv" / old).exists()
    assert report.skipped >= 1
    assert report.evicted["env"] >= 1
    # Nothing half-removed is left visible: no .gc- leftovers after the pass.
    assert not list((rig.envs / "venv").glob("*.gc-*"))
    assert (rig.envs / "venv" / young).is_dir() or report.evicted["env"] == 2


@pytest.mark.asyncio
async def test_lease_env_holds_for_the_current_attempt_only(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(envs_bytes=1)
    digest = rig.env("venv", 100)
    await lease_env("venv", digest)  # no current attempt: a no-op
    assert rig.index.leased("env") == set()
    leases = AttemptLeases(rig.index, "att_1")
    with leases.current():
        await lease_env("venv", digest)
    assert rig.index.leased("env") == {f"venv/{digest}"}
    rig.gc.run_pass("test")
    assert (rig.envs / "venv" / digest).is_dir()
    await leases.release()
    rig.gc.run_pass("test")
    assert not (rig.envs / "venv" / digest).exists()


def test_a_dead_owners_leases_are_reaped(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1)
    tree = _stage_tree(rig, tmp_path / "p", {"a": b"A" * 70})
    other = GcIndex(rig.cas.store.root, wall=rig.clock)
    other.open()
    other.lease("att_x", [("tree", tree.tree_id.hex)])
    other.record_tree(tree.tree_id.hex, {e.sha256 for e in tree.files()})
    rig.gc.run_pass("test")
    assert rig.has(tree.tree_id)  # the other owner is alive
    # The other process "dies": its owner lock is released without cleanup.
    assert other._owner_fh is not None
    other._owner_fh.close()
    other._owner_fh = None
    assert other._conn is not None
    other._conn.close()
    other._conn = None
    rig.gc.run_pass("test")
    assert not rig.has(tree.tree_id)


def test_reconcile_drops_rows_of_files_gone_and_adds_unknown_files(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    kept = rig.put(b"kept")
    gone = rig.put(b"gone")
    path = rig.cas.store.path_for(gone)
    os.chmod(path, 0o600)
    path.unlink()  # behind the index's back
    rig.cas._observers.clear()
    rig.cas.store.on_change = None
    sneaky = rig.cas.store.put_bytes(b"unseen").digest  # no observer
    rig.gc.reconcile()
    with rig.index.tx() as db:
        keys = {k for (k,) in db.execute("SELECT key FROM item WHERE kind='blob'")}
    assert keys == {kept.hex, sneaky.hex}


def test_eviction_withdraws_the_tree_from_the_cache_reports(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1)
    inventory = CacheInventory(rig.cas.store)
    rig.cas.add_observer(inventory.on_cas_change)
    tree = _stage_tree(rig, tmp_path / "p", {"a": b"A" * 70})
    assert inventory.tree_complete(tree)
    inventory.take_delta()
    rig.gc.run_pass("test")
    _added, removed = inventory.take_delta()
    assert ("tree", str(tree.tree_id)) in {(i.kind, i.key) for i in removed}
    assert not inventory.has_tree(tree.tree_id)


def test_eviction_and_a_starting_attempt_race_never_lose_a_leased_blob(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    """Many rounds: one thread runs passes with quota 1, another leases the tree
    and then checks every member. Once the lease committed, a member that was
    present may never disappear until it is released."""
    rig = make_rig(cas_bytes=1)
    tree = _stage_tree(
        rig, tmp_path / "p", {f"f{i}": bytes([i]) * 64 for i in range(8)}
    )
    members = [e.digest for e in tree.files()]
    stop = threading.Event()
    errors: list[str] = []

    def collect() -> None:
        while not stop.is_set():
            rig.gc.run_pass("race")

    collector = threading.Thread(target=collect)
    collector.start()
    try:
        for round_ in range(40):
            lease_id = f"att_{round_}"
            rig.index.lease(lease_id, [("tree", tree.tree_id.hex)])
            rig.index.record_tree(tree.tree_id.hex, {d.hex for d in members})
            present = [d for d in members if rig.has(d)]
            for _ in range(3):
                errors.extend(
                    f"round {round_}: {digest} vanished under a lease"
                    for digest in present
                    if not rig.has(digest)
                )
            # "fetch" what was missing, as a staging would
            for i, digest in enumerate(members):
                if not rig.has(digest):
                    rig.cas.store.put_bytes(bytes([i]) * 64)
            rig.index.release(lease_id)
    finally:
        stop.set()
        collector.join()
    assert errors == []


@pytest.mark.asyncio
async def test_service_pins_record_members_of_a_cached_tree(
    tmp_path: Path, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig(cas_bytes=1)
    tree = _stage_tree(rig, tmp_path / "p", {"a": b"A" * 70})
    service = GcService(rig.gc, interval_s=3600)
    assert await service.set_pins([str(tree.tree_id)]) == 1
    assert rig.index.tree_known(tree.tree_id.hex)
    await service.run_now("test")
    assert rig.has(tree.tree_id) and all(rig.has(e.digest) for e in tree.files())
    assert await service.set_pins(["not-a-digest"]) == -1  # refused whole
    assert rig.index.pins("tree") == {tree.tree_id.hex}
    assert await service.set_pins(["sha256:" + "a" * 64] * 4097) == -1  # bound
    assert await service.set_pins([]) == 0
    await service.run_now("test")
    assert not rig.has(tree.tree_id)
