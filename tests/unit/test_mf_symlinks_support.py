"""Unit tests for ``tests/_support/mf_symlinks.py``.

The "no symlink privilege" host (Windows 11 Home, non-admin, Developer Mode off:
``WinError 1314``) is simulated by monkeypatching ``os.symlink``, so these run on
every OS.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path

import mf_symlinks
import pytest
from mf_symlinks import (
    SYMLINK_REFUSED_REASON,
    WINERROR_PRIVILEGE_NOT_HELD,
    can_symlink,
    make_symlink,
    symlink_refused,
)

pytestmark = pytest.mark.unit


def _privilege_not_held() -> OSError:
    exc = OSError(errno.EACCES, "A required privilege is not held by the client")
    exc.winerror = WINERROR_PRIVILEGE_NOT_HELD  # type: ignore[attr-defined]
    return exc


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise _privilege_not_held()


@pytest.fixture(autouse=True)
def _fresh_probe() -> Iterator[None]:
    can_symlink.cache_clear()
    yield
    can_symlink.cache_clear()


def test_winerror_1314_is_a_refusal() -> None:
    assert symlink_refused(_privilege_not_held())


@pytest.mark.parametrize("code", [errno.ENOENT, errno.EEXIST, errno.EACCES])
def test_other_errors_are_not_a_refusal(code: int) -> None:
    assert not symlink_refused(OSError(code, os.strerror(code)))


@pytest.mark.skipif(os.name != "posix", reason="POSIX errno answers")
def test_a_posix_filesystem_without_symlinks_is_a_refusal() -> None:
    assert symlink_refused(OSError(errno.EPERM, "Operation not permitted"))
    assert symlink_refused(OSError(errno.ENOTSUP, "Operation not supported"))


def test_make_symlink_skips_when_the_os_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mf_symlinks.os, "symlink", _refuse)
    with pytest.raises(pytest.skip.Exception) as info:
        make_symlink(tmp_path / "link", tmp_path / "target")
    assert SYMLINK_REFUSED_REASON in str(info.value)
    assert not (tmp_path / "link").exists()


def test_make_symlink_skips_without_os_symlink_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(mf_symlinks.os, "symlink", unsupported)
    with pytest.raises(pytest.skip.Exception):
        make_symlink(tmp_path / "link", tmp_path / "target")


def test_make_symlink_still_fails_on_a_real_error(tmp_path: Path) -> None:
    (tmp_path / "taken").write_text("x")
    with pytest.raises(FileExistsError):
        make_symlink(tmp_path / "taken", tmp_path / "target")


def test_make_symlink_passes_the_directory_flag_and_argument_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object, bool]] = []

    def record(src: object, dst: object, target_is_directory: bool = False) -> None:
        calls.append((src, dst, target_is_directory))

    monkeypatch.setattr(mf_symlinks.os, "symlink", record)
    make_symlink(tmp_path / "link", tmp_path / "dir", target_is_directory=True)
    assert calls == [(tmp_path / "dir", tmp_path / "link", True)]


def test_can_symlink_is_false_when_the_os_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mf_symlinks.os, "symlink", _refuse)
    assert can_symlink() is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX always allows symlinks in tmp")
def test_make_symlink_and_can_symlink_work_where_allowed(tmp_path: Path) -> None:
    assert can_symlink() is True
    (tmp_path / "target").write_text("x")
    make_symlink(tmp_path / "link", tmp_path / "target")
    assert (tmp_path / "link").is_symlink()
    assert (tmp_path / "link").read_text() == "x"
