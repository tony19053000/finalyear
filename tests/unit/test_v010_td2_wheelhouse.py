"""V0.10 TD2: ``mf_wheelhouse.ensure_wheel`` returns a folder holding exactly
the wheel (D-2), never shares a ``.part`` between callers (D-3), and fails
hard when a fresh download is not the locked wheel."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from mf_wheelhouse import (
    LockedWheel,
    WheelDownloadMismatchError,
    WheelhouseUnavailableError,
    ensure_wheel,
)

pytestmark = pytest.mark.unit

DATA = b"PK-td2-wheel" * 4096
URL = "https://files.example/p/demo-2.0-py3-none-any.whl"


def _wheel(data: bytes = DATA) -> LockedWheel:
    return LockedWheel(URL, hashlib.sha256(data).hexdigest(), len(data))


def _serve(data: bytes, calls: list[Path] | None = None) -> Callable[[str, Path], None]:
    def serve(url: str, dest: Path) -> None:
        if calls is not None:
            calls.append(dest)
        dest.write_bytes(data)

    return serve


def _never(url: str, dest: Path) -> None:
    raise AssertionError("downloaded on a verified hit")


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def _place(root: Path, wheel: LockedWheel, data: bytes = DATA) -> Path:
    folder = root / wheel.sha256[:16]
    folder.mkdir(parents=True)
    (folder / wheel.filename).write_bytes(data)
    return folder


# ----------------------------------------------------------------- D-2
def test_stray_files_and_links_are_removed_on_a_cache_hit(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel)
    (folder / "demo-9.9-py3-none-any.whl").write_bytes(b"evil")
    (folder / ".hidden").write_bytes(b"x")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    (folder / "link.whl").symlink_to(outside)
    got = ensure_wheel(wheel, tmp_path, download=_never)
    assert sorted(p.name for p in got.iterdir()) == [wheel.filename]
    assert outside.read_text() == "keep me"  # the link went, not its target
    assert (got / wheel.filename).read_bytes() == DATA


def test_stray_files_are_removed_after_a_download_too(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = tmp_path / wheel.sha256[:16]
    folder.mkdir()
    (folder / "stray.whl").write_bytes(b"x")
    got = ensure_wheel(wheel, tmp_path, download=_serve(DATA))
    assert sorted(p.name for p in got.iterdir()) == [wheel.filename]


def test_a_stray_directory_is_a_skip_reason_and_is_not_deleted(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel)
    (folder / "sub").mkdir()
    (folder / "sub" / "keep").write_bytes(b"k")
    with pytest.raises(WheelhouseUnavailableError, match="directory 'sub'"):
        ensure_wheel(wheel, tmp_path, download=_never)
    assert (folder / "sub" / "keep").read_bytes() == b"k"


# ----------------------------------------------------------------- D-3
def test_every_call_gets_its_own_part_file_beside_the_folder(tmp_path: Path) -> None:
    wheel = _wheel()
    seen: list[Path] = []
    for _ in range(3):
        (tmp_path / wheel.sha256[:16] / wheel.filename).unlink(missing_ok=True)
        ensure_wheel(wheel, tmp_path, download=_serve(DATA, seen))
    assert len(set(seen)) == 3
    for part in seen:
        assert part.parent == tmp_path and part.name.endswith(".part")
        assert not part.exists()
    assert _files(tmp_path) == [f"{wheel.sha256[:16]}/{wheel.filename}"]


def test_the_installed_wheel_is_readable_by_others(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = ensure_wheel(wheel, tmp_path, download=_serve(DATA))
    assert os.stat(folder / wheel.filename).st_mode & 0o444 == 0o444


def test_many_concurrent_callers_in_one_process_all_get_the_good_wheel(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    gate = threading.Barrier(8)
    results: list[object] = []
    lock = threading.Lock()

    def slow_serve(url: str, dest: Path) -> None:
        with dest.open("wb") as fh:
            for i in range(0, len(DATA), 512):
                fh.write(DATA[i : i + 512])

    def run() -> None:
        gate.wait()
        try:
            got: object = ensure_wheel(wheel, tmp_path, download=slow_serve)
        except Exception as exc:  # noqa: BLE001 - any error is the finding
            got = exc
        with lock:
            results.append(got)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    folder = tmp_path / wheel.sha256[:16]
    assert results == [folder] * 8
    assert (folder / wheel.filename).read_bytes() == DATA
    assert _files(tmp_path) == [f"{wheel.sha256[:16]}/{wheel.filename}"]


# ------------------------------------------------------- hard failure
@pytest.mark.parametrize(
    "served",
    [
        pytest.param(b"tampered", id="other-bytes"),
        pytest.param(DATA[:-1], id="short"),
        pytest.param(DATA[:-1] + b"!", id="same-size-wrong-hash"),
    ],
)
def test_a_fresh_download_that_is_not_the_locked_wheel_fails_hard(
    tmp_path: Path, served: bytes
) -> None:
    wheel = _wheel()
    with pytest.raises(WheelDownloadMismatchError, match=r"does not match uv\.lock"):
        ensure_wheel(wheel, tmp_path, download=_serve(served))
    assert _files(tmp_path) == []
    assert not issubclass(WheelDownloadMismatchError, WheelhouseUnavailableError)


def test_a_corrupt_cache_entry_is_re_downloaded_not_a_failure(tmp_path: Path) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, b"corrupt")
    calls: list[Path] = []
    assert ensure_wheel(wheel, tmp_path, download=_serve(DATA, calls)) == folder
    assert len(calls) == 1 and (folder / wheel.filename).read_bytes() == DATA


def test_a_corrupt_cache_and_a_bad_download_is_a_hard_failure_and_keeps_nothing_new(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    folder = _place(tmp_path, wheel, b"corrupt")
    with pytest.raises(WheelDownloadMismatchError):
        ensure_wheel(wheel, tmp_path, download=_serve(b"also wrong"))
    assert _files(tmp_path) == [f"{wheel.sha256[:16]}/{wheel.filename}"]
    assert (folder / wheel.filename).read_bytes() == b"corrupt"


def test_a_symlinked_wheel_folder_is_refused_and_its_target_untouched(
    tmp_path: Path,
) -> None:
    """TD2 review N2: iterating a symlinked folder deleted files outside it."""
    import mf_wheelhouse as wh

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    link = tmp_path / "folder"
    link.symlink_to(outside, target_is_directory=True)
    wheel = wh.LockedWheel(
        url="https://x/numpy-0-py3-none-any.whl", sha256="0" * 64, size=1
    )
    with pytest.raises(wh.WheelhouseUnavailableError, match="symlink"):
        wh._only_the_wheel(link, wheel)
    assert (outside / "keep.txt").exists()
