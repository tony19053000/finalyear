"""V0.10 TD1 tester (independent): ``mf_wheelhouse`` under a tampered or
corrupt cache, size mismatch, an offline machine, and the promise that a cache
hit (and an unusable cache) never touches an index or the network.
"""

from __future__ import annotations

import hashlib
import socket
import threading
from pathlib import Path
from typing import Any

import pytest
from mf_wheelhouse import (
    LockedWheel,
    WheelDownloadMismatchError,
    WheelhouseUnavailableError,
    cache_root,
    ensure_wheel,
    locked_wheels,
    pick_wheel,
)

from meshforge.bench.workloads.materialise import NUMPY_REQUIREMENT

pytestmark = pytest.mark.unit

DATA = b"PK-pretend-wheel" * 1000
URL = "https://files.example/p/demo-1.0-py3-none-any.whl"


def _wheel(data: bytes = DATA, *, size: int | None = -1) -> LockedWheel:
    return LockedWheel(
        URL, hashlib.sha256(data).hexdigest(), len(data) if size == -1 else size
    )


def _boom(url: str, dest: Path) -> None:
    raise AssertionError(f"network touched: {url}")


def _place(root: Path, wheel: LockedWheel, data: bytes) -> Path:
    folder = root / wheel.sha256[:16]
    folder.mkdir(parents=True)
    (folder / wheel.filename).write_bytes(data)
    return folder


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a socket was used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_a_verified_cache_hit_never_contacts_anything(
    tmp_path: Path, no_sockets: None
) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, DATA)
    assert ensure_wheel(wheel, tmp_path, download=_boom) == folder
    # the default downloader too: a hit must not even build a client
    assert ensure_wheel(wheel, tmp_path) == folder


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda d: d[:-1] + bytes([d[-1] ^ 1]), id="one-bit-flip"),
        pytest.param(lambda d: d[:-1], id="truncated-by-one"),
        pytest.param(lambda d: d + b"\0", id="appended"),
        pytest.param(lambda d: b"", id="empty"),
        pytest.param(lambda d: b"x" * len(d), id="same-size-other-bytes"),
        pytest.param(lambda d: d * 2, id="doubled"),
    ],
)
def test_any_tampered_cached_wheel_is_never_returned_as_is(
    tmp_path: Path, tamper: object
) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, tamper(DATA))  # type: ignore[operator]
    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, tmp_path, download=_boom_as_offline)
    # and the good bytes replace it when the network works
    served: list[str] = []

    def serve(url: str, dest: Path) -> None:
        served.append(url)
        dest.write_bytes(DATA)

    assert ensure_wheel(wheel, tmp_path, download=serve) == folder
    assert (folder / wheel.filename).read_bytes() == DATA and served == [URL]


def _boom_as_offline(url: str, dest: Path) -> None:
    raise ConnectionError("offline")


def test_size_mismatch_in_the_lock_rejects_even_a_matching_hash(
    tmp_path: Path,
) -> None:
    wheel = _wheel(size=len(DATA) + 1)  # lock says a different size than the bytes
    _place(tmp_path, wheel, DATA)
    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, tmp_path, download=_boom_as_offline)


def test_download_with_wrong_size_is_refused_and_not_installed(
    tmp_path: Path,
) -> None:
    wheel = _wheel(size=len(DATA) - 5)

    def serve(url: str, dest: Path) -> None:
        dest.write_bytes(DATA)

    # V0.10 TD2: a fresh download that is not the locked wheel fails hard
    with pytest.raises(WheelDownloadMismatchError, match="does not match"):
        ensure_wheel(wheel, tmp_path, download=serve)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_unknown_size_in_the_lock_falls_back_to_the_hash(tmp_path: Path) -> None:
    wheel = _wheel(size=None)
    folder = _place(tmp_path, wheel, DATA)
    assert ensure_wheel(wheel, tmp_path, download=_boom) == folder
    (folder / wheel.filename).write_bytes(DATA + b"!")
    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, tmp_path, download=_boom_as_offline)


def test_a_failed_or_interrupted_download_leaves_no_partial_anywhere(
    tmp_path: Path,
) -> None:
    wheel = _wheel()

    def half(url: str, dest: Path) -> None:
        dest.write_bytes(DATA[: len(DATA) // 2])
        raise TimeoutError("read timed out")

    for _ in range(3):
        with pytest.raises(WheelhouseUnavailableError, match="TimeoutError"):
            ensure_wheel(wheel, tmp_path, download=half)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_download_failure_never_replaces_a_good_cache_entry_with_garbage(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, DATA)
    # verified entry: downloader must not even be called, and the bytes stay
    ensure_wheel(wheel, tmp_path, download=_boom)
    assert (folder / wheel.filename).read_bytes() == DATA


def test_target_path_is_a_directory_is_unavailable_not_a_crash(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = tmp_path / wheel.sha256[:16]
    (folder / wheel.filename).mkdir(parents=True)
    with pytest.raises((WheelhouseUnavailableError, OSError)):
        ensure_wheel(wheel, tmp_path, download=_boom_as_offline)


def test_real_downloader_offline_is_a_skip_reason_not_a_hang(tmp_path: Path) -> None:
    """Default downloader against a closed port: fast, typed failure, no files."""
    wheel = LockedWheel(
        "https://127.0.0.1:9/demo-1.0-py3-none-any.whl",
        hashlib.sha256(DATA).hexdigest(),
        len(DATA),
    )
    with pytest.raises(WheelhouseUnavailableError, match="could not be downloaded"):
        ensure_wheel(wheel, tmp_path)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_unwritable_cache_root_is_not_a_traceback_of_a_different_type(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises((WheelhouseUnavailableError, OSError)):
        ensure_wheel(_wheel(), blocker / "sub", download=_boom_as_offline)


def test_concurrent_callers_get_one_good_wheel(tmp_path: Path) -> None:
    """Two threads (same pid) filling an empty cache at once: neither may
    return a folder whose wheel is corrupt. (Was a non-strict xfail: the
    per-pid ``.part`` name was shared; V0.10 TD2 D-3 makes it per call.)"""
    wheel = _wheel()
    gate = threading.Barrier(4)
    results: list[object] = []

    def serve(url: str, dest: Path) -> None:
        with dest.open("wb") as fh:
            for i in range(0, len(DATA), 100):
                fh.write(DATA[i : i + 100])

    def run() -> None:
        gate.wait()
        try:
            results.append(ensure_wheel(wheel, tmp_path, download=serve))
        except WheelhouseUnavailableError as exc:
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    for got in results:
        if isinstance(got, Path):
            assert (got / wheel.filename).read_bytes() == DATA
    assert (tmp_path / wheel.sha256[:16] / wheel.filename).read_bytes() == DATA or all(
        isinstance(g, WheelhouseUnavailableError) for g in results
    )


# V0.10 TD2 D-2: was a strict xfail - stray files are now removed
def test_a_stray_second_wheel_in_the_folder_is_not_served_to_agents(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, DATA)
    (folder / "demo-1.0-py3-none-manylinux_evil.whl").write_bytes(b"evil")
    got = ensure_wheel(wheel, tmp_path, download=_boom)
    assert sorted(p.name for p in got.iterdir()) == [wheel.filename]


def test_cache_root_override_beats_xdg_and_expands_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESHFORGE_TEST_WHEELHOUSE", "~/wh")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_root() == tmp_path / "wh"
    monkeypatch.setenv("MESHFORGE_TEST_WHEELHOUSE", "")
    assert cache_root() == tmp_path / "xdg" / "meshforge-tests" / "wheelhouse"


def test_the_real_cache_entry_if_present_matches_uv_lock(
    no_sockets: None,
) -> None:
    """A previously filled cache (what the cluster gate used) verifies against
    the lock with no network."""
    name, _, version = NUMPY_REQUIREMENT.partition("==")
    wheel = pick_wheel(locked_wheels(name, version))
    assert wheel is not None
    target = cache_root() / wheel.sha256[:16] / wheel.filename
    if not target.exists():
        pytest.skip("wheelhouse cache not filled on this machine")
    folder = ensure_wheel(wheel)
    assert [p.name for p in folder.iterdir()] == [wheel.filename]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == wheel.sha256


def test_split_fabric_wheelhouse_means_no_index_and_find_links(
    tmp_path: Path,
) -> None:
    pytest.importorskip("psutil")
    import mf_split_cluster

    seen: dict[str, object] = {}

    def make_fabric(**kwargs: object) -> Any:
        seen.update(kwargs)
        return object()

    mf_split_cluster.split_fabric(make_fabric, wheelhouse=tmp_path)
    envs = seen["agent_extra"]["envs"]  # type: ignore[index]
    assert envs == {"find_links": str(tmp_path), "no_index": True}
    seen.clear()
    mf_split_cluster.split_fabric(make_fabric)
    assert seen["agent_extra"] is None  # default fabric: index allowed, as before
    seen.clear()
    mf_split_cluster.split_fabric(
        make_fabric,
        wheelhouse=tmp_path,
        agent_extra={"envs": {"cache_dir": "x"}, "other": {"a": 1}},
    )
    assert seen["agent_extra"]["envs"] == {  # type: ignore[index]
        "cache_dir": "x",
        "find_links": str(tmp_path),
        "no_index": True,
    }
