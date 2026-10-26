"""V0.17 SP1 (D386): the pure parts of the agent Windows service.

Everything the SCM and icacls/netsh get from us is decided by these helpers, so
they are pinned here on every OS; the pywin32 side is exercised on a real Windows
runner by ``lab/spikes/sp1_windows_service.py``.
"""

from __future__ import annotations

import io
import signal
import subprocess
import sys
from pathlib import PureWindowsPath

import pytest

from meshforge.agent import winservice as ws
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

PY = r"C:\Program Files\MeshForge\0.17.0\python\python.exe"
LAYOUT = ws.machine_layout(r"C:\ProgramData")


# ------------------------------------------------------------------ layout
def test_layout_is_the_d385_machine_state_folders() -> None:
    assert LAYOUT.root == PureWindowsPath(r"C:\ProgramData\MeshForge")
    assert str(LAYOUT.config_file) == r"C:\ProgramData\MeshForge\config\config.toml"
    # identity = data_dir/identity, the agent's own rule (ADR-0013)
    assert LAYOUT.identity_dir == LAYOUT.root / "identity"
    assert LAYOUT.cache_dir.name == "cache"
    assert LAYOUT.logs_dir.name == "logs"


# ------------------------------------------------------------ service spec
def test_spec_matches_d386() -> None:
    spec = ws.build_service_spec(PY, LAYOUT)
    assert spec.name == "MeshForgeAgent"
    assert spec.account == r"NT SERVICE\MeshForgeAgent"
    assert spec.delayed_auto_start is True
    assert spec.sid_type == "unrestricted"
    # Off: exit 4/5 report STOPPED with an error code and must not be restarted.
    assert spec.failure_actions_on_non_crash_failures is False
    assert spec.failure_reset_s == ws.FAILURE_RESET_S


def test_recovery_is_three_restarts_then_an_explicit_none() -> None:
    actions = ws.build_service_spec(PY, LAYOUT).failure_actions
    assert [a.kind for a in actions] == ["restart", "restart", "restart", "none"]
    assert [a.delay_ms for a in actions[:3]] == [5000, 5000, 5000]
    # The SCM repeats the last action for later failures: it must be "none".
    assert actions[-1] == ws.FailureAction("none", 0)


def test_bin_path_quotes_paths_with_spaces_and_round_trips() -> None:
    spec = ws.build_service_spec(PY, LAYOUT, grace_s=12.5)
    assert spec.bin_path.startswith(f'"{PY}" -I -m meshforge.agent.winservice host')
    # list2cmdline is the inverse of the MS C runtime argv parser
    expected = ws.host_argv(PY, LAYOUT, grace_s=12.5)
    assert subprocess.list2cmdline(expected) == spec.bin_path
    assert expected[expected.index("--grace-s") + 1] == "12.5"
    assert expected[expected.index("--config") + 1] == str(LAYOUT.config_file)


def test_host_argv_child_override_comes_after_a_double_dash() -> None:
    argv = ws.host_argv(PY, LAYOUT, child_argv=[PY, r"C:\probe.py", "--x"])
    assert argv[-4:] == ["--", PY, r"C:\probe.py", "--x"]
    assert "--" not in ws.host_argv(PY, LAYOUT)


def test_agent_child_runs_the_normal_run_command_behind_the_watcher() -> None:
    argv = ws.agent_child_argv(PY, r"C:\cfg.toml")
    assert argv[:5] == [PY, "-I", "-m", "meshforge.agent.winservice", "child"]
    assert argv[5:] == ["--", "run", "--config", r"C:\cfg.toml"]
    # V0.17 G4: no config file yet (fresh install) -> the system-mode defaults
    assert ws.agent_child_argv(PY, None)[5:] == ["--", "run"]


# --------------------------------------------------------------- exit codes
@pytest.mark.parametrize("code", [ExitCode.CONFIG_ERROR, ExitCode.CONFLICT])
def test_exit_4_and_5_stop_without_restart_and_keep_the_code(code: int) -> None:
    d = ws.classify_exit(int(code), stop_requested=False)
    assert d.kind is ws.StopKind.STOPPED
    assert d.win32_exit_code == ws.ERROR_SERVICE_SPECIFIC_ERROR == 1066
    assert d.service_exit_code == int(code)


@pytest.mark.parametrize("code", [0, 1, 6, 130, 3221225477, -1, 256])
def test_other_unrequested_exits_restart_with_a_valid_process_code(code: int) -> None:
    d = ws.classify_exit(code, stop_requested=False)
    assert d.kind is ws.StopKind.RESTART
    assert 1 <= d.process_exit_code <= 255
    if 0 < code < 256:
        assert d.process_exit_code == code


@pytest.mark.parametrize("code", [0, 1, 4, 5, -9, 3221225786])
def test_a_requested_stop_is_a_clean_stop_whatever_the_child_returned(
    code: int,
) -> None:
    d = ws.classify_exit(code, stop_requested=True)
    assert d == ws.ExitDecision(ws.StopKind.STOPPED, "stop_requested")


# --------------------------------------------------------------------- SIDs
def test_service_sid_matches_the_windows_algorithm() -> None:
    # Well-known: NT SERVICE\TrustedInstaller on every Windows install.
    assert ws.service_sid("TrustedInstaller") == (
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )
    # Case-insensitive, like service names.
    assert ws.service_sid("meshforgeagent") == ws.service_sid("MeshForgeAgent")
    assert ws.service_sid().startswith("S-1-5-80-")


# --------------------------------------------------------------------- ACLs
def test_acl_plan_grants_only_system_admins_and_the_service() -> None:
    sid = ws.service_sid()
    plan = ws.acl_plan(LAYOUT, sid)
    by_path = {acl.path: acl for acl in plan}
    assert set(by_path) == {
        LAYOUT.root,
        LAYOUT.config_dir,
        LAYOUT.identity_dir,
        LAYOUT.cache_dir,
        LAYOUT.logs_dir,
        LAYOUT.console_dir,  # V0.17 G4: the console token folder
    }
    for acl in plan:
        assert {g.sid for g in acl.grants} == {
            ws.SID_SYSTEM,
            ws.SID_ADMINISTRATORS,
            sid,
        }
    rights = {acl.path: {g.sid: g.access for g in acl.grants}[sid] for acl in plan}
    assert rights[LAYOUT.config_dir] is ws.Access.READ  # the service never edits config
    assert rights[LAYOUT.identity_dir] is ws.Access.FULL
    assert by_path[LAYOUT.identity_dir].owner_sid == sid  # D385 owner rule
    assert all(acl.owner_sid is None for acl in plan if acl.path != LAYOUT.identity_dir)
    # the root is first: its inheritable ACEs must not overwrite the sub-folders
    assert plan[0].path == LAYOUT.root


def test_icacls_argv_removes_inheritance_and_uses_sid_syntax() -> None:
    sid = ws.service_sid()
    acl = ws.DirAcl(
        PureWindowsPath(r"C:\ProgramData\MeshForge\identity"),
        (ws.Grant(ws.SID_SYSTEM, ws.Access.FULL), ws.Grant(sid, ws.Access.FULL)),
        owner_sid=sid,
    )
    grant, owner = ws.icacls_argv(acl)
    assert grant == [
        "icacls",
        r"C:\ProgramData\MeshForge\identity",
        "/inheritance:r",
        "/grant:r",
        "*S-1-5-18:(OI)(CI)F",
        "/grant:r",
        f"*{sid}:(OI)(CI)F",
    ]
    assert owner == [
        "icacls",
        r"C:\ProgramData\MeshForge\identity",
        "/setowner",
        f"*{sid}",
    ]
    no_owner = ws.DirAcl(acl.path, acl.grants)
    assert len(ws.icacls_argv(no_owner)) == 1


# ----------------------------------------------------------------- firewall
def test_peer_firewall_rule_is_named_private_domain_only() -> None:
    argv = ws.firewall_add_argv(ws.peer_firewall_rule())
    assert "name=MeshForge-Agent-Peer" in argv
    assert "localport=47120" in argv
    assert "protocol=TCP" in argv
    assert "profile=private,domain" in argv
    assert not any("public" in a.lower() for a in argv)
    assert ws.firewall_delete_argv("MeshForge-Agent-Peer") == [
        "netsh", "advfirewall", "firewall", "delete", "rule", "name=MeshForge-Agent-Peer",
    ]  # fmt: skip


# ------------------------------------------------------------- stop watcher
class _DoneError(Exception):
    pass


def test_watcher_signals_only_after_eof_and_repeats() -> None:
    sent: list[int] = []

    def interrupt(sig: signal.Signals) -> None:
        sent.append(int(sig))
        if len(sent) == 3:
            raise _DoneError

    stream = io.BytesIO(b"noise that is not a stop\n" * 3)
    with pytest.raises(_DoneError):
        ws.watch_for_stop(
            stream, signum=signal.SIGTERM, interval_s=0.0, interrupt=interrupt
        )
    assert sent == [int(signal.SIGTERM)] * 3
    assert stream.read() == b""  # everything was consumed before the first signal


def test_watcher_treats_a_broken_pipe_as_a_stop() -> None:
    class Broken(io.RawIOBase):
        def read(self, size: int = -1) -> bytes:
            raise OSError("pipe broken")

    sent: list[int] = []

    def interrupt(sig: signal.Signals) -> None:
        sent.append(int(sig))
        raise _DoneError

    with pytest.raises(_DoneError):
        ws.watch_for_stop(
            Broken(),  # type: ignore[arg-type]
            signum=signal.SIGTERM,
            interval_s=0.0,
            interrupt=interrupt,
        )
    assert sent == [int(signal.SIGTERM)]


def test_stop_signal_is_sigbreak_exactly_on_windows() -> None:
    if sys.platform == "win32":
        assert ws.stop_signal() == signal.SIGBREAK
    else:
        assert ws.stop_signal() == signal.SIGTERM


# ------------------------------------------------------------- command line
@pytest.mark.skipif(
    sys.platform == "win32", reason="the Windows commands are real there"
)
@pytest.mark.parametrize("command", [["install"], ["uninstall"]])
def test_windows_commands_refuse_elsewhere(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert ws.main(command) == int(ExitCode.USAGE_ERROR)
    assert "only on Windows" in capsys.readouterr().err


def test_the_pywin32_module_is_not_imported_by_the_pure_module() -> None:
    assert (
        "meshforge.agent.winservice_host" not in sys.modules or sys.platform == "win32"
    )
