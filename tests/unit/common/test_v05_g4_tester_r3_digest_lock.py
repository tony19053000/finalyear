"""G4 tester round 3: per-digest lock (lock order, parallel upload+fetch, crash, cost)."""

from __future__ import annotations

import multiprocessing as mp
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.hashing import sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE
_R32 = (
    "DEFECT-R3-2: ext4/tmpfs reuse the inode number after unlink+create, so "
    "(st_dev, st_ino) cannot tell a restarted part from the old one"
)
SRC = Path(__file__).resolve().parents[3] / "src" / "meshforge"


def _store(root: Path) -> CasStore:
    s = CasStore(root, max_blob_bytes=1 << 30)
    s.prepare()
    return s


def test_no_path_takes_digest_lock_inside_resume_lock() -> None:
    """Static lock-order check: no digest_lock acquisition lexically nested in resume_lock."""
    for path in SRC.rglob("*.py"):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"(\s*)with .*resume_lock\s*:\s*$", line)
            if not m or ("digest_lock" in line.split("resume_lock")[0] and False):
                continue
            if "digest_lock" in line and line.index("digest_lock") > line.index(
                "resume_lock"
            ):
                pytest.fail(f"{path}:{i + 1} resume_lock before digest_lock")
            indent = len(m.group(1))
            for nxt in lines[i + 1 :]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                assert "digest_lock" not in nxt, (
                    f"{path}: digest_lock inside resume_lock"
                )


_STALL_S = 30.0
"""No uploader finished one store call for this long = a deadlock."""
_TOTAL_S = 240.0
"""All six uploads take longer than this = a livelock (or a dead machine)."""


def _stacks(threads: list[threading.Thread]) -> str:
    """Where each still-running thread of ``threads`` stands (for a failure)."""
    frames = sys._current_frames()
    out: list[str] = []
    for t in threads:
        frame = frames.get(t.ident or -1)
        if t.is_alive() and frame is not None:
            out.append(f"--- {t.name}\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(out)


def test_upload_and_fetch_of_the_same_digest_in_parallel(tmp_path: Path) -> None:
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 5 + 17)
    digest = sha256_bytes(data)
    parts = [data[i : i + CS] for i in range(0, len(data), CS)]
    errors: list[BaseException] = []
    done = threading.Event()
    steps: list[int] = []  # one entry per finished store call of any uploader

    def uploader(order: list[int]) -> None:
        try:
            p = store.resumable(digest, len(data), CS)
            steps.append(1)
            for i in order:
                p.write_chunk(i, parts[i], sha256_bytes(parts[i]).hex)
                steps.append(1)
            p.complete()
            steps.append(1)
        except CasError:
            pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    deadline = time.monotonic() + 60.0

    def reader() -> None:
        # Bounded (D157): whatever happens to the uploaders or the asserts
        # below, this loop ends by itself - a failure never hangs pytest.
        while not done.is_set() and time.monotonic() < deadline:
            try:
                if store.has(digest):
                    with store.open_read(digest) as fh:
                        assert sha256_bytes(fh.read()) == digest
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return  # one failure is enough; do not spin on it

    # Daemon threads: a thread wedged in a deadlock cannot keep the
    # interpreter alive after the test failed.
    r = threading.Thread(target=reader, daemon=True)
    r.start()
    fwd = list(range(len(parts)))
    ts = [
        threading.Thread(
            target=uploader, args=(fwd[::-1] if k % 2 else fwd,), daemon=True
        )
        for k in range(6)
    ]
    try:
        for t in ts:
            t.start()
        # A deadlock is "no uploader finishes a store call any more", not "the
        # whole upload took long" (V0.7 G6 fix round 1). The reader above spins
        # on the GIL, so every OS call of an uploader waits for it: measured
        # here 0.03-0.07 s for the six uploads without the reader, 5-15 s with
        # it on an idle machine and above 30 s on a throttled CPU - the old
        # bare 30 s join then reported a deadlock that was not one. Now: fail
        # when no call finished for _STALL_S (a single call is a handful of
        # system calls), or when the uploads are not over after _TOTAL_S; the
        # failure names where every thread stands.
        started = last_step = time.monotonic()
        seen = 0
        while any(t.is_alive() for t in ts):
            time.sleep(0.2)
            now = time.monotonic()
            if len(steps) != seen:
                seen, last_step = len(steps), now
            stalled = now - last_step > _STALL_S
            if stalled or now - started > _TOTAL_S:
                why = (
                    f"no store call finished for {_STALL_S:.0f} s"
                    if stalled
                    else f"uploads not over after {_TOTAL_S:.0f} s"
                )
                pytest.fail(
                    f"deadlock between uploaders: {why} ({seen} calls done)\n"
                    + _stacks(ts)
                )
    finally:
        done.set()
        r.join(10)
    assert not r.is_alive(), "the reader did not stop"
    assert not errors, errors[:3]
    assert store.has(digest)
    assert store.resumes_in_progress() == 0


def _die_holding(root: str, hex_: str, ready: object) -> None:
    s = CasStore(Path(root), max_blob_bytes=1 << 30)
    with s.digest_lock(hex_):
        ready.set()  # type: ignore[attr-defined]
        time.sleep(0.2)
        os._exit(9)


def test_process_killed_holding_the_digest_lock_does_not_wedge_next_start(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cas"
    store = _store(root)
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_die_holding, args=(str(root), digest.hex, ready))
    proc.start()
    assert ready.wait(30)
    proc.join(30)
    assert proc.exitcode == 9
    names = [p.name for p in root.rglob("*") if "lock" in p.name.lower()]
    assert names == [], f"lock files left behind: {names}"
    fresh = _store(root)
    result: list[object] = []

    def go() -> None:
        p = fresh.resumable(digest, len(data), CS)
        for i in range(2):
            c = data[i * CS : (i + 1) * CS]
            p.write_chunk(i, c, sha256_bytes(c).hex)
        result.append(p.complete())

    t = threading.Thread(target=go, daemon=True)  # a wedge must not hang pytest
    t.start()
    t.join(30)
    assert result, "next start wedged"
    del store


def test_digest_lock_table_is_released_after_use(tmp_path: Path) -> None:
    s = _store(tmp_path / "cas")
    for n in range(200):
        with s.digest_lock(f"{n:064x}"):
            pass
    assert s._digest_locks == {}


def test_many_small_distinct_blobs_throughput_is_sane(tmp_path: Path) -> None:
    s = _store(tmp_path / "cas")
    n = 200
    blobs = [
        os.urandom(1000) + n.to_bytes(4, "big") + i.to_bytes(4, "big") for i in range(n)
    ]
    t0 = time.perf_counter()
    for b in blobs:
        d = sha256_bytes(b)
        p = s.resumable(d, len(b), CS)
        p.write_chunk(0, b, d.hex)
        p.complete()
    dt = time.perf_counter() - t0
    assert s.resumes_in_progress() == 0
    # Dominated by fsync; the lock itself must add no more than a generous bound.
    assert dt / n < 0.25, f"{dt / n * 1000:.1f} ms per blob"
    lock_t0 = time.perf_counter()
    for i in range(20000):
        with s.digest_lock(f"{i:064x}"):
            pass
    assert (time.perf_counter() - lock_t0) / 20000 < 0.0005


def _restart(store: CasStore, digest, data: bytes):
    """Another owner discards the transfer and opens a fresh one (new inode)."""
    store.resumable(digest, len(data), CS).discard()
    return store.resumable(digest, len(data), CS)


def test_stale_owner_cannot_write_into_a_restarted_transfer(tmp_path: Path) -> None:
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    a = store.resumable(digest, len(data), CS)
    b = _restart(store, digest, data)
    with pytest.raises(CasError):
        a.write_chunk(0, data[:CS], sha256_bytes(data[:CS]).hex)
    assert b.missing() == [0, 1]
    part, _ = store.resume_paths(digest)
    assert part.read_bytes() == bytes(len(data))  # nothing leaked into B's file


def test_stale_owner_cannot_complete_a_restarted_transfer(tmp_path: Path) -> None:
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    a = store.resumable(digest, len(data), CS)
    for i in range(2):
        c = data[i * CS : (i + 1) * CS]
        a.write_chunk(i, c, sha256_bytes(c).hex)
    b = _restart(store, digest, data)
    with pytest.raises(CasError):
        a.complete()
    assert not store.has(digest)
    assert b.missing() == [0, 1] and store.resumes_in_progress() == 1


def test_stale_owner_is_refused_when_the_inode_differs(tmp_path: Path) -> None:
    """Old inode kept alive by a hard link, so the new part gets a new inode."""
    store = _store(tmp_path / "cas")
    data = os.urandom(CS * 2)
    digest = sha256_bytes(data)
    a = store.resumable(digest, len(data), CS)
    part, _ = store.resume_paths(digest)
    os.link(part, tmp_path / "keep")
    b = _restart(store, digest, data)
    assert os.stat(part).st_ino != os.stat(tmp_path / "keep").st_ino
    with pytest.raises(CasError):
        a.write_chunk(0, data[:CS], sha256_bytes(data[:CS]).hex)
    c0, c1 = data[:CS], data[CS:]
    b.write_chunk(0, c0, sha256_bytes(c0).hex)
    b.write_chunk(1, c1, sha256_bytes(c1).hex)
    with pytest.raises(CasError):
        a.complete()
    assert b.complete().digest == digest
