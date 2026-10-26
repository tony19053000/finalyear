"""V0.17 G5 (brief §5e item 6, D396/Q40/Q41): ``docs/user/troubleshooting.md``
and the code stay in step, and the user docs' screenshots are real or listed.

* every ``Code:`` on the troubleshooting page exists in MeshForge, and every
  code MeshForge shows a user has an entry: the console's stop codes, the
  console's join problems, the Windows installer's outcomes, the upgrade
  notices, ``version_unsupported``, the controller's managed-database reasons
  and ``db status``'s ``rollback_failed``;
* every image a user page shows exists; every missing one is a
  ``TODO-SCREENSHOT`` placeholder (the Windows ones come from the 🧑 0.17.8 lab,
  Q41) - and the placeholders are listed here so nobody forgets them.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
USER_DOCS: Final = REPO / "docs" / "user"
TROUBLESHOOTING: Final = USER_DOCS / "troubleshooting.md"
_CODE_LINE: Final = re.compile(r"^Codes?: (.+)$", re.MULTILINE)
_TICKED: Final = re.compile(r"`([a-z_]+)`")
_TABLE_CODE: Final = re.compile(r"^\| (`[a-z_]+`(?:, `[a-z_]+`)*) \|", re.MULTILINE)


def documented_codes(text: str) -> set[str]:
    codes: set[str] = set()
    for line in _CODE_LINE.findall(text):
        codes.update(_TICKED.findall(line))
    for cell in _TABLE_CODE.findall(text):
        codes.update(_TICKED.findall(cell))
    return codes


def join_codes() -> set[str]:
    pytest.importorskip("cryptography", reason="needs the agent extra")
    from meshforge.agent.console import joining
    from meshforge.agent.enrollment import JoinError, JoinReason

    codes = {joining.explain(JoinError(reason, "x")).code for reason in JoinReason}
    codes |= {
        joining.explain(JoinError(JoinReason.REFUSED, "x", controller_reason=key)).code
        for key in joining._BY_CONTROLLER
    }
    return codes


def shown_codes() -> set[str]:
    """Every code MeshForge can show a user (module docstring)."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.agent.console.supervisor import STOP_CODES
    from meshforge.agent.winsetup import SetupOutcome
    from meshforge.common.upgrade_state import NoticeKind
    from meshforge.common.versioning import VERSION_UNSUPPORTED
    from meshforge.controller.db_managed.errors import ManagedDbReason

    notice_kinds = set(typing.get_args(NoticeKind.__value__)) - {"in_progress"}
    return (
        set(STOP_CODES)
        | join_codes()
        | {o.value for o in SetupOutcome if o is not SetupOutcome.OK}
        | notice_kinds
        | {VERSION_UNSUPPORTED, "rollback_failed"}
        | {r.value for r in ManagedDbReason}
    )


def test_every_documented_code_exists_and_every_shown_code_is_documented() -> None:
    documented = documented_codes(TROUBLESHOOTING.read_text(encoding="utf-8"))
    shown = shown_codes()
    assert sorted(documented - shown) == [], "documented but not a MeshForge code"
    assert sorted(shown - documented) == [], "MeshForge shows these with no entry"


def test_rollback_failed_is_really_what_db_status_reports() -> None:
    admin = REPO / "src/meshforge/controller/db_managed/admin.py"
    assert '"rollback_failed":' in admin.read_text(encoding="utf-8")


def test_every_stop_reason_in_the_supervisor_is_a_listed_code() -> None:
    from meshforge.agent.console import supervisor

    source = Path(supervisor.__file__).read_text(encoding="utf-8")
    literals = set(re.findall(r'StopReason\(\s*"([a-z_]+)"', source))
    literals |= set(re.findall(r'code = "([a-z_]+)"', source))
    assert literals <= set(supervisor.STOP_CODES), literals - set(supervisor.STOP_CODES)


def test_the_parser_reads_both_forms() -> None:
    text = "Code: `a_b`\nCodes: `c`, `d`\n| `e`, `f` | x | y |\nCode-less line `g`\n"
    assert documented_codes(text) == {"a_b", "c", "d", "e", "f"}


# --------------------------------------------------------------- screenshots
_IMAGE: Final = re.compile(r"!\[[^\]]*\]\((img/[^)]+)\)")
_TODO: Final = re.compile(r"<!-- TODO-SCREENSHOT: ([\w.-]+\.png) - (.+?) -->")


def screenshot_todos() -> list[tuple[str, str, str]]:
    """(page, file, what) of every placeholder still to be filled."""
    found = []
    for page in sorted(USER_DOCS.glob("*.md")):
        for name, what in _TODO.findall(page.read_text(encoding="utf-8")):
            found.append((page.name, name, what))
    return found


def test_every_shown_image_exists() -> None:
    missing = []
    for page in sorted(USER_DOCS.glob("*.md")):
        for rel in _IMAGE.findall(page.read_text(encoding="utf-8")):
            if not (USER_DOCS / rel).is_file():
                missing.append(f"{page.name}: {rel}")
    assert missing == [], "images referenced but not in docs/user/img (or a TODO)"


def test_screenshot_placeholders_are_listed_and_owned() -> None:
    todos = screenshot_todos()
    # Q41: the console and dashboard screenshots are taken by the agents from
    # the real UI (tests/e2e/test_v017_g5_doc_screenshots.py); the Windows
    # installer and the packaged controller's setup page come from the 0.17.8
    # lab (the e2e harness would show test paths and a loopback warning).
    for page, name, what in todos:
        assert name.startswith(("windows-", "controller-")), (page, name)
        assert "🧑" in what, (page, name, "say who provides it")
    print(
        "TODO-SCREENSHOT placeholders:", *(f"{p}: {n}" for p, n, _ in todos), sep="\n  "
    )
