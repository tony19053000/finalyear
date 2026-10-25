"""V0.17 G4: the pure Windows install pieces, pinned on every OS.

``wininstall`` (program layout, PATH shims, command-line parsing, tool argv),
``common.windows_service`` (service SID, who may use a private file),
``winservice`` additions (console folder ACL, child environment), the
multi-SID ``icacls`` builder and the Windows decisions of the admin CLI (Q32).
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from meshforge.agent import system_cli as agent_cli
from meshforge.agent import wininstall as wi
from meshforge.agent import winservice as ws
from meshforge.common import config
from meshforge.common import windows_service as wsvc
from meshforge.common.private_files import (
    MKDIR_700_SIDS,
    owner_only_icacls_argvs,
    private_icacls_argvs,
)
from meshforge.common.system_cli import Decision, SystemProgram, decide_windows

pytestmark = pytest.mark.unit

ROOT = PureWindowsPath(r"C:\Program Files\MeshForge")
PY = str(ROOT / "0.17.0-1" / "python" / "python.exe")
USER_SID = "S-1-5-21-111-222-333-1001"


# ------------------------------------------------------------ program layout
def test_program_layout_from_the_bundle_python() -> None:
    layout = wi.program_layout(PY)
    assert layout.root == ROOT
    assert layout.version_dir == "0.17.0-1"
    assert str(layout.python_exe) == PY
    assert layout.pythonw_exe.name == "pythonw.exe"
    assert layout.current_file == ROOT / "current.txt"
    assert layout.shim_dir == ROOT / "bin"
    assert wi.program_layout(PY.replace("python.exe", "pythonw.exe")) == layout


@pytest.mark.parametrize(
    "bad",
    [
        r"C:\python.exe",
        r"C:\Program Files\MeshForge\0.17.0-1\python\node.exe",
        r"C:\Program Files\MeshForge\0.17.0-1\bin\python.exe",
        r"C:\python\python.exe",
    ],
)
def test_program_layout_refuses_anything_else(bad: str) -> None:
    with pytest.raises(ValueError, match="python"):
        wi.program_layout(bad)


def test_shim_runs_the_named_versions_launcher_with_all_arguments() -> None:
    text = wi.shim_text(wi.program_layout(PY), "meshforge-agent")
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    # the last command is the call, so cmd's exit code is the launcher's
    assert lines[2] == r'"%~dp0..\0.17.0-1\bin\meshforge-agent.cmd" %*'
    assert lines[3] == ""
    assert "\n" not in text.replace("\r\n", "")
    with pytest.raises(ValueError, match="no shim"):
        wi.shim_text(wi.program_layout(PY), "meshforge-console-open")


# -------------------------------------------------------------- binPath parse
@pytest.mark.parametrize(
    "argv",
    [
        [PY, "-I", "-m", "meshforge.agent.winservice", "host", "--config", r"C:\a b"],
        [PY, "a\\", 'say "hi"', "x\\\\", "", 'trail\\"q'],
        [r"C:\x.exe", "plain"],
    ],
)
def test_parse_command_line_inverts_list2cmdline(argv: list[str]) -> None:
    assert wi.parse_command_line(subprocess.list2cmdline(argv)) == argv


def test_parse_command_line_edge_cases() -> None:
    assert wi.parse_command_line("") == []
    assert wi.parse_command_line("   ") == []
    # argv[0]: backslashes are literal, quotes only delimit
    assert wi.parse_command_line(r'"C:\dir\" x') == ["C:\\dir\\", "x"]
    # "" inside quotes is a literal quote (MS C runtime)
    assert wi.parse_command_line('p "a""b"') == ["p", 'a"b']
    # an unterminated quote runs to the end
    assert wi.parse_command_line('"C:\\p q') == ["C:\\p q"]


def test_service_python_reads_only_our_bin_path() -> None:
    layout = ws.machine_layout(r"C:\ProgramData")
    spec = ws.build_service_spec(PY, layout)
    assert wi.service_python(spec.bin_path) == PY
    assert wi.service_python(r'"C:\Windows\x.exe" -k netsvcs') is None
    assert wi.service_python(f'"{PY}" -I -m other host') is None
    assert wi.service_python("") is None


# ---------------------------------------------------------------------- PATH
def test_path_add_is_idempotent_and_insensitive() -> None:
    entry = str(ROOT / "bin")
    value = r"C:\Windows\system32;C:\Windows"
    added = wi.path_add(value, entry)
    assert added == value + ";" + entry
    assert wi.path_add(added, entry) == added
    for variant in (entry.upper(), entry + "\\", f'"{entry}"'):
        assert wi.path_add(value + ";" + variant, entry) == value + ";" + variant
    assert wi.path_add("", entry) == entry
    assert wi.path_add("a;;b;", entry) == f"a;b;{entry}"


def test_path_remove_removes_every_copy_only() -> None:
    entry = str(ROOT / "bin")
    value = f"C:\\a;{entry};C:\\b;{entry.lower()}\\;C:\\MeshForgeOther"
    assert wi.path_remove(value, entry) == r"C:\a;C:\b;C:\MeshForgeOther"
    assert wi.path_remove(r"C:\a", entry) == r"C:\a"


# ------------------------------------------------------------- tool argvs
def test_schtasks_argv_names_only_the_legacy_task() -> None:
    assert wi.schtasks_argv("query") == [
        "schtasks", "/Query", "/TN", "MeshForgeAgent", "/FO", "CSV", "/V", "/NH",
    ]  # fmt: skip
    assert wi.schtasks_argv("end")[-2:] == ["/TN", "MeshForgeAgent"]
    assert wi.schtasks_argv("disable")[-1] == "/DISABLE"
    assert wi.schtasks_argv("delete")[-1] == "/F"
    with pytest.raises(ValueError, match="unknown"):
        wi.schtasks_argv("run")


def test_docker_users_argv_is_the_service_account_only() -> None:
    assert wi.docker_users_argv(add=True) == [
        "net", "localgroup", "docker-users", r"NT SERVICE\MeshForgeAgent", "/add",
    ]  # fmt: skip
    assert wi.docker_users_argv(add=False)[-1] == "/delete"


def test_console_grant_and_revoke_argv() -> None:
    folder = PureWindowsPath(r"C:\ProgramData\MeshForge\console")
    assert wi.console_grant_argv(folder, [USER_SID, USER_SID, "S-1-5-21-9"]) == [
        "icacls",
        str(folder),
        "/grant",
        f"*{USER_SID}:(OI)(CI)RX",
        "/grant",
        "*S-1-5-21-9:(OI)(CI)RX",
    ]
    assert wi.console_revoke_argv(folder, [USER_SID]) == [
        "icacls",
        str(folder),
        "/remove:g",
        f"*{USER_SID}",
    ]
    for build in (wi.console_grant_argv, wi.console_revoke_argv):
        with pytest.raises(ValueError, match="no SID"):
            build(folder, [])


# ------------------------------------------------------ common.windows_service
def test_common_service_facts_match_the_agent_module() -> None:
    assert wsvc.SERVICE_NAME == ws.SERVICE_NAME == "MeshForgeAgent"
    assert wsvc.service_sid() == ws.service_sid()
    assert wsvc.INSTALL_MODE_ENV_VAR == config.INSTALL_MODE_ENV_VAR
    assert MKDIR_700_SIDS[0] == wsvc.SID_SYSTEM
    assert MKDIR_700_SIDS[1] == wsvc.SID_ADMINISTRATORS


@pytest.mark.parametrize(
    ("value", "expected"),
    [("system", True), (" SYSTEM ", True), ("user", False), ("", False)],
)
def test_system_mode_reads_the_install_mode(value: str, expected: bool) -> None:
    assert wsvc.system_mode({"MESHFORGE_INSTALL_MODE": value}) is expected
    assert wsvc.system_mode({}) is False


def test_private_files_belong_to_the_user_or_to_the_service() -> None:
    assert wsvc.windows_private_sids(USER_SID, system=False) == (USER_SID,)
    # system mode: whoever writes (the service, an elevated admin CLI) - the
    # service, SYSTEM and Administrators; never the writing user's own SID
    assert wsvc.windows_private_sids(USER_SID, system=True) == (
        wsvc.service_sid(),
        "S-1-5-18",
        "S-1-5-32-544",
    )


def test_private_icacls_argvs_one_full_ace_per_sid_and_strips_the_rest() -> None:
    sids = wsvc.windows_private_sids(USER_SID, system=True)
    logon = "S-1-5-5-0-12345"
    grant, strip = private_icacls_argvs(Path("node.key"), sids, logon)
    assert grant == [
        "icacls",
        "node.key",
        *(item for sid in sids for item in ("/grant:r", f"*{sid}:(F)")),
    ]
    # SYSTEM and Administrators are kept (they are in sids); CREATOR OWNER and
    # the logon session's ACE go
    assert strip == [
        "icacls",
        "node.key",
        "/inheritance:r",
        "/remove:g",
        "*S-1-3-4",
        f"*{logon}",
    ]
    grant_dir, _ = private_icacls_argvs(Path("identity"), sids, directory=True)
    assert grant_dir[3].endswith(":(OI)(CI)(F)")
    with pytest.raises(ValueError, match="at least one SID"):
        private_icacls_argvs(Path("x"), ())


def test_owner_only_is_the_single_sid_case() -> None:
    assert owner_only_icacls_argvs(
        Path("p"), USER_SID, "S-1-5-5-0-1"
    ) == private_icacls_argvs(Path("p"), (USER_SID,), "S-1-5-5-0-1")


# ------------------------------------------------------------- winservice
def test_child_env_sets_system_mode_without_dropping_the_rest() -> None:
    env = ws.child_env({"PATH": r"C:\Windows", "MESHFORGE_INSTALL_MODE": "user"})
    assert env == {"PATH": r"C:\Windows", "MESHFORGE_INSTALL_MODE": "system"}


def test_layout_folders_include_the_console_token_folder_in_order() -> None:
    layout = ws.machine_layout(r"C:\ProgramData")
    assert layout.folders[0] == layout.root
    assert layout.console_dir == layout.root / "console"
    assert set(layout.folders) == {
        layout.root,
        layout.config_dir,
        layout.identity_dir,
        layout.cache_dir,
        layout.logs_dir,
        layout.console_dir,
    }


def test_console_folder_readers_get_read_only_and_never_duplicate_the_admins() -> None:
    layout = ws.machine_layout(r"C:\ProgramData")
    svc = ws.service_sid()
    plan = {
        acl.path: acl
        for acl in ws.acl_plan(
            layout, svc, [USER_SID, USER_SID, svc, "S-1-5-18", "S-1-5-32-544"]
        )
    }
    console = plan[layout.console_dir]
    readers = [g for g in console.grants if g.sid == USER_SID]
    assert readers == [ws.Grant(USER_SID, ws.Access.READ)]
    assert [g.sid for g in console.grants].count(svc) == 1
    assert [g.sid for g in console.grants].count("S-1-5-18") == 1
    # no other folder gets the reader: the token folder only (Q30)
    for path, acl in plan.items():
        if path != layout.console_dir:
            assert USER_SID not in {g.sid for g in acl.grants}
    # the console edits policy + node name in the root: the service may modify
    root = {g.sid: g.access for g in plan[layout.root].grants}
    assert root[svc] is ws.Access.MODIFY
    assert {g.sid: g.access for g in plan[layout.config_dir].grants}[svc] is (
        ws.Access.READ
    )


def test_install_and_firewall_argv_never_widen_the_rule() -> None:
    argv = ws.firewall_add_argv(ws.peer_firewall_rule(47555))
    assert "localport=47555" in argv
    assert "profile=private,domain" in argv
    assert ws.firewall_delete_argv(ws.PEER_FIREWALL_RULE)[-1] == (
        "name=MeshForge-Agent-Peer"
    )


# ---------------------------------------------------- admin CLI (Q32)
AGENT = agent_cli.AGENT_PROGRAM


def _decide(args: list[str], **over: object) -> Decision:
    kw: dict[str, object] = {
        "program": AGENT,
        "environ": {"PROGRAMDATA": r"C:\ProgramData"},
        "installed": True,
        "bundled": True,
        "elevated": True,
    }
    kw.update(over)
    return decide_windows(args, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args", [["identity", "show"], ["join", "-"], ["policy", "set", "a=b"]]
)
def test_elevated_packaged_cli_acts_on_the_service_state_in_place(
    args: list[str],
) -> None:
    decision = _decide(args)
    assert decision.action == "inplace"
    # (rendered with the host's separator off Windows)
    assert "ProgramData" in decision.message and "MeshForge)" in decision.message


def test_non_elevated_packaged_cli_is_refused_with_both_ways_out() -> None:
    decision = _decide(["identity", "show"], elevated=False)
    assert decision.action == "refuse"
    assert "Run as administrator" in decision.message
    assert "MeshForge console" in decision.message


def test_run_is_refused_even_elevated_and_names_sc_exe() -> None:
    decision = _decide(["run"])
    assert decision.action == "refuse"
    assert "sc.exe start MeshForgeAgent" in decision.message
    assert decision.message.startswith("meshforge-agent run:")


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--version"],
        ["--help"],
        ["identity", "show", "--help"],
        ["migrate-identity"],
    ],
)
def test_harmless_or_self_elevating_commands_run_in_place(args: list[str]) -> None:
    assert _decide(args, elevated=False).action == "none"


def test_service_child_and_explicit_mode_are_left_alone() -> None:
    for mode in ("system", "user"):
        environ = {"MESHFORGE_INSTALL_MODE": mode}
        assert _decide(["run"], environ=environ).action == "none"


def test_not_installed_is_none_and_a_checkout_only_warns() -> None:
    assert _decide(["identity", "show"], installed=False).action == "none"
    decision = _decide(["identity", "show"], bundled=False, elevated=False)
    assert decision.action == "warn"
    assert "YOUR user-mode state" in decision.message


def test_only_the_agent_program_has_a_windows_install() -> None:
    assert AGENT.windows is True
    assert SystemProgram(prog="x", program_dir=Path("/opt/x")).windows is False


def test_windows_installed_needs_both_marker_files(tmp_path: Path) -> None:
    from meshforge.common.system_cli import windows_installed

    env = {"PROGRAMDATA": str(tmp_path / "pd"), "PROGRAMFILES": str(tmp_path / "pf")}
    assert not windows_installed(env)
    (tmp_path / "pd" / "MeshForge" / "config").mkdir(parents=True)
    assert not windows_installed(env)
    (tmp_path / "pf" / "MeshForge").mkdir(parents=True)
    (tmp_path / "pf" / "MeshForge" / "current.txt").write_text("0.17.0-1\r\n")
    assert windows_installed(env)
