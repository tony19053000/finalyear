"""V0.17 G4: the pure Windows install pieces, pinned on every OS.

``wininstall`` (program layout, PATH shims, command-line parsing, tool argv),
``common.windows_service`` (service SID, who may use a private file),
``winservice`` additions (console folder ACL, child environment), the
multi-SID ``icacls`` builder and the Windows decisions of the admin CLI (Q32).
"""

from __future__ import annotations

import os
import subprocess
import sys
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
    # only the data root: its inside is not visible to a normal user
    (tmp_path / "pd" / "MeshForge").mkdir(parents=True)
    assert not windows_installed(env)
    (tmp_path / "pf" / "MeshForge").mkdir(parents=True)
    (tmp_path / "pf" / "MeshForge" / "current.txt").write_text("0.17.0-1\r\n")
    assert windows_installed(env)


# ------------------------------------------- job envs after an upgrade (F2)
def test_a_venv_whose_base_interpreter_folder_is_gone_is_damaged(
    tmp_path: Path,
) -> None:
    from meshforge.agent.envs.builder import venv_home_missing

    venv = tmp_path / "venv"
    venv.mkdir()
    assert venv_home_missing(venv) is False  # no pyvenv.cfg: other checks decide
    home = tmp_path / "MeshForge" / "0.17.0-1" / "python"
    home.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {home}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    assert venv_home_missing(venv) is False
    home.rmdir()  # the upgrade removed the old version's folder
    assert venv_home_missing(venv) is True
    (venv / "pyvenv.cfg").write_text("version = 3.12\n", encoding="utf-8")
    assert venv_home_missing(venv) is False


# ------------------------------- fix round 1 B1: "remove all data" never leaves
@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges there")
def test_data_removal_removes_links_as_links_and_never_fixes_acls_on_them(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    root = tmp_path / "MeshForge"
    (root / "envs" / "venv" / "abc").mkdir(parents=True)
    (root / "envs" / "venv" / "abc" / "pyvenv.cfg").write_text("home = x")
    (root / "identity").mkdir()
    (root / "identity" / "node.key").write_text("k")
    # a job planted links pointing out of the data root
    (root / "envs" / "trap").symlink_to(outside, target_is_directory=True)
    (root / "envs" / "venv" / "file-trap").symlink_to(outside / "precious.txt")
    fixed: list[Path] = []
    count = wi.remove_tree_no_follow(root, fixed.append)
    assert not os.path.lexists(root)
    assert (outside / "precious.txt").read_text() == "keep me"
    assert fixed == []  # nothing refused: no ACL change at all
    assert count == 9  # 7 real entries + 2 links (removed as links)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions"
)
def test_a_refused_delete_fixes_only_that_entry_then_retries(tmp_path: Path) -> None:
    root = tmp_path / "MeshForge"
    locked = root / "identity"
    locked.mkdir(parents=True)
    (locked / "node.key").write_text("k")
    (root / "envs").mkdir()
    (root / "envs" / "trap").symlink_to(tmp_path, target_is_directory=True)
    locked.chmod(0o500)  # deleting node.key is refused until access is fixed
    calls: list[list[str]] = []

    def fix_access(path: Path) -> None:
        argvs = wi.data_fix_argvs(path)
        calls.extend(argvs)
        path.parent.chmod(0o700)

    try:
        wi.remove_tree_no_follow(root, fix_access)
    finally:
        if locked.exists():
            locked.chmod(0o700)
    assert not root.exists()
    assert tmp_path.is_dir()  # the junction target survived
    assert calls, "the refused delete asked for access"
    for argv in calls:
        assert argv[0] == "icacls"
        assert "/T" not in argv and "/t" not in argv
        assert "/L" in argv
        assert "trap" not in argv[1]  # never a link
        assert argv[1] == str(locked / "node.key")


def test_data_fix_argvs_are_one_entry_no_recursion_no_link_target() -> None:
    owner, grant = wi.data_fix_argvs(PureWindowsPath(r"C:\ProgramData\MeshForge\x"))
    assert owner == [
        "icacls", r"C:\ProgramData\MeshForge\x", "/setowner", "*S-1-5-32-544",
        "/L", "/C", "/Q",
    ]  # fmt: skip
    assert grant[2:4] == ["/grant", "*S-1-5-32-544:F"]
    assert "/T" not in owner + grant


def test_a_root_that_is_itself_a_link_is_only_unlinked(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    (target / "f").write_text("x")
    root = tmp_path / "MeshForge"
    root.symlink_to(target, target_is_directory=True)
    assert wi.remove_tree_no_follow(root, lambda p: None) == 1
    assert (target / "f").read_text() == "x"


# ----------------------------------------- fix round 1 N4/N5: small decisions
def test_console_reader_is_the_session_user_else_the_process_user() -> None:
    excluded = (wsvc.service_sid(), "S-1-5-18", "S-1-5-32-544")
    admin = "S-1-5-21-1-2-3-500"
    assert wi.console_reader_sids(USER_SID, admin, excluded=excluded) == [USER_SID]
    assert wi.console_reader_sids(None, admin, excluded=excluded) == [admin]
    assert wi.console_reader_sids(None, "S-1-5-18", excluded=excluded) == []


@pytest.mark.parametrize(
    ("code", "output", "outcome"),
    [
        (0, "Deleted 1 rule(s).", "deleted"),
        (1, "No rules match the specified criteria.", "absent"),
        # fix round 2: the exit code decides, never netsh's (localised) text
        (
            1,
            "Es gibt keine Regeln, die den angegebenen Kriterien entsprechen.",
            "absent",
        ),
        (1, "Aucune règle ne correspond aux critères spécifiés.", "absent"),
        (1, "", "absent"),
        (5, "", "unexpected"),
        (2, "No rules match the specified criteria.", "unexpected"),
    ],
)
def test_netsh_delete_outcome(code: int, output: str, outcome: str) -> None:
    assert wi.netsh_delete_outcome(code, output) == outcome


@pytest.mark.parametrize(
    ("code", "output", "outcome"),
    [
        (0, "The command completed successfully.", "removed"),
        (2, "System error 1377 has occurred.", "not_member"),
        (2, "System error 1376 has occurred.", "no_group"),
        (2, "System error 5 has occurred. Access is denied.", "unexpected"),
    ],
)
def test_docker_remove_outcome(code: int, output: str, outcome: str) -> None:
    assert wi.docker_remove_outcome(code, output) == outcome


# ------------------------------------- fix round 2: a failed delete never stops the add
class _Firewall:
    def __init__(self, code: int, output: str) -> None:
        self.code, self.output = code, output
        self.calls: list[str] = []
        self.warnings: list[tuple[int, str]] = []

    def delete(self) -> tuple[int, str]:
        self.calls.append("delete")
        return self.code, self.output

    def add(self) -> None:
        self.calls.append("add")

    def warn(self, code: int, output: str) -> None:
        self.warnings.append((code, output))


@pytest.mark.parametrize(
    "output",
    [
        "Es gibt keine Regeln, die den angegebenen Kriterien entsprechen.",
        "Aucune règle ne correspond aux critères spécifiés.",
    ],
)
def test_localised_no_rule_is_absent_and_the_add_runs(output: str) -> None:
    fw = _Firewall(1, output)
    outcome = wi.sync_peer_rule(fw.delete, fw.add, peer_serving=True, warn=fw.warn)
    assert outcome == "absent"
    assert fw.calls == ["delete", "add"]
    assert fw.warnings == []


def test_an_unexpected_delete_is_logged_and_the_add_still_runs() -> None:
    fw = _Firewall(5, "  Zugriff verweigert.  ")
    outcome = wi.sync_peer_rule(fw.delete, fw.add, peer_serving=True, warn=fw.warn)
    assert outcome == "unexpected"
    assert fw.calls == ["delete", "add"]
    assert fw.warnings == [(5, "Zugriff verweigert.")]


@pytest.mark.parametrize("code", [0, 1, 5])
def test_peer_off_only_deletes(code: int) -> None:
    fw = _Firewall(code, "")
    wi.sync_peer_rule(fw.delete, fw.add, peer_serving=False, warn=fw.warn)
    assert fw.calls == ["delete"]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions"
)
def test_a_delete_refused_after_the_fix_names_taking_ownership(tmp_path: Path) -> None:
    root = tmp_path / "MeshForge"
    root.mkdir()
    (root / "stuck").write_text("x")
    root.chmod(0o500)
    try:
        with pytest.raises(OSError, match="after taking ownership"):
            wi.remove_tree_no_follow(root, lambda p: None)  # the fix does not help
    finally:
        root.chmod(0o700)
