"""V0.5 G4 D90/D100/D101: the agent's cache inventory and reporter.

Real CAS on disk (tmp_path), the real observer hook; the reporter sends into a
list instead of a session.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.cache import CacheInventory, CacheReporter, scan_envs
from meshforge.agent.cache.inventory import TREES_INDEX_NAME
from meshforge.agent.cas import AgentCas
from meshforge.agent.settings import CacheSettings
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.hashing import Digest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit


def _store(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _built(tmp_path: Path, name: str = "p") -> BuiltTree:
    root = tmp_path / name
    root.mkdir()
    (root / "a.txt").write_bytes(b"alpha " + name.encode())
    (root / "b.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE + 3))
    return build_tree(root, chunk_size=MIN_CHUNK_SIZE)


def _hold(store: CasStore, built: BuiltTree) -> None:
    for hex_, path in built.sources.items():
        store.put_file(path, Digest(hex_))
    store.put_bytes(built.manifest, built.tree.tree_id)


def _keys(items: Any) -> set[tuple[str, str]]:
    return {(i.kind, i.key) for i in items}


def test_observer_tracks_blobs_and_net_delta(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    put = store.put_bytes(b"one")
    digest = put.digest
    added, removed = inv.take_delta()
    assert _keys(added) == {("blob", str(digest))} and removed == []
    # add + evict before a report: net nothing
    other = store.put_bytes(b"two").digest
    store.evict(other)
    assert inv.take_delta() == ([], [])
    store.evict(digest)
    added, removed = inv.take_delta()
    assert added == [] and _keys(removed) == {("blob", str(digest))}


def test_tree_reported_only_when_complete_and_withdrawn_on_member_eviction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    built = _built(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    # Not complete: the manifest alone.
    store.put_bytes(built.manifest, built.tree.tree_id)
    assert inv.tree_complete(built.tree) is False
    _hold(store, built)
    assert inv.tree_complete(built.tree) is True
    added, _ = inv.take_delta()
    tree_key = ("tree", str(built.tree.tree_id))
    assert tree_key in _keys(added)
    assert inv.has_tree(built.tree.tree_id)
    # Evict one member: the tree is withdrawn at once.
    member = next(iter(built.sources))
    store.evict(Digest(member))
    _, removed = inv.take_delta()
    assert tree_key in _keys(removed)
    assert not inv.has_tree(built.tree.tree_id)


def test_manifest_eviction_withdraws_the_tree(tmp_path: Path) -> None:
    store = _store(tmp_path)
    built = _built(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    _hold(store, built)
    inv.tree_complete(built.tree)
    inv.take_delta()
    store.evict(built.tree.tree_id)
    _, removed = inv.take_delta()
    assert ("tree", str(built.tree.tree_id)) in _keys(removed)


def test_trees_survive_a_restart_only_if_still_complete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    one, two = _built(tmp_path, "one"), _built(tmp_path, "two")
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    for built in (one, two):
        _hold(store, built)
        assert inv.tree_complete(built.tree)
    inv.save_trees()
    assert len((store.root / TREES_INDEX_NAME).read_text().split()) == 2
    # "Agent restart": a member of tree two disappears while it is down.
    store.on_change = None
    store.evict(Digest(next(h for h in two.sources if h not in one.sources)))
    again = CacheInventory(store)
    again.load()
    assert again.has_tree(one.tree.tree_id)
    assert not again.has_tree(two.tree.tree_id)
    snap = _keys(again.snapshot())
    assert ("tree", str(one.tree.tree_id)) in snap
    assert ("tree", str(two.tree.tree_id)) not in snap
    # A tampered index line is ignored, never trusted.
    (store.root / TREES_INDEX_NAME).write_text("zz\n" + "0" * 64 + "\n")
    third = CacheInventory(store)
    third.load()
    assert third.counts()["tree"] == 0


def test_snapshot_clears_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    store.put_bytes(b"x")
    assert inv.pending_count() == 1
    assert len(inv.snapshot()) == 1
    assert inv.pending_count() == 0


def test_max_trees_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    one, two = _built(tmp_path, "one"), _built(tmp_path, "two")
    inv = CacheInventory(store, max_trees=1)
    inv.load()
    store.on_change = inv.on_cas_change
    _hold(store, one)
    _hold(store, two)
    assert inv.tree_complete(one.tree) is True
    assert inv.tree_complete(two.tree) is False


def test_env_scan_and_kind_replace(tmp_path: Path) -> None:
    root = tmp_path / "envs"
    good = root / "venv" / ("a" * 64)
    good.mkdir(parents=True)
    (good / ".meshforge-env.json").write_text(
        json.dumps({"env_hash": "a" * 64, "summary": {"bytes": 123}})
    )
    wrong = root / "subprocess" / ("b" * 64)
    wrong.mkdir(parents=True)
    (wrong / ".meshforge-env.json").write_text(json.dumps({"env_hash": "c" * 64}))
    (root / "venv" / (("d" * 64) + ".building")).mkdir()
    assert scan_envs(root) == {"a" * 64: 123}
    inv = CacheInventory(_store(tmp_path))
    inv.load()
    inv.set_envs({"a" * 64: 123})
    added, _ = inv.take_delta()
    assert _keys(added) == {("env", "a" * 64)}
    inv.set_envs({})
    _, removed = inv.take_delta()
    assert _keys(removed) == {("env", "a" * 64)}


# ----------------------------------------------------------------- reporter


class _Sink:
    def __init__(self) -> None:
        self.reports: list[agent_pb2.CacheReport] = []

    async def __call__(self, name: str, payload: Any) -> str:
        assert name == "cache_report"
        self.reports.append(payload)
        return "m"


def _reporter(inv: CacheInventory, sink: _Sink, **cfg: Any) -> CacheReporter:
    return CacheReporter(
        inv,
        sink,
        CacheSettings(**{"flush_s": 0.05, **cfg}),
        node_id="node_x",
        online=lambda: True,
    )


@pytest.mark.asyncio
async def test_snapshot_is_paged_then_deltas_follow(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.put_bytes(f"blob {i}".encode())
    inv = CacheInventory(store)
    store.on_change = inv.on_cas_change
    sink = _Sink()
    reporter = _reporter(inv, sink, page_items=2)
    await reporter.start()
    try:
        reporter.request_snapshot()
        for _ in range(100):
            if len(sink.reports) >= 3:
                break
            await asyncio.sleep(0.01)
        pages = sink.reports[:3]
        assert [p.snapshot for p in pages] == [True, True, True]
        assert [p.snapshot_last for p in pages] == [False, False, True]
        assert [p.seq for p in pages] == [1, 2, 3]
        assert sum(len(p.added) for p in pages) == 5
        await asyncio.to_thread(store.put_bytes, b"new one")
        for _ in range(100):
            if len(sink.reports) >= 4:
                break
            await asyncio.sleep(0.01)
        delta = sink.reports[3]
        assert not delta.snapshot and delta.seq == 4 and len(delta.added) == 1
        # A reconnect: a new snapshot continues the same seq counter.
        reporter.request_snapshot()
        for _ in range(100):
            if len(sink.reports) >= 7:
                break
            await asyncio.sleep(0.01)
        assert [p.seq for p in sink.reports[4:7]] == [5, 6, 7]
        assert sink.reports[6].snapshot_last
    finally:
        await reporter.close()


@pytest.mark.asyncio
async def test_no_delta_before_the_first_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    store.on_change = inv.on_cas_change
    sink = _Sink()
    reporter = _reporter(inv, sink)
    await reporter.start()
    try:
        await asyncio.to_thread(store.put_bytes, b"x")
        for _ in range(3):  # three flush periods
            await asyncio.sleep(0.05)
        assert sink.reports == []
    finally:
        await reporter.close()


@pytest.mark.asyncio
async def test_delta_paging_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    sink = _Sink()
    reporter = _reporter(inv, sink, page_items=3)
    await reporter.send_snapshot()
    digests = [store.put_bytes(f"{i}".encode()).digest for i in range(4)]
    store.evict(digests[0])  # net zero for this one
    for d in digests[1:3]:
        store.evict(d)
        store.put_bytes(f"{digests.index(d)}".encode())  # back again: an add
    store.evict(digests[3])
    sent = await reporter.send_delta()
    assert sent >= 1
    for report in sink.reports[1:]:
        assert len(report.added) + len(report.removed) <= 3


def test_agent_cas_tree_staged_hook(tmp_path: Path) -> None:
    cas = AgentCas(_store(tmp_path))
    seen: list[str] = []
    built = _built(tmp_path)
    cas.tree_staged(built.tree)  # no hook: nothing happens
    cas.on_tree_complete = lambda tree: seen.append(str(tree.tree_id))
    cas.tree_staged(built.tree)
    assert seen == [str(built.tree.tree_id)]


def test_reconcile_disk_repairs_blobs_changed_behind_the_cache(tmp_path: Path) -> None:
    """G4 fix round 1: a blob unlinked (or added) behind the CAS is repaired by
    the snapshot-time re-listing, and the tree it completed is withdrawn."""
    store = _store(tmp_path)
    built = _built(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    _hold(store, built)
    assert inv.tree_complete(built.tree)
    inv.take_delta()
    member = next(iter(built.sources))
    path = store.path_for(Digest(member))
    path.chmod(0o600)
    path.unlink()  # no observer call
    store.on_change = None
    extra = store.put_bytes(b"appeared unseen").digest
    assert inv.reconcile_disk() == (1, 1)
    added, removed = inv.take_delta()
    assert ("blob", str(extra)) in _keys(added)
    assert ("blob", f"sha256:{member}") in _keys(removed)
    assert ("tree", str(built.tree.tree_id)) in _keys(removed)
    assert not inv.has_tree(built.tree.tree_id)
    assert inv.reconcile_disk() == (0, 0)  # idempotent
