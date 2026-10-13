# ruff: noqa: B007, E501, S110, SIM105, T201  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (630e0c1): `reconcile_disk` with a real pause between
repair batches - correctness under concurrent cache use, bounded, no deadlock
when several reconciles and real installs/evictions overlap."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from meshforge.agent.cache.inventory import CacheInventory, scan_cas
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest

pytestmark = pytest.mark.unit


def _hex(i: int) -> str:
    return hashlib.sha256(f"r2-blob-{i}".encode()).hexdigest()


def _plant(store: CasStore, i: int) -> str:
    h = _hex(i)
    p = store.path_for(Digest(h))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return h


def test_mass_repair_racing_installs_evictions_readers_and_other_reconciles(
    tmp_path: Path,
) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    inv = CacheInventory(store)
    inv.load()
    store.on_change = inv.on_cas_change
    n = 20_000
    for i in range(n):
        _plant(store, i)
    inv.load()
    assert len(scan_cas(store)) == n
    # delete 60% behind the cache's back, add 3000 behind its back
    for i in range(0, n, 5):
        pass
    for i in range(n):
        if i % 5 < 3:
            store.path_for(Digest(_hex(i))).unlink()
    for i in range(n, n + 3000):
        _plant(store, i)

    stop = threading.Event()
    errors: list[BaseException] = []
    worst_read = [0.0]
    installed: list[str] = []

    def churn(seed: int) -> None:
        try:
            k = 0
            while not stop.is_set():
                res = store.put_bytes(os.urandom(24) + seed.to_bytes(2, "big"))
                installed.append(res.digest.hex if hasattr(res, "digest") else "")
                k += 1
                if k % 3 == 0:
                    for d in list(installed[-3:]):
                        if d:
                            try:
                                store.delete(Digest(d))
                            except Exception:  # noqa: BLE001 - eviction races are fine
                                pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                t = time.perf_counter()
                inv.pending_count()
                inv.counts()
                worst_read[0] = max(worst_read[0], time.perf_counter() - t)
                time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    results: list[tuple[int, int]] = []

    def reconciler() -> None:
        try:
            results.append(inv.reconcile_disk())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    churners = [
        threading.Thread(target=churn, args=(s,), daemon=True) for s in range(2)
    ]
    rd = threading.Thread(target=reader, daemon=True)
    recs = [threading.Thread(target=reconciler, daemon=True) for _ in range(3)]
    t0 = time.perf_counter()
    for t in [*churners, rd, *recs]:
        t.start()
    for t in recs:
        t.join(120)
        assert not t.is_alive(), "reconcile deadlocked or starved"
    took = time.perf_counter() - t0
    stop.set()
    for t in [*churners, rd]:
        t.join(30)
        assert not t.is_alive()
    assert errors == [], errors
    # converge: a final quiet reconcile finds the index equal to the disk
    inv.reconcile_disk()
    assert inv.reconcile_disk() == (0, 0)
    on_disk = set(scan_cas(store))
    keys = {i.key.removeprefix("sha256:") for i in inv.snapshot() if i.kind == "blob"}
    assert keys == on_disk
    assert took < 60, took
    assert worst_read[0] < 1.0, worst_read
    print(
        f"3 concurrent reconciles of ~12000+3000 repairs: {took:.1f}s, worst read {worst_read[0] * 1000:.0f} ms"
    )
