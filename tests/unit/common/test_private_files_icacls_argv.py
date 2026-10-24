"""``owner_only_icacls_argv``: one ACE left, the mkdir(0o700) ACEs removed (D399 CI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.common.private_files import MKDIR_700_SIDS, owner_only_icacls_argv

pytestmark = pytest.mark.unit

USER = "S-1-5-21-1-2-3-1001"


def test_a_normal_user_gets_one_grant_and_the_mkdir_aces_removed() -> None:
    argv = owner_only_icacls_argv(Path("C:/x/identity"), USER)
    assert argv[:2] == ["icacls", str(Path("C:/x/identity"))]
    assert argv[2:5] == ["/inheritance:r", "/grant:r", f"*{USER}:(F)"]
    assert argv[5] == "/remove:g"
    assert argv[6:] == [f"*{sid}" for sid in MKDIR_700_SIDS]


@pytest.mark.parametrize("sid", MKDIR_700_SIDS)
def test_the_current_sid_is_never_removed(sid: str) -> None:
    argv = owner_only_icacls_argv(Path("p"), sid)
    assert f"*{sid}:(F)" in argv
    assert f"*{sid}" not in argv[argv.index("/remove:g") + 1 :]
