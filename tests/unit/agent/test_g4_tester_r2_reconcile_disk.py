"""V0.5 G4 tester round 2: CacheInventory.reconcile_disk (fix round 1, #6).

Correctness (blob added / deleted behind the cache, racing real CAS installs and
evictions) and cost (many blobs; the event loop must not be blocked while the
re-listing runs in its worker thread).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from meshforge.agent.cache.inventory import CacheInventory, scan_cas
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest

pytestmark = pytest.mark.unit


def _hex(i: int) -> str:
    return hashlib.sha256(f"blob-{i}".encode()).hexdigest()


def _plant(store: CasStore, i: int) -> str:
    h = _hex(i)
    p = store.path_for(Digest(h))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return h


def _store(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _snap_keys(inv: CacheInventory) -> set[str]:
    return {i.key.removeprefix("sha256:") for i in inv.snapshot() if i.kind == "blob"}


def test_a_blob_added_behind_the_cache_appears_and_one_deleted_leaves(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    keep = _plant(store, 1)
    gone = _plant(store, 2)
    inv.load()  # both known
    new = _plant(store, 3)  # behind the cache
    store.path_for(Digest(gone)).unlink()  # behind the cache
    assert inv.reconcile_disk() == (1, 1)
    assert _snap_keys(inv) == {keep, new}
    assert inv.reconcile_disk() == (0, 0)  # idempotent


def test_reconcile_racing_real_installs_and_evictions_converges(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn(seed: int) -> None:
        n = 0
        try:
            while not stop.is_set():
                res = store.put_bytes(os.urandom(32) + seed.to_bytes(2, "big"))
                n += 1
                if n % 3 == 0:
                    store.evict(res.digest)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reconciler() -> None:
        try:
            while not stop.is_set():
                inv.reconcile_disk()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=churn, args=(s,)) for s in range(4)]
    ts += [threading.Thread(target=reconciler) for _ in range(2)]
    for t in ts:
        t.start()
    threading.Event().wait(2.5)
    stop.set()
    for t in ts:
        t.join(20)
    assert not errors, errors[:3]
    inv.reconcile_disk()
    assert _snap_keys(inv) == set(scan_cas(store)), "index != disk after quiet"


async def _stall_during(inv: CacheInventory) -> tuple[float, float]:
    stalls: list[float] = []
    stop = False

    async def tick() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            stalls.append(now - last - 0.005)
            last = now

    task = asyncio.create_task(tick())
    started = time.perf_counter()
    await asyncio.to_thread(inv.reconcile_disk)
    took = time.perf_counter() - started
    stop = True
    await task
    return max(stalls), took


N = 40_000 if sys.platform != "win32" else 8_000
"""Blobs for the cost tests. Windows (NTFS + Defender) creates and unlinks small
files ~10x slower: 40k blobs ran past the 120 s timeout on the first CI run
(40280010136) and the thread-method timeout then killed the whole session. 8k
blobs still make the re-listing long enough to expose a blocked event loop."""


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_steady_state_reconcile_of_many_blobs_does_not_block_the_loop(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for i in range(N):
        _plant(store, i)
    inv = CacheInventory(store)
    await asyncio.to_thread(inv.load)
    stall, took = await _stall_during(inv)
    assert stall < 0.2, f"loop stalled {stall:.3f}s (reconcile took {took:.2f}s)"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_repairing_many_blobs_does_not_block_the_loop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(N):
        _plant(store, i)
    inv = CacheInventory(store)
    await asyncio.to_thread(inv.load)
    for i in range(N):
        store.path_for(Digest(_hex(i))).unlink()  # all deleted behind the cache

    stalls: list[float] = []
    stop = False

    async def poke() -> None:  # what the reporter loop does between sleeps
        while not stop:
            started = time.perf_counter()
            inv.pending_count()
            inv.counts()
            stalls.append(time.perf_counter() - started)
            await asyncio.sleep(0.002)

    task = asyncio.create_task(poke())
    await asyncio.to_thread(inv.reconcile_disk)
    stop = True
    await task
    assert max(stalls) < 0.2, f"loop blocked {max(stalls):.3f}s on the inventory lock"
