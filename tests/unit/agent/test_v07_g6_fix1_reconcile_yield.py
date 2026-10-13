"""V0.7 G6 fix round 1 (exit list): ``reconcile_disk`` really hands the lock over.

``threading.Lock`` is not fair. With ``time.sleep(0)`` between its batches the
repairing thread re-took the inventory lock before the waiting event loop woke
up, many batches in a row: ``test_repairing_many_blobs_does_not_block_the_loop``
measured 0.24-0.35 s stalls (limit 0.2 s) 3/3 alone on a laptop on battery
(``powersave``), and passed on mains. The stall test depends on the CPU's
state; this one pins the cause on any machine.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from meshforge.agent.cache import inventory as inventory_module
from meshforge.agent.cache.inventory import CacheInventory
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest

pytestmark = pytest.mark.unit


def _plant(store: CasStore, i: int) -> Path:
    digest = Digest(hashlib.sha256(f"blob-{i}".encode()).hexdigest())
    path = store.path_for(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _inventory(tmp_path: Path, count: int) -> tuple[CacheInventory, list[Path]]:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    paths = [_plant(store, i) for i in range(count)]
    inv = CacheInventory(store)
    inv.load()
    return inv, paths


def test_the_pause_between_repair_batches_is_a_real_sleep() -> None:
    assert 0.0001 <= inventory_module._REPAIR_YIELD_S <= 0.005


@pytest.mark.parametrize("direction", ["removed", "added"])
def test_every_repair_batch_is_followed_by_a_pause_with_the_lock_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    batches = 3
    count = inventory_module._REPAIR_BATCH * batches
    inv, paths = _inventory(tmp_path, count if direction == "removed" else 0)
    if direction == "removed":
        for path in paths:
            path.unlink()  # deleted behind the cache
    else:
        for i in range(count):
            _plant(inv._store, i)  # appeared behind the cache

    pauses: list[tuple[float, bool]] = []

    def fake_sleep(seconds: float) -> None:
        # Another thread (the event loop in production) can take the lock now.
        got: list[bool] = []
        probe = threading.Thread(
            target=lambda: got.append(inv._lock.acquire(timeout=5) and _release(inv))
        )
        probe.start()
        probe.join()
        pauses.append((seconds, got == [True]))

    monkeypatch.setattr(inventory_module.time, "sleep", fake_sleep)
    result = inv.reconcile_disk()

    assert result == ((0, count) if direction == "removed" else (count, 0))
    assert len(pauses) == batches
    assert all(seconds > 0 for seconds, _ in pauses), pauses
    assert all(free for _, free in pauses), "a pause was taken with the lock held"


def _release(inv: CacheInventory) -> bool:
    inv._lock.release()
    return True
