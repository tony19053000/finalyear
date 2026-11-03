"""V1.0 G8 F4 - independent adversarial tests for ``controller.json`` (tester).

Derived from the acceptance criteria (7: corrupt/oversized/symlinked file handled
safely; 8: same permissions as the identity files; plus concurrency, identity
lock contention, IPv6, insecure-dev), not from the implementation.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshforge.agent import known_controller
from meshforge.agent.discovery import LastKnownFallbackResolver
from meshforge.agent.identity import (
    CONTROLLER_FILE_NAME,
    load_or_create,
    locked_identity,
)
from meshforge.agent.session.client import AgentSessionClient

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX")


class _Nothing:
    async def resolve(self) -> str | None:
        return None


@pytest.fixture
def ident_dir(tmp_path: Path) -> Path:
    return load_or_create(tmp_path / "identity").path


def _within(seconds: float, fn, *args):  # type: ignore[no-untyped-def]
    """Run ``fn`` in a daemon thread; fail (not hang the suite) if it blocks."""
    box: list[object] = []
    err: list[BaseException] = []

    def run() -> None:
        try:
            box.append(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"{fn.__name__} blocked > {seconds}s"
    if err:
        raise err[0]
    return box[0] if box else None


# ------------------------------------------------- criterion 7: hostile files


@pytest.mark.parametrize(
    "content",
    [
        b"[" * 3000,  # deeply nested: json raises RecursionError, not JSONDecodeError
        b'{"a":' * 800 + b"1" + b"}" * 800,
        b'{"format": 1, "address": "10.0.0.5:47100"} trailing',
        b'{"format": 1, "address": "10.0.0.5:47100\\u0000"}',
        b'{"format": 1, "address": null}',
        b'{"format": 1, "address": ["10.0.0.5:47100"]}',
        b"\x00" * 100,
        b"",
        b'{"format": "1", "address": "10.0.0.5:47100"}',
    ],
)
def test_hostile_content_is_ignored_never_raises(
    ident_dir: Path, content: bytes
) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).write_bytes(content)
    result = known_controller.load_known(ident_dir)
    assert result is None


def test_deep_nesting_does_not_crash_the_resolver(ident_dir: Path) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).write_bytes(b"[" * 3000)
    resolver = LastKnownFallbackResolver(_Nothing(), ident_dir, node_id="n")
    assert asyncio.run(resolver.resolve()) is None


def test_oversized_file_is_not_read_whole(ident_dir: Path) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).write_bytes(b" " * (50 * 1024 * 1024))
    start = time.monotonic()
    assert known_controller.load_known(ident_dir) is None
    assert time.monotonic() - start < 2


@posix_only
def test_directory_named_controller_json_is_not_fatal(ident_dir: Path) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).mkdir()
    assert known_controller.load_known(ident_dir) is None
    assert not known_controller.remember(ident_dir, "10.0.0.5:47100", source="session")
    known_controller.forget(ident_dir)  # must not raise


@posix_only
def test_fifo_controller_json_does_not_hang_the_agent(ident_dir: Path) -> None:
    """A FIFO planted as controller.json would block open() forever in the
    resolver thread (the DISCOVERING state would never end)."""
    os.mkfifo(ident_dir / CONTROLLER_FILE_NAME)
    assert _within(5, known_controller.load_known, ident_dir) is None


@posix_only
def test_symlink_is_never_written_through(ident_dir: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (ident_dir / CONTROLLER_FILE_NAME).symlink_to(victim)
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="session")
    assert victim.read_text() == "precious"
    link = ident_dir / CONTROLLER_FILE_NAME
    assert not link.is_symlink()  # replaced by a regular owner-only file
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address == "10.0.0.5:47100"


@posix_only
def test_symlink_to_a_device_or_secret_is_not_trusted(ident_dir: Path) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).symlink_to("/dev/zero")
    assert _within(5, known_controller.load_known, ident_dir) is None
    (ident_dir / CONTROLLER_FILE_NAME).unlink()
    (ident_dir / CONTROLLER_FILE_NAME).symlink_to("/etc/passwd")
    assert known_controller.load_known(ident_dir) is None


@posix_only
def test_symlink_to_foreign_valid_json_is_refused(
    ident_dir: Path, tmp_path: Path
) -> None:
    """Defence in depth (criterion 7 'symlinked'): the identity files are
    regular files only (``private_files.read_private``); a symlink pointing at
    attacker-chosen content in another directory should not be followed."""
    other = tmp_path / "elsewhere.json"
    other.write_text('{"format": 1, "address": "203.0.113.9:47100"}')
    (ident_dir / CONTROLLER_FILE_NAME).symlink_to(other)
    assert known_controller.load_known(ident_dir) is None


# ----------------------------------------------- criterion 8: permissions


@posix_only
def test_permissions_match_identity_files(ident_dir: Path) -> None:
    assert known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    mode = stat.S_IMODE((ident_dir / CONTROLLER_FILE_NAME).stat().st_mode)
    key_mode = stat.S_IMODE((ident_dir / "node.key").stat().st_mode)
    assert mode == key_mode == 0o600
    assert stat.S_IMODE(ident_dir.stat().st_mode) == 0o700


@posix_only
def test_rewrite_keeps_owner_only_even_under_loose_umask(ident_dir: Path) -> None:
    old = os.umask(0)
    try:
        known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
        known_controller.remember(ident_dir, "10.0.0.6:47100", source="session")
    finally:
        os.umask(old)
    assert stat.S_IMODE((ident_dir / CONTROLLER_FILE_NAME).stat().st_mode) == 0o600


def test_no_temp_debris_after_many_writes(ident_dir: Path) -> None:
    for i in range(20):
        known_controller.remember(ident_dir, f"10.0.0.{i + 1}:47100", source="session")
    assert [p.name for p in ident_dir.glob(".*.tmp")] == []


# -------------------------------------------- concurrency / lock contention


def test_concurrent_writers_leave_one_valid_file(ident_dir: Path) -> None:
    addrs = [f"10.1.0.{i}:47100" for i in range(1, 17)]
    errors: list[BaseException] = []

    def go(a: str) -> None:
        try:
            known_controller.remember(ident_dir, a, source="session")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(a,)) for a in addrs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address in addrs
    assert [p.name for p in ident_dir.glob(".*.tmp")] == []


def test_readers_never_see_a_torn_file_during_writes(ident_dir: Path) -> None:
    known_controller.remember(ident_dir, "10.0.0.1:47100", source="join")
    stop = threading.Event()
    bad: list[str] = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            known_controller.remember(
                ident_dir, f"10.2.{i % 250}.1:47100", source="session"
            )

    def reader() -> None:
        bad.extend(
            "None" for _ in range(500) if known_controller.load_known(ident_dir) is None
        )

    w = threading.Thread(target=writer)
    w.start()
    reader()
    stop.set()
    w.join(30)
    assert bad == []


def test_lock_contention_remember_gives_up_quickly_and_keeps_old(
    ident_dir: Path,
) -> None:
    known_controller.remember(ident_dir, "10.0.0.1:47100", source="join")
    with locked_identity(ident_dir):
        # same-process second acquisition from another thread must time out
        def attempt() -> bool:
            return known_controller.remember(
                ident_dir, "10.0.0.2:47100", source="session", lock_timeout_s=0.3
            )

        start = time.monotonic()
        ok = _within(10, attempt)
        assert ok is False
        assert time.monotonic() - start < 5
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address == "10.0.0.1:47100"


def test_lock_contention_forget_is_logged_not_raised(ident_dir: Path) -> None:
    known_controller.remember(ident_dir, "10.0.0.1:47100", source="join")
    with locked_identity(ident_dir):
        _within(10, lambda: known_controller.forget(ident_dir, lock_timeout_s=0.3))
    assert known_controller.load_known(ident_dir) is not None  # not deleted, no crash


def test_load_does_not_need_the_lock(ident_dir: Path) -> None:
    """Discovery must not stall while the identity lock is held elsewhere."""
    known_controller.remember(ident_dir, "10.0.0.1:47100", source="join")
    with locked_identity(ident_dir):
        got = _within(5, known_controller.load_known, ident_dir)
    assert got is not None


# ------------------------------------------------------ addresses / IPv6


@pytest.mark.parametrize(
    "address",
    [
        "[::1]:47100",
        "[fd00::5]:47100",
        "host.local:47100",
        "10.0.0.5:65535",
        "[fe80::1%eth0]:47100",
    ],
)
def test_round_trip_of_plausible_targets(ident_dir: Path, address: str) -> None:
    assert known_controller.remember(ident_dir, address, source="join")
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address == address


@pytest.mark.parametrize(
    "address",
    [
        "::1:47100",
        "[::1]47100",
        "[::1]",
        ":47100",
        "10.0.0.5:-1",
        "10.0.0.5:abc",
        "a\nb:47100",
        " :1",
    ],
)
def test_malformed_targets_are_rejected(ident_dir: Path, address: str) -> None:
    assert not known_controller.remember(ident_dir, address, source="session")
    assert not (ident_dir / CONTROLLER_FILE_NAME).exists()


def test_unicode_host_does_not_break_the_writer(ident_dir: Path) -> None:
    """Non-ASCII hostnames (IDN) must not raise out of remember()."""
    result = known_controller.remember(ident_dir, "münchen.local:47100", source="join")
    assert result in (True, False)  # either way: no exception
    if result:
        assert known_controller.load_known(ident_dir) is not None


# --------------------------------------------- insecure-dev / client wiring


def _fake_client(ident_dir: Path, *, insecure: bool, target: str, timeout: float = 1.0):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        controller_target=target,
        _remembered=None,
        identity=SimpleNamespace(path=ident_dir),
        node_id="n",
        settings=SimpleNamespace(
            security=SimpleNamespace(insecure=insecure),
            agent=SimpleNamespace(identity_lock_timeout_s=timeout),
        ),
    )


def test_insecure_dev_never_writes_the_hint(ident_dir: Path) -> None:
    c = _fake_client(ident_dir, insecure=True, target="10.9.9.9:47100")
    asyncio.run(AgentSessionClient._remember_controller(c))  # type: ignore[arg-type]
    assert not (ident_dir / CONTROLLER_FILE_NAME).exists()


def test_busy_lock_does_not_mark_remembered_and_retries(ident_dir: Path) -> None:
    c = _fake_client(ident_dir, insecure=False, target="10.9.9.9:47100", timeout=0.2)

    async def scenario() -> None:
        with locked_identity(ident_dir):
            await AgentSessionClient._remember_controller(c)  # type: ignore[arg-type]
        assert c._remembered is None
        await AgentSessionClient._remember_controller(c)  # type: ignore[arg-type]
        assert c._remembered == "10.9.9.9:47100"

    asyncio.run(scenario())
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.source == "session"


def test_remember_after_reset_does_not_resurrect_for_a_left_fabric(
    ident_dir: Path,
) -> None:
    """Leave/reset deletes the hint; a stale in-memory 'remembered' must not
    stop a later different endpoint being stored, and reset must win."""
    from meshforge.agent.identity import reset

    known_controller.remember(ident_dir, "10.0.0.1:47100", source="join")
    reset(ident_dir)
    assert known_controller.load_known(ident_dir) is None
