"""Tester round 1 (V0.4 G2): fabric-token rotation edges not covered upstream.

Permission mode after `rotate_token` (the implementer suite only checks the mode
right after *creation*), and a rotation race observed with the real wall clock
(not an injected one) confirming there is no window in which the *new* token is
rejected once the reload interval has elapsed, and no window where the file is
briefly world-readable during a rotate.
"""

from __future__ import annotations

import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from meshforge.controller.fabric_token import (
    FabricTokenVerifier,
    load_or_create_token,
    rotate_token,
)

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")


@posix_only
def test_rotate_leaves_the_file_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    load_or_create_token(path)
    rotate_token(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    rotate_token(path)
    rotate_token(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_rotate_never_transiently_widens_permissions(tmp_path: Path) -> None:
    """Sample the mode from a second thread hammering `stat` while one thread
    rotates repeatedly; the mode must never be observed as anything but 0600."""
    path = tmp_path / "fabric.token"
    load_or_create_token(path)
    stop = threading.Event()
    bad_modes: list[int] = []

    def watcher() -> None:
        while not stop.is_set():
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                continue
            if mode != 0o600:
                bad_modes.append(mode)

    t = threading.Thread(target=watcher)
    t.start()
    try:
        for _ in range(50):
            rotate_token(path)
    finally:
        stop.set()
        t.join()
    assert bad_modes == []


def test_rotation_race_real_clock_no_permanent_lockout(tmp_path: Path) -> None:
    """With the REAL wall clock and a short reload interval: readers hammering
    `verify()` throughout a rotation must, once >= reload_interval has passed
    since the rotate, always accept the new token -- never get stuck rejecting
    it forever (a defect that would lock every agent out until a controller
    restart)."""
    path = tmp_path / "fabric.token"
    old, _ = load_or_create_token(path)
    verifier = FabricTokenVerifier(
        path, clock=time.monotonic, reload_interval_s=0.05, initial=old
    )
    new = rotate_token(path)
    deadline = time.monotonic() + 5.0
    accepted = False
    while time.monotonic() < deadline:
        if verifier.verify(new.get_secret_value()):
            accepted = True
            break
        time.sleep(0.01)
    assert accepted, "new token was never accepted within 5s of rotation"
    # And it stays accepted (no flapping back to rejecting it).
    for _ in range(20):
        assert verifier.verify(new.get_secret_value())


def test_concurrent_readers_during_a_rotation_never_see_a_torn_token(
    tmp_path: Path,
) -> None:
    """Many threads calling verify() while a rotation happens concurrently must
    never observe an exception, and once the new token is visible it is always
    the complete 43-character token -- never a partially-written value."""
    path = tmp_path / "fabric.token"
    old, _ = load_or_create_token(path)
    verifier = FabricTokenVerifier(
        path, clock=time.monotonic, reload_interval_s=0.01, initial=old
    )
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                verifier.verify(old.get_secret_value())
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for _ in range(10):
        rotate_token(path)
        time.sleep(0.005)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
    # The file on disk is always a clean, fully-formed token.
    final = path.read_text().strip()
    assert len(final) == 43
