"""V0.17 G5 tester round 2: ``meshforge-agent console-access add|remove`` and
the release workflow's tag check.

* Linux path of the command, through the real typer app.
* The Windows implementation (``winservice_admin.cmd_console_access``) cannot be
  imported off Windows (it asserts the platform and needs pywin32), so its
  source is extracted and run against fakes: the REAL function body, fake
  elevation / SCM / account lookup.
* ``release.yml``: the tag check's own regex is lifted out of the workflow and
  run, the way the workflow runs it (bash ``[[ =~ ]]``), against hostile tags.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
ADMIN_SRC: Final = REPO / "src/meshforge/agent/winservice_admin.py"
RELEASE: Final = REPO / ".github/workflows/release.yml"


# ------------------------------------------------------------ Linux path
@pytest.mark.skipif(sys.platform == "win32", reason="the Linux answer")
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["console-access", "add", "--user", "bob"], "usermod -aG meshforge-admin"),
        (["console-access", "remove", "--user", "bob"], "gpasswd -d"),
        (["console-access", "add"], "meshforge-admin"),
        (["console-access", "add", "--sid", "S-1-5-21-1-2-3-1001"], "meshforge-admin"),
    ],
)
def test_linux_says_how_and_changes_nothing(args: list[str], needle: str) -> None:
    pytest.importorskip("cryptography", reason="needs the agent extra")
    from typer.testing import CliRunner

    from meshforge.agent.main import app

    result = CliRunner().invoke(app, args)
    out = result.output + (getattr(result, "stderr", "") or "")
    assert result.exit_code == 2, out  # USAGE_ERROR: "for Windows"
    assert needle in out and "Windows" in out


@pytest.mark.parametrize(
    "action", ["", "Add", "ADD", "grant", "add ", "--user", "../x"]
)
def test_an_unknown_action_is_a_usage_error_on_every_platform(action: str) -> None:
    pytest.importorskip("cryptography", reason="needs the agent extra")
    from typer.testing import CliRunner

    from meshforge.agent.main import app

    result = CliRunner().invoke(app, ["console-access", action])
    assert result.exit_code != 0
    assert result.exit_code != 0 and "meshforge-admin" not in result.output.replace(
        "ACTION is add or remove", ""
    )


def test_the_command_is_listed_in_help_and_named_by_the_installer_message() -> None:
    pytest.importorskip("cryptography", reason="needs the agent extra")
    from typer.testing import CliRunner

    from meshforge.agent.main import app

    assert "console-access" in CliRunner().invoke(app, ["--help"]).output
    text = (REPO / "src/meshforge/agent/winsetup.py").read_text(encoding="utf-8")
    assert "meshforge-agent console-access add --user" in text


# --------------------------------------------- the Windows body, real source
class OpsError(Exception):
    pass


class FakeOps:
    def __init__(self, readers: list[str] | None = None, fail: bool = False) -> None:
        self.granted: list[list[str]] = []
        self.revoked: list[list[str]] = []
        self.readers = readers or []
        self.fail = fail

    def console_readers(self) -> list[str]:
        return self.readers

    def grant_console(self, sids: Any) -> None:
        if self.fail:
            raise OpsError("icacls failed (exit 5)")
        self.granted.append(list(sids))

    def revoke_console(self, sids: Any) -> None:
        self.revoked.append(list(sids))


ACCOUNTS: Final = {"alice": "S-1-5-21-1-2-3-1001", "bob": "S-1-5-21-1-2-3-1002",
                   "SYSTEM": "S-1-5-18", "Administrators": "S-1-5-32-544",
                   "NT SERVICE\\MeshForgeAgent": "S-1-5-80-9"}  # fmt: skip


def _admin_constant(name: str) -> Any:
    """A module-level constant of winservice_admin, evaluated from its source."""
    tree = ast.parse(ADMIN_SRC.read_text(encoding="utf-8"))
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
    )
    return eval(compile(ast.Expression(node.value), "x", "eval"), {"re": re})  # noqa: S307


def load(admin: bool = True, ops: FakeOps | None = None) -> tuple[Any, FakeOps]:
    tree = ast.parse(ADMIN_SRC.read_text(encoding="utf-8"))
    func = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_console_access"
    )
    source = ast.unparse(func)
    ops = ops or FakeOps()

    class ExitCode:
        OK = 0
        FAILURE = 1
        USAGE_ERROR = 2

    def need_admin(command: str) -> int | None:
        return None if admin else 1

    def account_sid(name: str) -> str:
        if name not in ACCOUNTS:
            raise OpsError(f"no Windows account {name!r}")
        return ACCOUNTS[name]

    ns: dict[str, Any] = {
        "Sequence": list,
        "sys": sys,
        "ExitCode": ExitCode,
        "OpsError": OpsError,
        "_need_admin": need_admin,
        "_ops": lambda: ops,
        "account_sid": account_sid,
        "_PROTECTED": frozenset({"S-1-5-18", "S-1-5-32-544"}),
        "_SID": _admin_constant("_SID"),
        "service_sid": lambda: "S-1-5-80-9",
        "print": lambda *a, **k: None,
    }
    exec(compile("from __future__ import annotations\n" + source, "x", "exec"), ns)  # noqa: S102
    return ns["cmd_console_access"], ops


def test_not_elevated_is_refused_and_touches_nothing() -> None:
    run, ops = load(admin=False)
    assert run("add", ["alice"], [], False) == 1
    assert ops.granted == [] and ops.revoked == []


@pytest.mark.parametrize("action", ["add", "remove"])
def test_a_named_user_is_granted_or_revoked_by_sid(action: str) -> None:
    run, ops = load()
    assert run(action, ["alice", "bob"], [], False) == 0
    got = ops.granted if action == "add" else ops.revoked
    assert got == [["S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002"]]


def test_duplicates_are_collapsed() -> None:
    run, ops = load()
    run("add", ["alice", "alice"], ["S-1-5-21-1-2-3-1001"], False)
    assert ops.granted == [["S-1-5-21-1-2-3-1001"]]


def test_an_unknown_user_changes_nothing_even_next_to_a_good_one() -> None:
    run, ops = load()
    assert run("add", ["alice", "nobody-here"], [], False) == 2
    assert ops.granted == []  # all or nothing


@pytest.mark.parametrize(
    "sid", ["Everyone", "alice", "", "*S-1-1-0", "1-5-21", "s-1-5-21-1"]
)
def test_something_that_is_not_a_sid_is_never_granted(sid: str) -> None:
    run, ops = load()
    assert run("add", [], [sid], False) == 2
    assert ops.granted == []


@pytest.mark.parametrize(
    "who", ["SYSTEM", "Administrators", "NT SERVICE\\MeshForgeAgent"]
)
def test_protected_accounts_and_the_service_are_refused(who: str) -> None:
    run, ops = load()
    assert run("add", [who], [], False) == 2
    assert run("remove", [who], [], False) == 2
    assert ops.granted == [] and ops.revoked == []


def test_a_protected_sid_hidden_among_good_ones_blocks_the_whole_call() -> None:
    run, ops = load()
    assert run("add", ["alice"], ["S-1-5-32-544"], False) == 2
    assert ops.granted == []


def test_nothing_named_is_a_usage_error() -> None:
    run, _ = load()
    assert run("add", [], [], False) == 2


def test_session_user_alone_is_enough_and_an_empty_session_is_not() -> None:
    run, ops = load(ops=FakeOps(readers=["S-1-5-21-1-2-3-1001"]))
    assert run("add", [], [], True) == 0
    assert ops.granted == [["S-1-5-21-1-2-3-1001"]]
    run2, ops2 = load(ops=FakeOps(readers=[]))
    assert run2("add", [], [], True) == 2 and ops2.granted == []


def test_a_failing_icacls_is_a_failure_exit_not_a_traceback() -> None:
    run, _ = load(ops=FakeOps(fail=True))
    assert run("add", ["alice"], [], False) == 1


@pytest.mark.parametrize(
    "sid",
    [
        "S-1-1-0:(OI)(CI)F",
        "S-1-5-21-1-2-3-1001,*S-1-1-0",
        "S-1-x",
        "S-1-",
        "S-1",
        "S-1-5-21-1001\n",
        "S-1-5- 21",
        "S-1-5-\u0661",
    ],
)
def test_g5_t5_a_malformed_sid_is_refused_before_icacls(sid: str) -> None:
    run, ops = load()
    assert run("add", [], [sid], False) == 2
    assert ops.granted == []


def test_the_icacls_argv_builders_never_join_sids_into_one_string() -> None:
    from pathlib import PureWindowsPath

    from meshforge.agent.wininstall import console_grant_argv, console_revoke_argv

    folder = PureWindowsPath(r"C:\ProgramData\MeshForge\console")
    argv = console_grant_argv(folder, ["S-1-5-21-1", "S-1-5-21-1", "S-1-5-21-2"])
    assert argv.count("/grant") == 2  # deduplicated, one argument each
    assert all(a.startswith("*S-1-") for a in argv[3::2])
    with pytest.raises(ValueError):
        console_grant_argv(folder, [])
    assert "/remove:g" in console_revoke_argv(folder, ["S-1-5-21-1"])


# ------------------------------------------------------------ release.yml
def _tag_regex() -> str:
    text = RELEASE.read_text(encoding="utf-8")
    assert "export LC_ALL=C" in text
    match = re.search(r'\[\[ "\$TAG" =~ (\S+) \]\]', text)
    assert match, "the release workflow no longer checks the tag with [[ =~ ]]"
    return match.group(1)


def _accepts(tag: str) -> bool:
    done = subprocess.run(
        ["bash", "-c", 'export LC_ALL=C; [[ "$TAG" =~ $RE ]]'],
        env={"TAG": tag, "RE": _tag_regex(), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="bash")
@pytest.mark.parametrize(
    ("tag", "ok"),
    [
        ("v0.17.0", True),
        ("v1.2.30", True),
        ("v10.200.3000", True),
        ("v0.17", False),
        ("0.17.0", False),
        ("V0.17.0", False),
        ("v0.17.0-rc1", False),
        ("v0.17.0.1", False),
        ("v0.17.0 ", False),
        (" v0.17.0", False),
        ("v0.17.x", False),
        ("v0.17.0;rm -rf /", False),
        ("v0.17.0$(id)", False),
        ("v0.17.0`id`", False),
        ("v..", False),
        ("v0.17.0/../x", False),
        ("", False),
        ("refs/tags/v0.17.0", False),
        ("v\u0660.\u0661.\u0662", False),
    ],
)
def test_the_release_tag_check(tag: str, ok: bool) -> None:
    assert _accepts(tag) is ok


@pytest.mark.skipif(sys.platform == "win32", reason="bash")
@pytest.mark.parametrize(
    "tag", ["v0.17.0\nv9.9.9\nevil", "v0.17.0\n", "\nv0.17.0", "evil\nv0.17.0"]
)
def test_g5_t6_a_multiline_tag_is_refused(tag: str) -> None:
    assert _accepts(tag) is False


def test_the_push_trigger_is_a_release_tag_glob_only() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert 'tags: ["v[0-9]+.[0-9]+.[0-9]+"]' in text
    assert "branches" not in textwrap.dedent(text.split("jobs:")[0])
    # a release is built only from the tag, never from a branch head
    assert text.count("refs/tags/${{ env.TAG }}") >= 3


def test_the_tag_must_equal_the_project_version_check_is_exact() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert '[ "$TAG" != "v$version" ]' in text


def test_g5_t7_linux_remove_advice_works_on_ubuntu_22_04() -> None:
    pytest.importorskip("cryptography", reason="needs the agent extra")
    from typer.testing import CliRunner

    from meshforge.agent.main import app

    out = CliRunner().invoke(app, ["console-access", "remove", "--user", "bob"]).output
    assert "usermod -rG" not in out
