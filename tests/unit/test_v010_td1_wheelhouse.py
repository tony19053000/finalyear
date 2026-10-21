"""V0.10 TD1 (D288 follow-up): the cluster tests' numpy wheelhouse
(``tests/_support/mf_wheelhouse.py``) - wheel choice from ``uv.lock``, the
cache, and the offline / bad-download paths. No network: the downloader is
injected."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from mf_wheelhouse import (
    CACHE_ENV,
    LockedWheel,
    WheelDownloadMismatchError,
    WheelhouseUnavailableError,
    cache_root,
    ensure_wheel,
    locked_wheels,
    pick_wheel,
)
from packaging.tags import Tag

from meshforge.bench.workloads.materialise import NUMPY_REQUIREMENT

pytestmark = pytest.mark.unit

DATA = b"not really a wheel, but bytes with a known hash"
URL = "https://files.example/p/demo-1.0-py3-none-any.whl"


def _wheel(data: bytes = DATA, *, size: int | None = -1) -> LockedWheel:
    return LockedWheel(
        url=URL,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data) if size == -1 else size,
    )


def _serve(data: bytes, calls: list[str]) -> Callable[[str, Path], None]:
    def download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(data)

    return download


def _offline(url: str, dest: Path) -> None:
    dest.write_bytes(b"half")  # a partial write, then the network goes away
    raise OSError("Network is unreachable")


def test_the_real_lock_has_a_wheel_of_the_pinned_numpy_for_this_interpreter() -> None:
    name, _, version = NUMPY_REQUIREMENT.partition("==")
    wheels = locked_wheels(name, version)
    assert wheels, NUMPY_REQUIREMENT
    assert all(len(w.sha256) == 64 and w.filename.endswith(".whl") for w in wheels)
    chosen = pick_wheel(wheels)
    assert chosen is not None and chosen.filename.startswith(f"numpy-{version}-")


def test_locked_wheels_reads_sha256_entries_only(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "demo"\n'
        'version = "1.0"\n'
        "wheels = [\n"
        f'  {{ url = "{URL}", hash = "sha256:{"a" * 64}", size = 10 }},\n'
        '  { url = "https://x/demo-1.0-py2-none-any.whl", hash = "md5:00" },\n'
        "]\n"
        "[[package]]\n"
        'name = "demo"\n'
        'version = "2.0"\n'
        "wheels = []\n"
    )
    assert locked_wheels("demo", "1.0", lock) == [
        LockedWheel(url=URL, sha256="a" * 64, size=10)
    ]
    assert locked_wheels("demo", "2.0", lock) == []
    assert locked_wheels("other", "1.0", lock) == []


def test_pick_wheel_follows_the_tag_priority() -> None:
    generic = LockedWheel("https://x/demo-1.0-py3-none-any.whl", "a" * 64, 1)
    linux = LockedWheel(
        "https://x/demo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl", "b" * 64, 1
    )
    mac = LockedWheel(
        "https://x/demo-1.0-cp312-cp312-macosx_14_0_arm64.whl", "c" * 64, 1
    )
    wheels = [generic, linux, mac]
    linux_first = [
        Tag("cp312", "cp312", "manylinux_2_28_x86_64"),
        Tag("py3", "none", "any"),
    ]
    assert pick_wheel(wheels, linux_first) == linux
    assert pick_wheel(wheels, [Tag("cp311", "cp311", "win_amd64")]) is None
    assert pick_wheel([generic, mac], linux_first) == generic


def test_cache_root_honours_the_override_then_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CACHE_ENV, str(tmp_path / "lab-wheels"))
    assert cache_root() == tmp_path / "lab-wheels"
    monkeypatch.delenv(CACHE_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_root() == tmp_path / "xdg" / "meshforge-tests" / "wheelhouse"


def test_a_miss_downloads_once_and_the_folder_holds_only_the_wheel(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    wheel = _wheel()
    folder = ensure_wheel(wheel, tmp_path, download=_serve(DATA, calls))
    assert calls == [URL]
    assert sorted(p.name for p in folder.iterdir()) == [wheel.filename]
    assert (folder / wheel.filename).read_bytes() == DATA
    assert not list(tmp_path.glob(".*.part"))
    # a hit needs no network at all
    again = ensure_wheel(wheel, tmp_path, download=_offline)
    assert again == folder


def test_offline_with_an_empty_cache_is_a_skip_reason_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    with pytest.raises(WheelhouseUnavailableError, match="could not be downloaded"):
        ensure_wheel(wheel, tmp_path, download=_offline)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_download_that_does_not_match_the_lock_is_refused(tmp_path: Path) -> None:
    wheel = _wheel()
    # V0.10 TD2: a hard failure, not a skip reason
    with pytest.raises(WheelDownloadMismatchError, match="does not match"):
        ensure_wheel(wheel, tmp_path, download=_serve(b"tampered", []))
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_corrupt_cached_wheel_is_replaced(tmp_path: Path) -> None:
    calls: list[str] = []
    wheel = _wheel()
    folder = tmp_path / wheel.sha256[:16]
    folder.mkdir()
    (folder / wheel.filename).write_bytes(DATA[:-1] + b"X")  # same size, wrong hash
    ensure_wheel(wheel, tmp_path, download=_serve(DATA, calls))
    assert calls == [URL]
    assert (folder / wheel.filename).read_bytes() == DATA


def test_a_corrupt_cached_wheel_offline_is_a_skip_reason(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = tmp_path / wheel.sha256[:16]
    folder.mkdir()
    (folder / wheel.filename).write_bytes(b"short")
    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, tmp_path, download=_offline)
