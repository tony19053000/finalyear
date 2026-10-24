"""``owner_only_icacls_argvs``: grant first, then one ACE left - the
mkdir(0o700) and logon-session ACEs removed (D399 CI)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from meshforge.common import private_files
from meshforge.common.private_files import MKDIR_700_SIDS, owner_only_icacls_argvs

pytestmark = pytest.mark.unit

USER = "S-1-5-21-1-2-3-1001"
LOGON = "S-1-5-5-0-741059"
_WHOAMI_LOGONID = (
    '"User Name","SID"\n"NT AUTHORITY\\\\LogonSessionId_0_7","S-1-5-5-0-7"\n'
)


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
        assert argv[:2] == ["whoami", "/logonid"]
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
