"""``owner_only_icacls_argvs``: grant first, then one ACE left - the
mkdir(0o700) and logon-session ACEs removed (D399 CI)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.common import private_files
from meshforge.common.private_files import MKDIR_700_SIDS, owner_only_icacls_argvs

pytestmark = pytest.mark.unit

USER = "S-1-5-21-1-2-3-1001"
LOGON = "S-1-5-5-0-741059"
_WHOAMI_LOGONID = "S-1-5-5-0-7\r\n"  # what ``whoami /logonid`` prints


def test_the_grant_runs_first_and_removes_nothing() -> None:
    path = Path("C:/x/identity")
    grant, strip = owner_only_icacls_argvs(path, USER)
    assert grant == ["icacls", str(path), "/grant:r", f"*{USER}:(F)"]
    assert strip[:3] == ["icacls", str(path), "/inheritance:r"]
    assert strip[3] == "/remove:g"
    assert strip[4:] == [f"*{sid}" for sid in MKDIR_700_SIDS]


def test_the_logon_session_sid_is_removed_too() -> None:
    _, strip = owner_only_icacls_argvs(Path("p"), USER, LOGON)
    assert strip[strip.index("/remove:g") + 1 :] == [
        *(f"*{sid}" for sid in MKDIR_700_SIDS),
        f"*{LOGON}",
    ]


@pytest.mark.parametrize("sid", MKDIR_700_SIDS)
def test_the_current_sid_is_never_removed(sid: str) -> None:
    grant, strip = owner_only_icacls_argvs(Path("p"), sid, LOGON)
    assert grant[-1] == f"*{sid}:(F)"
    assert f"*{sid}" not in strip


@pytest.mark.parametrize(
    ("code", "stdout", "expected"),
    [
        (0, _WHOAMI_LOGONID, "S-1-5-5-0-7"),
        (0, "no sid here\n", None),
        (1, '"x","S-1-5-5-0-7"\n', None),
    ],
)
def test_current_logon_sid_parses_whoami_or_gives_none(
    monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, expected: str | None
) -> None:
    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["whoami", "/logonid"]  # no /fo: whoami refuses it
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert private_files.current_logon_sid() == expected


def test_current_logon_sid_is_none_when_whoami_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("no whoami")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert private_files.current_logon_sid() is None


def test_a_directory_grant_is_inheritable_and_a_file_grant_is_not() -> None:
    """D399 (Windows CI: PermissionError on a lock file made while the folder
    was being protected; the logon-session ACE on files made in it later):
    the folder's owner grant must be the ACE its children inherit."""
    folder, _ = owner_only_icacls_argvs(Path("d"), USER, LOGON, directory=True)
    assert folder[-1] == f"*{USER}:(OI)(CI)(F)"
    file_, _ = owner_only_icacls_argvs(Path("f"), USER, LOGON)
    assert file_[-1] == f"*{USER}:(F)"


@pytest.mark.parametrize("make_dir", [True, False])
def test_protect_windows_passes_directory_by_what_the_path_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_dir: bool
) -> None:
    from meshforge.agent import identity

    path = tmp_path / "x"
    if make_dir:
        path.mkdir()
    else:
        path.write_bytes(b"")
    expected = f"*{USER}:(OI)(CI)(F)" if make_dir else f"*{USER}:(F)"
    for module in (private_files, identity):
        seen: list[list[str]] = []
        monkeypatch.setattr(module, "_current_user_sid", lambda: USER)
        monkeypatch.setattr(module, "current_logon_sid", lambda: LOGON)
        monkeypatch.setattr(
            module, "_run_icacls", lambda argv, _p, seen=seen: seen.append(argv)
        )
        module._protect_windows(path)
        assert seen[0][-1] == expected, module.__name__
        assert seen[1][2] == "/inheritance:r"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows only")
def test_current_logon_sid_is_found_on_real_windows() -> None:
    """Every Windows token has a logon session; a lookup that silently gives
    ``None`` left its read ACE on every secret file (D399, Windows CI)."""
    sid = private_files.current_logon_sid()
    assert sid is not None and sid.startswith("S-1-5-5-"), sid
