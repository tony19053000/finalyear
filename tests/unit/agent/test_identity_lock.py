"""Task 0.3.2 fix round: cross-process lock on the identity directory (ADR-0013 §5/§6).

A real second OS process (``multiprocessing`` spawn) holds ``identity.lock`` while
the parent calls into the identity module; no mocks of the lock. Nothing depends on
this machine's user name or hostname.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent import identity as ident
from meshforge.agent.identity import (
    IdentityError,
    load_or_create,
    next_epoch,
    reset,
)
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.filelock import locked

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX permission bits"
)


@pytest.fixture
def id_dir(tmp_path: Path) -> Path:
    return tmp_path / "identity"


# ------------------------------------------------------------- a real lock holder


# The parent and the child talk over a ``Pipe``, not ``multiprocessing.Event``s:
# an Event is five named POSIX semaphores whose finalizers unregister them with the
# multiprocessing resource tracker. ``pytest.raises(...) as info`` makes a
# frame -> exception -> traceback -> frame cycle that keeps the yielded Event alive
# until a later cyclic GC, and when that GC fires inside the next test's
# ``ctx.Event()`` (the tracker is mid-``_send`` under its lock) every finalizer
# re-enters the tracker: "ReentrantCallError ... semaphore might leak", raised by
# pytest as 5 unraisable exceptions at the next setup (CI run 40733787742). A pipe
# is two plain fds: nothing registers with the tracker, nothing can re-enter it.
_HELD = "held"


def _hold_lock(lock_path: str, conn: Any) -> None:
    """Child: take the lock, report ``held``, keep it until the parent sends
    anything or closes its end (``poll`` returns on EOF as well)."""
    with locked(Path(lock_path), timeout_s=5.0):
        conn.send(_HELD)
        conn.poll(30)
    conn.close()


class _Release:
    """What the fixture yields: ``set()`` lets the child release the lock
    (idempotent, like the ``Event`` it replaces)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def set(self) -> None:
        if not self._conn.closed:
            self._conn.send("release")
            self._conn.close()


@pytest.fixture
def lock_holder(id_dir: Path) -> Iterator[Any]:
    """A child process holding ``identity.lock``; yields the handle that releases it."""
    load_or_create(id_dir)  # identity + lock file exist
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(
        target=_hold_lock, args=(str(id_dir / ident.LOCK_FILE_NAME), child_conn)
    )
    proc.start()
    child_conn.close()  # the child has its own copy; EOF reaches it if we die
    release = _Release(parent_conn)
    try:
        assert parent_conn.poll(30), "child never acquired the lock"
        assert parent_conn.recv() == _HELD
        yield release
    finally:
        release.set()
        proc.join(timeout=10)
        assert proc.exitcode == 0
        proc.close()


def test_lock_held_by_another_process_gives_identity_error_after_bounded_wait(
    id_dir: Path, lock_holder: Any
) -> None:
    started = time.monotonic()
    with pytest.raises(IdentityError, match="locked by another process") as info:
        load_or_create(id_dir, lock_timeout_s=0.2)
    waited = time.monotonic() - started
    assert 0.2 <= waited < 3.0, waited  # bounded: did wait, did not hang
    assert info.value.path == id_dir / ident.LOCK_FILE_NAME
    assert "stop it, or wait and retry" in info.value.remedy
    assert "PRIVATE KEY" not in str(info.value)


def test_lock_blocks_next_epoch_and_reset_too(id_dir: Path, lock_holder: Any) -> None:
    identity = ident._load(id_dir, id_dir / "node.key", id_dir / "node.json")
    with pytest.raises(IdentityError, match="locked by another process"):
        next_epoch(identity, lock_timeout_s=0.05)
    with pytest.raises(IdentityError, match="locked by another process"):
        reset(id_dir, lock_timeout_s=0.05)
    # Nothing was changed by the refused calls.
    assert json.loads((id_dir / "node.json").read_text())["session_epoch"] == 0
    assert (id_dir / "node.key").exists()


def test_lock_released_then_everything_succeeds(id_dir: Path, lock_holder: Any) -> None:
    lock_holder.set()  # child releases and exits
    deadline = time.monotonic() + 5
    while True:  # poll instead of a fixed sleep
        try:
            identity = load_or_create(id_dir, lock_timeout_s=0.05)
            break
        except IdentityError:
            assert time.monotonic() < deadline, "lock never released"
            time.sleep(0.01)
    assert next_epoch(identity, lock_timeout_s=0.05).session_epoch == 1
    reset(id_dir, lock_timeout_s=0.05)
    assert not (id_dir / "node.key").exists()


# -------------------------------------------------------------- lock file rules


def test_lock_file_is_created_protected_and_survives_reset(id_dir: Path) -> None:
    load_or_create(id_dir)
    lock = id_dir / ident.LOCK_FILE_NAME
    assert lock.exists() and lock.stat().st_size == 0
    if os.name == "posix":
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    reset(id_dir)
    assert lock.exists(), "reset must never delete the lock file (waiter safety)"
    assert sorted(p.name for p in id_dir.iterdir()) == [ident.LOCK_FILE_NAME]
    # A fresh identity can still be minted afterwards, reusing the same lock file.
    assert load_or_create(id_dir).session_epoch == 0


def test_reset_on_missing_directory_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "never-existed"
    reset(missing)
    assert not missing.exists()


def test_unopenable_lock_file_is_an_identity_error(id_dir: Path) -> None:
    id_dir.mkdir()
    (id_dir / ident.LOCK_FILE_NAME).mkdir()  # a directory where the lock should be
    with pytest.raises(
        IdentityError, match="could not open identity lock file"
    ) as info:
        load_or_create(id_dir)
    assert info.value.path == id_dir / ident.LOCK_FILE_NAME


# ------------------------------------------------------- exclusive atomic write


def test_atomic_exclusive_refuses_to_overwrite_and_leaves_no_debris(
    tmp_path: Path,
) -> None:
    target = tmp_path / "f"
    atomic_write_bytes(target, b"one", exclusive=True)
    with pytest.raises(FileExistsError):
        atomic_write_bytes(target, b"two", exclusive=True)
    assert target.read_bytes() == b"one"
    atomic_write_bytes(target, b"three")  # non-exclusive still overwrites
    assert target.read_bytes() == b"three"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_atomic_exclusive_failed_replace_removes_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f"

    def boom(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="No space left"):
        atomic_write_bytes(target, b"x", exclusive=True)
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []  # neither the claim nor the temp file


@posix_only
def test_atomic_exclusive_claim_keeps_the_requested_mode(tmp_path: Path) -> None:
    target = tmp_path / "f"
    atomic_write_bytes(target, b"x", mode=0o600, exclusive=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ---------------------------------------------------------------- epoch bound (D3)


@pytest.mark.parametrize("epoch", [2**64, 2**64 + 1, 2**70])
def test_session_epoch_beyond_uint64_is_refused_on_load(
    id_dir: Path, epoch: int
) -> None:
    load_or_create(id_dir)
    path = id_dir / "node.json"
    meta = json.loads(path.read_text())
    meta["session_epoch"] = epoch
    path.write_text(json.dumps(meta))
    with pytest.raises(IdentityError, match="session_epoch") as info:
        load_or_create(id_dir)
    assert info.value.path == path
    assert "identity reset" in info.value.remedy


def test_session_epoch_at_uint64_max_loads_but_cannot_advance(id_dir: Path) -> None:
    load_or_create(id_dir)
    path = id_dir / "node.json"
    meta = json.loads(path.read_text())
    meta["session_epoch"] = ident.MAX_SESSION_EPOCH
    path.write_text(json.dumps(meta))
    identity = load_or_create(id_dir)
    assert identity.session_epoch == 2**64 - 1
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    agent_pb2.Hello(session_epoch=identity.session_epoch)  # encodable
    with pytest.raises(IdentityError, match="exhausted"):
        next_epoch(identity)
    assert json.loads(path.read_text())["session_epoch"] == 2**64 - 1  # unchanged


# ------------------------------------------------- reviewer findings (G1 review)
def test_next_epoch_refuses_to_clobber_an_identity_reset_underneath_it(
    tmp_path: Path,
) -> None:
    """Blocking finding: ``reset`` + fresh ``load_or_create`` while an old
    ``NodeIdentity`` is still in memory; ``next_epoch(old)`` must not write the
    old node_id/fingerprint next to the new key (that wedges the node)."""
    d = tmp_path / "id"
    old = load_or_create(d)
    reset(d)
    new = load_or_create(d)
    assert new.node_id != old.node_id
    before = (d / "node.json").read_bytes()
    with pytest.raises(IdentityError, match="changed underneath"):
        next_epoch(old)
    assert (d / "node.json").read_bytes() == before
    # the fresh identity is still perfectly loadable and advanceable
    assert load_or_create(d).node_id == new.node_id
    assert next_epoch(new).session_epoch == 1


def test_an_oserror_in_the_locked_body_is_not_blamed_on_the_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "id"
    load_or_create(d)

    def boom(*_: object, **__: object) -> None:
        raise PermissionError(13, "simulated body failure")

    monkeypatch.setattr(ident, "_check_protected", boom)
    with pytest.raises(PermissionError, match="simulated body failure"):
        load_or_create(d)


def test_temp_debris_from_a_killed_writer_is_swept_under_the_lock(
    tmp_path: Path,
) -> None:
    d = tmp_path / "id"
    load_or_create(d)
    debris = d / ".node.json.deadbeef.tmp"
    debris.write_bytes(b"half-written")
    load_or_create(d)
    assert not debris.exists()
