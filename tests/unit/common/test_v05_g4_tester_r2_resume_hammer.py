"""V0.5 G4 tester round 2: hammer the resumable-transfer paths (fix round 1, #2/#3).

open / write_chunk / complete / discard / sweep from many threads (and, for the
typed-error property, from several processes sharing one CAS directory).

Properties: nothing but ``CasError`` ever escapes (never a raw OSError /
FileNotFoundError); every installed blob verifies; once quiet the in-memory
counter equals the sidecars on disk; the cap is never exceeded while running.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import random
import threading
from pathlib import Path

import pytest

from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.hashing import Digest, sha256_bytes

pytestmark = pytest.mark.unit
CS = MIN_CHUNK_SIZE
CAP = 6


def _on_disk(store: CasStore) -> int:
    return sum(1 for p in store.tmp.iterdir() if p.name.endswith(".part.chunks"))


def _chunks(data: bytes) -> list[bytes]:
    return [data[i : i + CS] for i in range(0, len(data), CS)]


def _churn(
    store: CasStore,
    blobs: list[bytes],
    seed: int,
    stop: threading.Event,
    errors: list[BaseException],
    installed: set[str],
) -> None:
    rnd = random.Random(seed)  # noqa: S311
    while not stop.is_set():
        data = rnd.choice(blobs)
        digest = sha256_bytes(data)
        try:
            part = store.resumable(digest, len(data), CS)
            parts = _chunks(data)
            order = list(range(len(parts)))
            rnd.shuffle(order)
            action = rnd.random()
            for i in order:
                part.write_chunk(i, parts[i], sha256_bytes(parts[i]).hex)
                if action < 0.2:
                    part.discard()  # abandon midway
                    break
            else:
                if action < 0.8:
                    part.complete()
                    installed.add(digest.hex)
                else:
                    part.discard()
        except CasError:
            pass  # typed refusals are fine (cap, swept, integrity)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)


def test_threads_hammer_open_write_complete_discard_sweep() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = CasStore(Path(tmp) / "cas", max_blob_bytes=1 << 30, max_resumable=CAP)
        store.prepare()
        blobs = [os.urandom(3 * CS - 5) + bytes([i]) for i in range(10)]
        stop = threading.Event()
        errors: list[BaseException] = []
        installed: set[str] = set()
        over_cap: list[int] = []

        def sweeper() -> None:
            rnd = random.Random(1)  # noqa: S311
            while not stop.is_set():
                store._resume_ttl_s = rnd.choice([0.0, 0.0, 600.0])
                store.sweep_resumes()

        def watcher() -> None:
            while not stop.is_set():
                n = store.resumes_in_progress()
                if n > CAP:
                    over_cap.append(n)

        ts = [
            threading.Thread(
                target=_churn, args=(store, blobs, s, stop, errors, installed)
            )
            for s in range(8)
        ]
        ts += [threading.Thread(target=sweeper), threading.Thread(target=watcher)]
        for t in ts:
            t.start()
        threading.Event().wait(4.0)
        stop.set()
        for t in ts:
            t.join(20)
        assert not errors, [repr(e) for e in errors[:5]]
        assert not over_cap, over_cap[:5]
        store._resume_ttl_s = 600.0
        # (Counter == sidecars is NOT asserted here: complete() racing open() of
        # the same digest drifts it - DEFECT-R2-3, xfail below. open/discard/sweep
        # alone are exact: test_v05_g4_tester_resume_count.)
        assert installed, "nothing completed; the hammer proved nothing"
        for hex_ in installed:
            assert store.verify(Digest(hex_))


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R2-2: complete() hashes the part, then installs it by path; a "
    "concurrent discard()+open() of the same digest swaps in a fresh zero-filled "
    "part in between, so a blob NOT matching its digest is installed",
)
def test_same_digest_complete_vs_discard_never_installs_a_wrong_blob() -> None:
    """Many uploaders of ONE blob; some abandon (discard). Whatever interleaving,
    the CAS must never hold a file whose hash is not its name (retry up to 8 x
    1.5 s windows: the defect appears in most windows)."""
    import tempfile

    for _trial in range(8):
        with tempfile.TemporaryDirectory() as tmp:
            store = CasStore(
                Path(tmp) / "cas", max_blob_bytes=1 << 30, max_resumable=CAP
            )
            store.prepare()
            data = os.urandom(4 * CS + 1)
            digest = sha256_bytes(data)
            errors: list[BaseException] = []
            stop = threading.Event()

            def go(
                seed: int,
                errs: list[BaseException] = errors,
                store: CasStore = store,
                data: bytes = data,
                digest: Digest = digest,
                stop: threading.Event = stop,
            ) -> None:
                rnd = random.Random(seed)  # noqa: S311
                while not stop.is_set():
                    try:
                        part = store.resumable(digest, len(data), CS)
                        for i, c in enumerate(_chunks(data)):
                            part.write_chunk(i, c, sha256_bytes(c).hex)
                        (part.complete if rnd.random() < 0.7 else part.discard)()
                    except CasError:
                        pass
                    except BaseException as exc:  # noqa: BLE001
                        errs.append(exc)

            ts = [threading.Thread(target=go, args=(s,)) for s in range(6)]
            for t in ts:
                t.start()
            threading.Event().wait(1.5)
            stop.set()
            for t in ts:
                t.join(20)
            assert not errors, [repr(e) for e in errors[:5]]
            final = store.path_for(digest)
            if final.exists():
                assert sha256_bytes(final.read_bytes()) == digest, (
                    "wrong blob installed"
                )
            assert store.resumes_in_progress() == _on_disk(store)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R2-3: complete() installs the part and only then unlinks the "
    "sidecar/forgets under the lock; an open() of the same digest in between "
    "re-creates part+sidecar, and its next write re-saves a sidecar the counter "
    "no longer counts (counter 0, sidecar 1)",
)
def test_same_digest_completes_only_counter_equals_sidecars() -> None:
    import tempfile

    for _trial in range(12):
        with tempfile.TemporaryDirectory() as tmp:
            store = CasStore(
                Path(tmp) / "cas", max_blob_bytes=1 << 30, max_resumable=CAP
            )
            store.prepare()
            data = os.urandom(4 * CS + 1)
            digest = sha256_bytes(data)
            errors: list[BaseException] = []
            stop = threading.Event()

            def go(
                errs: list[BaseException] = errors,
                store: CasStore = store,
                data: bytes = data,
                digest: Digest = digest,
                stop: threading.Event = stop,
            ) -> None:
                while not stop.is_set():
                    try:
                        part = store.resumable(digest, len(data), CS)
                        for i, c in enumerate(_chunks(data)):
                            part.write_chunk(i, c, sha256_bytes(c).hex)
                        part.complete()
                    except CasError:
                        pass
                    except BaseException as exc:  # noqa: BLE001
                        errs.append(exc)

            ts = [threading.Thread(target=go) for _ in range(6)]
            for t in ts:
                t.start()
            threading.Event().wait(1.0)
            stop.set()
            for t in ts:
                t.join(20)
            assert not errors, [repr(e) for e in errors[:5]]
            assert store.verify(digest)  # completes alone never corrupt
            assert store.resumes_in_progress() == _on_disk(store)


def _proc(root: str, seed: int, seconds: float, q: mp.Queue[object]) -> None:
    import time

    store = CasStore(Path(root), max_blob_bytes=1 << 30, max_resumable=1000)
    store._resume_ttl_s = 0.0
    blobs = [random.Random(i).randbytes(3 * CS - 5) for i in range(6)]  # noqa: S311
    stop = threading.Event()
    errors: list[BaseException] = []
    installed: set[str] = set()
    th = threading.Thread(
        target=_churn, args=(store, blobs, seed, stop, errors, installed)
    )
    sw = threading.Thread(
        target=lambda: [store.sweep_resumes() for _ in iter(stop.is_set, True)]
    )
    th.start()
    sw.start()
    time.sleep(seconds)
    stop.set()
    th.join(20)
    sw.join(20)
    q.put([repr(e) for e in errors])


def test_processes_sharing_one_cas_dir_only_ever_see_typed_errors() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cas"
        CasStore(root, max_blob_bytes=1 << 30).prepare()
        ctx = mp.get_context("spawn")
        q: mp.Queue[object] = ctx.Queue()
        procs = [
            ctx.Process(target=_proc, args=(str(root), s, 3.0, q)) for s in range(3)
        ]
        for p in procs:
            p.start()
        results = [q.get(timeout=90) for _ in procs]
        for p in procs:
            p.join(30)
            assert p.exitcode == 0
        assert all(r == [] for r in results), results
        store = CasStore(root, max_blob_bytes=1 << 30)
        for i in range(6):
            data = random.Random(i).randbytes(3 * CS - 5)  # noqa: S311
            if store.has(sha256_bytes(data)):
                assert store.verify(sha256_bytes(data))


def test_complete_after_the_part_vanished_is_a_typed_error_and_a_fresh_open_works(
    tmp_path: Path,
) -> None:
    """Deterministic form of the sweep-under-complete race (fix round 1, #2)."""
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable=CAP)
    store.prepare()
    data = os.urandom(2 * CS + 3)
    digest = sha256_bytes(data)
    part = store.resumable(digest, len(data), CS)
    for i, c in enumerate(_chunks(data)):
        part.write_chunk(i, c, sha256_bytes(c).hex)
    part.part.unlink()  # swept / discarded by someone else
    with pytest.raises(CasError):
        part.complete()
    # The sidecar still says "all done" but the part is gone: a new open starts over.
    again = store.resumable(digest, len(data), CS)
    assert again.missing() == list(range(again.count))
    for i, c in enumerate(_chunks(data)):
        again.write_chunk(i, c, sha256_bytes(c).hex)
    again.complete()
    assert store.verify(digest)
    assert store.resumes_in_progress() == _on_disk(store) == 0


def test_part_replaced_by_a_directory_or_fifo_is_typed_not_raw(tmp_path: Path) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable=CAP)
    store.prepare()
    data = os.urandom(CS + 1)
    digest = sha256_bytes(data)
    part = store.resumable(digest, len(data), CS)
    path = part.part
    path.unlink()
    path.mkdir()  # a directory where the part was
    with pytest.raises(CasError):
        part.write_chunk(0, data[:CS], None)
    again = None
    with contextlib.suppress(CasError):  # but never a raw OSError
        again = store.resumable(digest, len(data), CS)
    assert again is None or again.missing()
