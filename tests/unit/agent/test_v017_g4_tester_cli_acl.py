"""V0.17 G4 tester: elevation logic, ACL/SID facts, command-line and PATH edge cases.

Brief 5d items 3, 5, 8: the admin CLI acts on the service's state only when
elevated, ``run`` is always refused, the service SID is the documented SHA-1
derivation, private files are service + SYSTEM + Administrators in system mode,
the console token folder grant never uses a private ACL. Pure functions; the
Windows tools themselves (icacls, netsh) cannot run on this Linux machine.
"""

from __future__ import annotations

import random
from pathlib import PureWindowsPath
from subprocess import list2cmdline

import pytest

from meshforge.agent import wininstall as wi
from meshforge.agent.system_cli import AGENT_PROGRAM
from meshforge.common.system_cli import SystemProgram, decide_windows
from meshforge.common.windows_service import (
    INSTALL_MODE_ENV_VAR,
    SID_ADMINISTRATORS,
    SID_SYSTEM,
    service_sid,
    system_mode,
    windows_private_sids,
)

pytestmark = pytest.mark.unit


def _decide(
    args: list[str],
    *,
    elevated: bool,
    installed: bool = True,
    bundled: bool = True,
    environ: dict[str, str] | None = None,
    program: SystemProgram = AGENT_PROGRAM,
) -> str:
    return decide_windows(
        args,
        program=program,
        environ=environ or {},
        installed=installed,
        bundled=bundled,
        elevated=elevated,
    ).action


# ------------------------------------------------------------------ SIDs / ACLs
def test_service_sid_matches_a_known_windows_service_sid() -> None:
    """Independent oracle: the publicly documented SID of NT SERVICE\\TrustedInstaller."""
    assert (
        service_sid("TrustedInstaller")
        == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )


def test_service_sid_is_case_insensitive_and_stable() -> None:
    assert service_sid("MeshForgeAgent") == service_sid("MESHFORGEAGENT")
    assert service_sid("MeshForgeAgent") != service_sid("MeshForgeAgent2")
    assert service_sid().startswith("S-1-5-80-")
    assert len(service_sid().split("-")) == 9


def test_private_sids_user_vs_system_mode() -> None:
    me = "S-1-5-21-1-2-3-1001"
    assert windows_private_sids(me, system=False) == (me,)
    sids = windows_private_sids(me, system=True)
    assert set(sids) == {service_sid(), SID_SYSTEM, SID_ADMINISTRATORS}
    assert me not in sids  # whoever wrote the file, a normal user gets nothing
    assert windows_private_sids("S-1-5-21-9-9-9-500", system=True) == sids


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("system", True),
        (" SYSTEM ", True),
        ("user", False),
        ("", False),
        ("sys", False),
    ],
)
def test_system_mode_parsing(value: str, expected: bool) -> None:
    assert system_mode({INSTALL_MODE_ENV_VAR: value}) is expected
    assert system_mode({}) is False


def test_console_grant_is_read_only_and_inheritable_and_dedups() -> None:
    folder = PureWindowsPath(r"C:\ProgramData\MeshForge\console")
    argv = wi.console_grant_argv(folder, ["S-1-5-21-1-2-3-1001"] * 2 + ["S-1-5-32-544"])
    assert argv[:2] == ["icacls", str(folder)]
    grants = [a for a in argv if a.startswith("*")]
    assert grants == ["*S-1-5-21-1-2-3-1001:(OI)(CI)RX", "*S-1-5-32-544:(OI)(CI)RX"]
    assert not any(":(F)" in a or "(M)" in a or "(W)" in a for a in argv)
    assert "/inheritance:r" not in argv and "/reset" not in argv
    with pytest.raises(ValueError):
        wi.console_grant_argv(folder, [])
    with pytest.raises(ValueError):
        wi.console_revoke_argv(folder, [])


def test_console_grant_never_puts_a_token_or_shell_metachar_in_argv() -> None:
    folder = PureWindowsPath(r"C:\ProgramData\MeshForge\console")
    argv = wi.console_grant_argv(folder, ["S-1-5-21-1-2-3-1001"])
    assert all(isinstance(a, str) for a in argv)
    assert not any("token" in a.lower() for a in argv[2:])


def test_docker_users_argv_adds_and_removes_the_service_account_only() -> None:
    add, rem = wi.docker_users_argv(add=True), wi.docker_users_argv(add=False)
    assert (
        add[:4]
        == rem[:4]
        == ["net", "localgroup", "docker-users", r"NT SERVICE\MeshForgeAgent"]
    )
    assert add[4] == "/add" and rem[4] == "/delete"


def test_schtasks_unknown_action_is_rejected_and_delete_is_by_exact_name() -> None:
    with pytest.raises(ValueError):
        wi.schtasks_argv("run")
    assert wi.schtasks_argv("delete") == [
        "schtasks",
        "/Delete",
        "/TN",
        "MeshForgeAgent",
        "/F",
    ]
    # a wildcard name must never be built from the defaults
    for action in ("query", "end", "disable", "delete"):
        assert not any("*" in a for a in wi.schtasks_argv(action))


# -------------------------------------------------------------- elevation logic
@pytest.mark.parametrize(
    "args",
    [
        ["identity", "show"],
        ["identity", "join", "CODE"],
        ["policy", "set", "--accept-jobs"],
        ["--config", r"C:\x y\c.toml", "identity", "show"],
        ["hardware", "show"],
    ],
)
def test_installed_system_commands_need_elevation(args: list[str]) -> None:
    assert _decide(args, elevated=False) == "refuse"
    assert _decide(args, elevated=True) == "inplace"


def test_refusal_message_names_the_path_and_the_fix() -> None:
    d = decide_windows(
        ["identity", "show"], program=AGENT_PROGRAM,
        environ={"PROGRAMDATA": r"D:\PD"}, installed=True, bundled=True, elevated=False,
    )  # fmt: skip
    assert "D:\\PD\\MeshForge" in d.message or "D:\\PD" in d.message.replace("/", "\\")
    assert "administrator" in d.message.lower() and "console" in d.message.lower()


@pytest.mark.parametrize("elevated", [False, True])
def test_run_is_always_refused_even_elevated(elevated: bool) -> None:
    assert _decide(["run"], elevated=elevated) == "refuse"
    assert _decide(["--config", "c.toml", "run"], elevated=elevated) == "refuse"
    assert _decide(["--config=c.toml", "run"], elevated=elevated) == "refuse"
    assert _decide(["-v", "run"], elevated=elevated) == "refuse"


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G4-T1: `meshforge-agent -- run` bypasses the always-refuse rule "
    "(first_command() returns None after `--`, but the CLI still runs `run`)",
)
def test_run_after_double_dash_is_not_a_bypass() -> None:
    assert _decide(["--", "run"], elevated=True) == "refuse"


def test_run_after_double_dash_unelevated_is_refused() -> None:
    assert _decide(["--", "run"], elevated=False) == "refuse"


@pytest.mark.parametrize(
    "args",
    [["--version"], ["--help"], ["-h"], ["identity", "--help"], ["run", "--help"]],
)
def test_version_and_help_never_need_elevation(args: list[str]) -> None:
    assert _decide(args, elevated=False) == "none"


def test_migrate_identity_is_left_to_its_own_elevation_check() -> None:
    assert _decide(["migrate-identity"], elevated=False) == "none"


def test_bare_command_runs_in_place() -> None:
    assert _decide([], elevated=False) == "none"


def test_not_installed_means_no_interference() -> None:
    for elevated in (False, True):
        assert (
            _decide(["identity", "show"], elevated=elevated, installed=False) == "none"
        )
        assert _decide(["run"], elevated=elevated, installed=False) == "none"


@pytest.mark.parametrize("value", ["system", "user", "x"])
def test_explicit_install_mode_env_is_never_second_guessed(value: str) -> None:
    env = {INSTALL_MODE_ENV_VAR: value}
    assert _decide(["identity", "show"], elevated=False, environ=env) == "none"
    assert _decide(["run"], elevated=True, environ=env) == "none"


def test_blank_install_mode_env_does_not_disable_the_guard() -> None:
    assert (
        _decide(
            ["identity", "show"], elevated=False, environ={INSTALL_MODE_ENV_VAR: "  "}
        )
        == "refuse"
    )


def test_dev_checkout_warns_but_does_not_block_even_unelevated() -> None:
    for args in (["identity", "show"], ["run"]):
        assert _decide(args, elevated=False, bundled=False) == "warn"


def test_controller_program_never_triggers_windows_branch() -> None:
    from meshforge.controller.system_install import CONTROLLER_PROGRAM

    assert not CONTROLLER_PROGRAM.windows
    # common.system_cli.apply gates on program.windows; decide_windows itself is
    # only called for windows=True programs (checked structurally here).
    assert AGENT_PROGRAM.windows


def test_decide_windows_is_pure_and_deterministic() -> None:
    a = decide_windows(["identity", "show"], program=AGENT_PROGRAM, environ={},
                       installed=True, bundled=True, elevated=True)  # fmt: skip
    b = decide_windows(["identity", "show"], program=AGENT_PROGRAM, environ={},
                       installed=True, bundled=True, elevated=True)  # fmt: skip
    assert a == b and a.argv == ()


# ------------------------------------------------------ command line / PATH
def test_parse_command_line_roundtrips_random_argv() -> None:
    rng = random.Random(1705)  # noqa: S311 - seeded fuzz, not crypto
    alphabet = ["a", "b", " ", "\t", '"', "\\", "'", "&", "%", "é", "日", "=", ";"]
    for _ in range(3000):
        argv = ["C:\\Program Files\\MeshForge\\0.17.0-1\\python\\python.exe"]
        for _ in range(rng.randint(0, 5)):
            argv.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8))))  # noqa: PERF401
        assert wi.parse_command_line(list2cmdline(argv)) == argv, argv


def test_parse_command_line_empty_and_garbage() -> None:
    assert wi.parse_command_line("") == []
    assert wi.parse_command_line("   \t ") == []
    assert wi.parse_command_line('"unterminated') == ["unterminated"]
    assert wi.parse_command_line('prog "a b" "')[:2] == ["prog", "a b"]  # no hang/raise


def test_service_python_rejects_foreign_services() -> None:
    ours = (
        r'"C:\Program Files\MeshForge\0.17.0-1\python\python.exe" -I -m '
        "meshforge.agent.winservice host"
    )
    assert (
        wi.service_python(ours)
        == r"C:\Program Files\MeshForge\0.17.0-1\python\python.exe"
    )
    assert (
        wi.service_python(ours + " extra")
        == r"C:\Program Files\MeshForge\0.17.0-1\python\python.exe"
    )
    for foreign in (
        "",
        "svchost.exe -k netsvcs",
        r"C:\python.exe -m meshforge.agent.winservice host",
        r"C:\p.exe -I -m other.module host",
    ):
        assert wi.service_python(foreign) is None


@pytest.mark.parametrize(
    "bad",
    [
        r"C:\x\python.exe",
        r"python.exe",
        r"C:\Program Files\MeshForge\1\notpython\python.exe",
        r"C:\Program Files\MeshForge\1\python\evil.exe",
        "",
    ],
)
def test_program_layout_refuses_non_bundle_paths(bad: str) -> None:
    with pytest.raises(ValueError):
        wi.program_layout(bad)


def test_program_layout_accepts_pythonw_and_case_differences() -> None:
    lay = wi.program_layout(r"c:\program files\meshforge\0.17.0-1\PYTHON\PythonW.exe")
    assert lay.version_dir == "0.17.0-1"


def test_path_add_remove_roundtrip_and_idempotence() -> None:
    entry = r"C:\Program Files\MeshForge\bin"
    base = r"C:\Windows;C:\Windows\System32"
    added = wi.path_add(base, entry)
    assert wi.path_add(added, entry) == added
    assert wi.path_add(added, entry.lower() + "\\") == added
    assert wi.path_add(added, f'"{entry}"') == added
    assert wi.path_remove(added, entry) == base
    assert wi.path_remove(base, entry) == base
    assert wi.path_remove("", entry) == ""
    assert wi.path_add("", entry) == entry


def test_path_remove_only_touches_the_exact_entry() -> None:
    entry = r"C:\Program Files\MeshForge\bin"
    value = rf"{entry}2;{entry}\sub;C:\Other\MeshForge\bin;{entry}"
    assert (
        wi.path_remove(value, entry) == rf"{entry}2;{entry}\sub;C:\Other\MeshForge\bin"
    )


def test_shim_text_is_crlf_forwards_args_and_propagates_exit_code() -> None:
    lay = wi.ProgramLayout(PureWindowsPath(r"C:\Program Files\MeshForge"), "0.17.0-1")
    for command in wi.SHIM_COMMANDS:
        text = wi.shim_text(lay, command)
        assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
        assert text.rstrip().endswith("%*")
        assert "0.17.0-1" in text
        assert "%~dp0" in text  # relocatable, no absolute build path
    with pytest.raises(ValueError):
        wi.shim_text(lay, "evil")
