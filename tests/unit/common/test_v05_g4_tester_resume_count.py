"""V0.5 G4 tester round 1: the in-memory resumable-transfer counter vs the disk.

Brief 0.5.4 / G1 nit: the cap is counted in memory. Whatever races (opens,
discards, TTL sweeps), once everything is quiet the counter must equal the
number of sidecars on disk - a drifting counter either refuses transfers forever
or lets the cap be exceeded.
"""

from __future__ import annotations

import os
import random
import threading
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.hashing import sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE


def _on_disk(store: CasStore) -> int:
    return sum(1 for p in store.tmp.iterdir() if p.name.endswith(".part.chunks"))


def test_counter_equals_the_sidecars_on_disk_after_racing_open_discard_sweep(
    tmp_path: Path,
) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable=64)
    store.prepare()
    store._resume_ttl_s = 0.0  # every transfer is instantly "old"
    blobs = [os.urandom(2 * CS) + bytes([i]) for i in range(8)]
    stop = threading.Event()
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        rnd = random.Random(seed)  # noqa: S311  (test jitter, not crypto)
        while not stop.is_set():
            data = rnd.choice(blobs)  # noqa: S311
            try:
                part = store.resumable(sha256_bytes(data), len(data), CS)
                if rnd.random() < 0.5:  # noqa: S311
                    part.discard()
            except FileNotFoundError:
                pass  # the open/sweep race (DEFECT-5 in the report) - tolerated here
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def sweeper() -> None:
        while not stop.is_set():
            store.sweep_resumes()

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
    threads.append(threading.Thread(target=sweeper))
    for t in threads:
        t.start()
    threading.Event().wait(1.5)
    stop.set()
    for t in threads:
        t.join(10)
    assert not errors, errors[:3]
    assert store.resumes_in_progress() == _on_disk(store), (
        store.resumes_in_progress(),
        _on_disk(store),
    )
    assert store.resumes_in_progress() <= 64


def test_concurrent_openers_never_exceed_the_cap(tmp_path: Path) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable=5)
    store.prepare()
    blobs = [os.urandom(2 * CS) + bytes([i]) for i in range(40)]
    opened: list[object] = []
    refused: list[object] = []
    lock = threading.Lock()

    def go(data: bytes) -> None:
        try:
            part = store.resumable(sha256_bytes(data), len(data), CS)
        except Exception as exc:  # noqa: BLE001
            with lock:
                refused.append(exc)
        else:
            with lock:
                opened.append(part)

    threads = [threading.Thread(target=go, args=(b,)) for b in blobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(opened) == 5 and len(refused) == 35
    assert _on_disk(store) == 5 == store.resumes_in_progress()
