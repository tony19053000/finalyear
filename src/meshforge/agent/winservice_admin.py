# ruff: noqa: E402, T201 - platform assert first; this is a CLI (its output is the point)
"""The installer's and an administrator's Windows commands (V0.17 G4). Windows only.

``python.exe -I -m meshforge.agent.winservice <command>`` (parsed in
:mod:`meshforge.agent.winservice`):

* ``setup`` / ``teardown`` - the Inno Setup installer's two steps
  (:mod:`meshforge.agent.winsetup`); the result goes to stdout, to
  ``--result-file`` (JSON, for tests) and ``--message-file`` (UTF-8 text with a
  BOM, the installer's last page);
* ``update-path`` - point the service at another version's ``python.exe``;
* ``health`` - is the running service healthy (the upgrade check, alone);
* ``console-access add|remove`` - who may open the local console (Q30);
* ``docker-access`` - add ``NT SERVICE\\MeshForgeAgent`` to ``docker-users`` and
  restart the service (Q31; the Start-menu entry runs it through UAC);
* ``firewall-sync`` - the peer rule follows ``[agent.peer] bind`` (D389).

All need an elevated (administrator) process and say so plainly otherwise.
"""

from __future__ import annotations

import sys

assert sys.platform == "win32"  # noqa: S101 - tells mypy this module is Windows-only

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from meshforge.agent.wininstall import ProgramLayout, docker_users_argv, program_layout
from meshforge.agent.winservice import MachineLayout, machine_layout
from meshforge.agent.winservice_ops import (
    RealOps,
    account_sid,
    is_admin,
    run_tool,
)
from meshforge.agent.winsetup import (
    DEFAULT_HEALTH_TIMEOUT_S,
    OpsError,
    setup,
    teardown,
    wait_healthy,
)
from meshforge.agent.winupgrade import FileRecords
from meshforge.common.exit_codes import ExitCode
from meshforge.common.logging import configure_logging
from meshforge.common.windows_service import (
    SERVICE_ACCOUNT,
    SERVICE_NAME,
    SID_ADMINISTRATORS,
    SID_SYSTEM,
    service_sid,
)

__all__ = ["run_admin_command"]


def _machine() -> MachineLayout:
    return machine_layout(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))


def _ops() -> RealOps:
    return RealOps(_machine(), _program())


def _program() -> ProgramLayout:
    try:
        return program_layout(sys.executable)
    except ValueError:
        # Not run from an installed bundle (a developer checkout): the shims and
        # current.txt would have no home - use a layout that names that plainly.
        return ProgramLayout(PureWindowsPath(sys.executable).parent, "development")


def _write_outputs(
    payload: dict[str, object],
    headline: str,
    lines: Sequence[str],
    result_file: str | None,
    message_file: str | None,
) -> None:
    text = "\n".join([headline, *lines]) + "\n"
    print(text, end="")
    if result_file:
        Path(result_file).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
    if message_file:
        # Inno Setup's LoadStringsFromFile reads UTF-8 when the file has a BOM.
        Path(message_file).write_text(text, encoding="utf-8-sig")


def _need_admin(command: str) -> int | None:
    if is_admin():
        return None
    print(
        f"{command}: needs an elevated prompt (right-click PowerShell or Terminal -> "
        "Run as administrator)",
        file=sys.stderr,
    )
    return int(ExitCode.FAILURE)


def cmd_setup(
    *, result_file: str | None, message_file: str | None, health_timeout_s: float
) -> int:
    if (refused := _need_admin("setup")) is not None:
        return refused
    try:
        layout = program_layout(sys.executable)
    except ValueError as exc:
        print(f"setup: run it with the installed bundle's python.exe ({exc})")
        return int(ExitCode.USAGE_ERROR)
    result = setup(
        RealOps(_machine(), layout),
        layout,
        health_timeout_s=health_timeout_s,
        records=FileRecords(Path(str(layout.root))),  # V0.17 G5 (Q38/Q39)
    )
    _write_outputs(
        result.as_dict(), result.summary(), result.lines, result_file, message_file
    )
    return int(result.exit_code)


def cmd_teardown(
    *, remove_data: bool, result_file: str | None, message_file: str | None
) -> int:
    if (refused := _need_admin("teardown")) is not None:
        return refused
    result = teardown(_ops(), remove_data=remove_data)
    headline = (
        "MeshForge was removed." if result.ok else "MeshForge was NOT fully removed."
    )
    _write_outputs(result.as_dict(), headline, result.lines, result_file, message_file)
    return int(result.exit_code)


def cmd_update_path(python_exe: str) -> int:
    if (refused := _need_admin("update-path")) is not None:
        return refused
    if not Path(python_exe).is_file():
        print(f"update-path: no interpreter at {python_exe}", file=sys.stderr)
        return int(ExitCode.USAGE_ERROR)
    try:
        _ops().install_service(python_exe)
    except OpsError as exc:
        print(f"update-path failed: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    print(f"{SERVICE_NAME} now runs {python_exe} (restart it to use it)")
    return int(ExitCode.OK)


def cmd_health(timeout_s: float) -> int:
    ops = _ops()
    try:
        health = wait_healthy(
            ops,
            log_offset=0,
            timeout_s=timeout_s,
            need_console=ops.console_enabled(),
            need_started=ops.enrolled(),
        )
    except OpsError as exc:
        print(f"health: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    print(
        f"{SERVICE_NAME}: {health.status.state} (pid {health.status.pid}) - "
        f"{health.reason}"
    )
    return int(ExitCode.OK if health.ok else ExitCode.FAILURE)


_PROTECTED = frozenset({SID_SYSTEM, SID_ADMINISTRATORS})
_SID = re.compile(r"S-1(-[0-9]+)+", re.ASCII)


def cmd_console_access(
    action: str, users: Sequence[str], sids: Sequence[str], session_user: bool
) -> int:
    if (refused := _need_admin("console-access")) is not None:
        return refused
    ops = _ops()
    try:
        wanted = [account_sid(name) for name in users] + list(sids)
        if session_user:
            wanted += ops.console_readers()
    except OpsError as exc:
        print(f"console-access: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE_ERROR)
    wanted = list(dict.fromkeys(wanted))
    # a whole, strict SID: anything else (e.g. "S-1-1-0:(OI)(CI)F") would reach
    # icacls as the caller's own permission text
    malformed = [s for s in wanted if not _SID.fullmatch(s)]
    if malformed:
        print(
            f"console-access: not a SID (S-1-N-N...): {', '.join(map(repr, malformed))}",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE_ERROR)
    blocked = [s for s in wanted if s in _PROTECTED or s == service_sid()]
    if blocked or not wanted:
        print(
            "console-access: name at least one user (--user NAME, --sid S-1-..., "
            "--session-user); SYSTEM, Administrators and the service are not "
            "changed here",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE_ERROR)
    try:
        if action == "add":
            ops.grant_console(wanted)
        else:
            ops.revoke_console(wanted)
    except OpsError as exc:
        print(f"console-access failed: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    verb = "may now open" if action == "add" else "may no longer open"
    print(f"{', '.join(wanted)} {verb} the MeshForge console")
    if action == "add":
        print(
            "(sign out and back in is NOT needed; open MeshForge from the Start menu)"
        )
    return int(ExitCode.OK)


def cmd_docker_access() -> int:
    if (refused := _need_admin("docker-access")) is not None:
        return refused
    try:
        done = run_tool(docker_users_argv(add=True), ok_codes=frozenset(range(256)))
        text = (done.stdout + done.stderr).strip()
        # 1378 = "already a member": also fine.
        if done.returncode != 0 and "1378" not in text:
            print(
                f"could not add {SERVICE_ACCOUNT} to docker-users (exit "
                f"{done.returncode}): {text[-300:]} - is Docker Desktop installed?",
                file=sys.stderr,
            )
            return int(ExitCode.FAILURE)
        ops = _ops()
        ops.stop_service(60.0)
        ops.start_service()  # a service gets its groups at start
    except OpsError as exc:
        print(f"docker-access failed: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    print(
        f"{SERVICE_ACCOUNT} is in docker-users and {SERVICE_NAME} restarted. If "
        "Docker still says no access, restart Docker Desktop (or sign out and in)."
    )
    return int(ExitCode.OK)


def cmd_firewall_sync() -> int:
    if (refused := _need_admin("firewall-sync")) is not None:
        return refused
    ops = _ops()
    peer = ops.peer_serving()
    if peer is None:
        print(
            "firewall-sync: the agent configuration cannot be read; fix "
            r"C:\ProgramData\MeshForge\config\config.toml first",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG_ERROR)
    try:
        ops.sync_firewall(peer)
    except OpsError as exc:
        print(f"firewall-sync failed: {exc}", file=sys.stderr)
        return int(ExitCode.FAILURE)
    print(
        "peer firewall rule MeshForge-Agent-Peer: "
        + ("present (Private + Domain networks only)" if peer else "removed (peer off)")
    )
    return int(ExitCode.OK)


def run_admin_command(args: object) -> int:
    """Dispatch a parsed ``winservice`` admin command (``argparse.Namespace``)."""
    configure_logging("WARNING", fmt="console", component="agent-service")
    command = str(getattr(args, "command", ""))
    if command == "setup":
        return cmd_setup(
            result_file=getattr(args, "result_file", None),
            message_file=getattr(args, "message_file", None),
            health_timeout_s=float(
                getattr(args, "health_timeout", DEFAULT_HEALTH_TIMEOUT_S)
            ),
        )
    if command == "teardown":
        return cmd_teardown(
            remove_data=bool(getattr(args, "remove_data", False)),
            result_file=getattr(args, "result_file", None),
            message_file=getattr(args, "message_file", None),
        )
    if command == "update-path":
        return cmd_update_path(str(getattr(args, "python_exe", sys.executable)))
    if command == "health":
        return cmd_health(float(getattr(args, "timeout", DEFAULT_HEALTH_TIMEOUT_S)))
    if command == "console-access":
        return cmd_console_access(
            str(getattr(args, "action", "add")),
            list(getattr(args, "user", None) or []),
            list(getattr(args, "sid", None) or []),
            bool(getattr(args, "session_user", False)),
        )
    if command == "docker-access":
        return cmd_docker_access()
    if command == "firewall-sync":
        return cmd_firewall_sync()
    print(f"unknown command {command!r}", file=sys.stderr)
    return int(ExitCode.USAGE_ERROR)
