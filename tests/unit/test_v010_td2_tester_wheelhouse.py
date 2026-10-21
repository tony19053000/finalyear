"""V0.10 TD2 tester: attacks on ``mf_wheelhouse.ensure_wheel`` - stray files
and links, stray directories, per-call ``.part``, permissions, concurrent
callers (threads), and the cluster fixture's hard failure."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from mf_wheelhouse import (
    LockedWheel,
    WheelDownloadMismatchError,
    WheelhouseUnavailableError,
    ensure_wheel,
)

pytestmark = pytest.mark.unit

DATA = b"PK-tester-td2-wheel" * 2048
URL = "https://files.example/p/demo-3.0-py3-none-any.whl"


def _wheel(data: bytes = DATA) -> LockedWheel:
    return LockedWheel(URL, hashlib.sha256(data).hexdigest(), len(data))


def _serve(data: bytes = DATA):  # type: ignore[no-untyped-def]
    def serve(url: str, dest: Path) -> None:
        dest.write_bytes(data)

    return serve


def _fail(url: str, dest: Path) -> None:
    raise AssertionError("downloaded on a verified hit")


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks / modes")


@posix
def test_symlink_stray_is_unlinked_and_its_target_is_untouched(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = ensure_wheel(wheel, root, download=_serve())
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    outside_dir = tmp_path / "precious_dir"
    outside_dir.mkdir()
    (outside_dir / "f").write_text("also")
    (folder / "link.whl").symlink_to(outside)
    (folder / "dirlink").symlink_to(outside_dir)  # symlink to a directory
    (folder / "dangling").symlink_to(tmp_path / "nowhere")
    got = ensure_wheel(wheel, root, download=_fail)
    assert got == folder
    assert _listing(folder) == [wheel.filename]
    assert outside.read_text() == "keep me"
    assert (outside_dir / "f").read_text() == "also"


@posix
def test_the_wheel_itself_being_a_symlink_to_good_bytes_is_still_exactly_one(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = root / wheel.sha256[:16]
    folder.mkdir(parents=True)
    real = tmp_path / "real.whl"
    real.write_bytes(DATA)
    (folder / wheel.filename).symlink_to(real)
    (folder / "stray").write_bytes(b"x")
    assert ensure_wheel(wheel, root, download=_fail) == folder
    assert _listing(folder) == [wheel.filename]
    assert real.read_bytes() == DATA


def test_stray_directory_is_a_skip_reason_and_is_left_alone(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = ensure_wheel(wheel, root, download=_serve())
    stray = folder / "subdir"
    stray.mkdir()
    (stray / "important").write_text("data")
    (folder / "stray_file").write_bytes(b"x")
    with pytest.raises(WheelhouseUnavailableError, match="subdir"):
        ensure_wheel(wheel, root, download=_fail)
    assert (stray / "important").read_text() == "data"


def test_stray_directory_also_blocks_after_a_fresh_download(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    (root / wheel.sha256[:16] / "sub").mkdir(parents=True)
    with pytest.raises(WheelhouseUnavailableError, match="sub"):
        ensure_wheel(wheel, root, download=_serve())
    assert (root / wheel.sha256[:16] / "sub").is_dir()


def test_empty_stray_file_and_dotfiles_are_removed(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = ensure_wheel(wheel, root, download=_serve())
    (folder / "").with_name("empty").write_bytes(b"")
    (folder / ".DS_Store").write_bytes(b"\x00")
    (folder / "日本.whl").write_bytes(b"u")
    assert ensure_wheel(wheel, root, download=_fail) == folder
    assert _listing(folder) == [wheel.filename]


def test_part_is_per_call_beside_the_folder_and_gone(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    seen: list[Path] = []

    def serve(url: str, dest: Path) -> None:
        seen.append(dest)
        assert dest.parent == root  # beside the folder, not inside it
        assert dest.exists()  # created for the caller, unique
        dest.write_bytes(DATA)

    for _ in range(2):  # second call is a hit: no new .part
        ensure_wheel(wheel, root, download=serve)
    other = root / "other"
    other.mkdir()
    ensure_wheel(
        _wheel(b"second wheel" * 100), root, download=_serve(b"second wheel" * 100)
    )
    assert len(seen) == 1
    assert not [p for p in root.iterdir() if p.name.endswith(".part")]
    assert (root / wheel.sha256[:16]).is_dir()
    assert _listing(root / wheel.sha256[:16]) == [wheel.filename]


def test_part_removed_when_download_raises_or_mismatches(tmp_path: Path) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"

    def boom(url: str, dest: Path) -> None:
        dest.write_bytes(b"partial")
        raise OSError("connection reset")

    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, root, download=boom)
    with pytest.raises(WheelDownloadMismatchError):
        ensure_wheel(wheel, root, download=_serve(b"wrong"))
    assert not [p for p in root.iterdir() if p.is_file()]
    assert _listing(root / wheel.sha256[:16]) == []  # no half-written wheel


@posix
def test_wheel_is_0644_even_with_a_restrictive_umask(tmp_path: Path) -> None:
    wheel = _wheel()
    old = os.umask(0o077)
    try:
        folder = ensure_wheel(wheel, tmp_path / "cache", download=_serve())
    finally:
        os.umask(old)
    mode = stat.S_IMODE((folder / wheel.filename).stat().st_mode)
    assert mode == 0o644, oct(mode)


def test_wrong_size_with_right_prefix_is_a_hard_failure(tmp_path: Path) -> None:
    wheel = _wheel()
    for data in (DATA + b"x", DATA[:-1], b""):
        with pytest.raises(WheelDownloadMismatchError):
            ensure_wheel(wheel, tmp_path / "c", download=_serve(data))
    assert not issubclass(WheelDownloadMismatchError, WheelhouseUnavailableError)
    assert not issubclass(WheelhouseUnavailableError, WheelDownloadMismatchError)


def test_same_size_wrong_content_is_a_hard_failure(tmp_path: Path) -> None:
    wheel = _wheel()
    bad = bytes([DATA[0] ^ 1]) + DATA[1:]
    with pytest.raises(WheelDownloadMismatchError, match="does not match"):
        ensure_wheel(wheel, tmp_path / "c", download=_serve(bad))


def test_corrupt_cache_with_unreachable_network_is_unavailable_not_failure(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = root / wheel.sha256[:16]
    folder.mkdir(parents=True)
    (folder / wheel.filename).write_bytes(b"corrupt")

    def offline(url: str, dest: Path) -> None:
        raise ConnectionError("no network")

    with pytest.raises(WheelhouseUnavailableError):
        ensure_wheel(wheel, root, download=offline)


def test_concurrent_callers_fresh_cache_all_succeed_one_good_wheel(
    tmp_path: Path,
) -> None:
    wheel = _wheel()
    root = tmp_path / "cache"
    barrier = threading.Barrier(12)
    results: list[Path | BaseException] = []
    lock = threading.Lock()

    def serve(url: str, dest: Path) -> None:
        dest.write_bytes(DATA)

    def run() -> None:
        barrier.wait()
        try:
            out: Path | BaseException = ensure_wheel(wheel, root, download=serve)
        except BaseException as exc:  # noqa: BLE001
            out = exc
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    assert len({str(r) for r in results}) == 1
    folder = root / wheel.sha256[:16]
    assert _listing(folder) == [wheel.filename]
    assert (folder / wheel.filename).read_bytes() == DATA
    assert not [p for p in root.iterdir() if p.name.endswith(".part")]


def test_concurrent_callers_with_stray_files_do_not_crash(tmp_path: Path) -> None:
    """Two callers both removing the same strays (FileNotFoundError race)."""
    wheel = _wheel()
    root = tmp_path / "cache"
    folder = ensure_wheel(wheel, root, download=_serve())
    for i in range(200):
        (folder / f"stray{i}").write_bytes(b"x")
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def run() -> None:
        barrier.wait()
        try:
            ensure_wheel(wheel, root, download=_fail)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert _listing(folder) == [wheel.filename]


# -------------------------------------------------------- cluster fixture
def _fixture_function():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[1] / "cluster" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_td2_cluster_conftest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    fixture = module.numpy_wheelhouse
    return getattr(fixture, "__wrapped__", None) or fixture._get_wrapped_function()


def test_fixture_turns_a_mismatch_into_pytest_fail_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mf_wheelhouse

    func = _fixture_function()

    def mismatch(wheel: LockedWheel, *a: object, **k: object) -> Path:
        raise WheelDownloadMismatchError("served other bytes")

    monkeypatch.setattr(mf_wheelhouse, "ensure_wheel", mismatch)
    with pytest.raises(BaseException) as caught:
        func()
    # a skip here would be the defect: assert on the exact outcome type
    assert caught.type is pytest.fail.Exception, caught.type
    assert "served other bytes" in str(caught.value)


def test_fixture_still_skips_when_only_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mf_wheelhouse

    func = _fixture_function()

    def offline(wheel: LockedWheel, *a: object, **k: object) -> Path:
        raise WheelhouseUnavailableError("no network")

    monkeypatch.setattr(mf_wheelhouse, "ensure_wheel", offline)
    with pytest.raises(BaseException) as caught:
        func()
    assert caught.type is pytest.skip.Exception, caught.type
    assert "no network" in str(caught.value)


@posix
def test_a_symlinked_wheel_folder_is_refused_and_its_target_untouched(
    tmp_path: Path,
) -> None:
    """Iterating a symlinked folder would delete the *target's* files."""
    wheel = _wheel()
    root = tmp_path / "cache"
    root.mkdir()
    target = tmp_path / "somebody_elses_data"
    target.mkdir()
    (target / wheel.filename).write_bytes(DATA)
    (target / "precious").write_text("keep")
    (root / wheel.sha256[:16]).symlink_to(target, target_is_directory=True)
    with pytest.raises(WheelhouseUnavailableError, match="symlink"):
        ensure_wheel(wheel, root, download=_fail)
    assert (target / "precious").read_text() == "keep"
    assert (target / wheel.filename).read_bytes() == DATA
