# ruff: noqa: E402 - the platform assert must precede the pywin32 imports
"""The real Windows side of the installer helper (V0.17 G4). Windows only.

:class:`RealOps` implements :class:`meshforge.agent.winsetup.Ops` with the SCM
(pywin32), ``icacls`` / ``netsh`` / ``schtasks`` / ``net`` (fixed argument lists
from :mod:`meshforge.agent.wininstall` and :mod:`meshforge.agent.winservice`),
the registry (``winreg``) and the agent's own modules (migration, settings).
Nothing is decided here; each method is one step of
:func:`meshforge.agent.winsetup.setup` / ``teardown``. Exercised end to end by the
``package-windows`` CI job (``.github/workflows/ci.yml``).
"""

from __future__ import annotations

import sys

assert sys.platform == "win32"  # noqa: S101 - tells mypy this module is Windows-only

import contextlib
import csv
import ctypes
import json
import os
import shutil
import stat
import subprocess
import time
import winreg
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

import pywintypes
import win32api
import win32con
import win32gui
import win32security
import win32service
import win32serviceutil
import win32ts

from meshforge.agent import winservice_host as host
from meshforge.agent.wininstall import (
    SHIM_COMMANDS,
    ProgramLayout,
    console_grant_argv,
    console_revoke_argv,
    docker_users_argv,
    path_add,
    path_remove,
    schtasks_argv,
    service_python,
    shim_text,
)
from meshforge.agent.winservice import (
    AGENT_LOG_NAME,
    PEER_FIREWALL_RULE,
    MachineLayout,
    build_service_spec,
    firewall_add_argv,
    firewall_delete_argv,
    peer_firewall_rule,
)
from meshforge.agent.winsetup import OpsError, ServiceStatus
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.windows_service import (
    INSTALL_MODE_ENV_VAR,
    SERVICE_NAME,
    SID_ADMINISTRATORS,
    SID_SYSTEM,
    service_sid,
)

__all__ = ["RealOps", "current_user_sid", "is_admin", "run_tool", "session_user_sid"]

_TOOL_TIMEOUT_S: Final = 60.0
_STATES: Final = {
    win32service.SERVICE_STOPPED: "stopped",
    win32service.SERVICE_START_PENDING: "start_pending",
    win32service.SERVICE_STOP_PENDING: "stop_pending",
    win32service.SERVICE_RUNNING: "running",
    win32service.SERVICE_CONTINUE_PENDING: "continue_pending",
    win32service.SERVICE_PAUSE_PENDING: "pause_pending",
    win32service.SERVICE_PAUSED: "paused",
}
_ERROR_SERVICE_DOES_NOT_EXIST: Final = 1060
_ERROR_SERVICE_ALREADY_RUNNING: Final = 1056
_ENV_KEY: Final = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_HEALTH_URL: Final = "http://127.0.0.1:{port}/console/v1/health"


def is_admin() -> bool:
    """This process runs elevated (an administrator's full token)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run_tool(
    argv: Sequence[str], *, ok_codes: frozenset[int] = frozenset({0})
) -> subprocess.CompletedProcess[str]:
    """A Windows tool with a fixed argument list; :class:`OpsError` (naming the
    tool and its output's tail) unless its exit code is in ``ok_codes``."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv from the pure helpers
            list(argv),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TOOL_TIMEOUT_S,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpsError(f"{argv[0]} could not run: {exc}") from None
    if done.returncode not in ok_codes:
        out = (done.stdout + done.stderr).strip().replace("\r", "")
        raise OpsError(
            f"{' '.join(argv[:2])} failed (exit {done.returncode}): {out[-300:]}"
        )
    return done


def _sid_string(sid: Any) -> str:  # a PySID
    return str(win32security.ConvertSidToStringSid(sid))


def current_user_sid() -> str:
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    try:
        user, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        win32api.CloseHandle(token)
    return _sid_string(user)


def session_user_sid() -> str | None:
    """The user signed in to *this process's* Windows session - the person at the
    keyboard who started the installer, also when an administrator's password
    elevated it (over-the-shoulder UAC runs as the admin, in the user's
    session). ``None`` in session 0 or when it cannot be resolved."""
    try:
        # Typed alias: the stubs leave this one untyped.
        to_session: Callable[[int], int] = win32ts.ProcessIdToSessionId
        session = to_session(os.getpid())
        if session == 0:
            return None
        server = win32ts.WTS_CURRENT_SERVER_HANDLE
        user = win32ts.WTSQuerySessionInformation(server, session, win32ts.WTSUserName)
        domain = win32ts.WTSQuerySessionInformation(
            server, session, win32ts.WTSDomainName
        )
        if not user:
            return None
        sid, _domain, _kind = win32security.LookupAccountName(
            None, f"{domain}\\{user}" if domain else user
        )
    except pywintypes.error:
        return None
    return _sid_string(sid)


def account_sid(name: str) -> str:
    """``DOMAIN\\user`` or ``user`` -> its SID string (:class:`OpsError` if unknown)."""
    try:
        sid, _domain, _kind = win32security.LookupAccountName(None, name)
    except pywintypes.error as exc:
        raise OpsError(f"no Windows account {name!r}: {exc.strerror}") from None
    return _sid_string(sid)


class RealOps:
    """:class:`meshforge.agent.winsetup.Ops` on this machine."""

    def __init__(self, machine: MachineLayout, program: ProgramLayout) -> None:
        self.machine = machine
        self.program = program
        # The settings this process loads (firewall, console) are the service's.
        os.environ[INSTALL_MODE_ENV_VAR] = "system"

    # ---------------------------------------------------------------- SCM
    def service_status(self) -> ServiceStatus:
        try:
            scm = win32service.OpenSCManager(
                None, None, win32service.SC_MANAGER_CONNECT
            )
        except pywintypes.error as exc:
            raise OpsError(f"cannot open the service manager: {exc.strerror}") from None
        try:
            try:
                handle = win32service.OpenService(
                    scm,
                    SERVICE_NAME,
                    win32service.SERVICE_QUERY_STATUS
                    | win32service.SERVICE_QUERY_CONFIG,
                )
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_SERVICE_DOES_NOT_EXIST:
                    return ServiceStatus(exists=False)
                raise OpsError(f"cannot open {SERVICE_NAME}: {exc.strerror}") from None
            try:
                # A dict at runtime (SP1); the stubs type it as a tuple.
                status: Any = win32service.QueryServiceStatusEx(handle)
                config = win32service.QueryServiceConfig(handle)
            finally:
                win32service.CloseServiceHandle(handle)
        finally:
            win32service.CloseServiceHandle(scm)
        pid = int(status.get("ProcessId") or 0) or None
        return ServiceStatus(
            exists=True,
            state=_STATES.get(status["CurrentState"], str(status["CurrentState"])),
            pid=pid,
            win32_exit_code=int(status["Win32ExitCode"]),
            service_exit_code=int(status["ServiceSpecificExitCode"]),
            python=service_python(str(config[3])),
        )

    def stop_service(self, timeout_s: float) -> bool:
        with contextlib.suppress(pywintypes.error):
            win32serviceutil.StopService(SERVICE_NAME)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.service_status()
            if not status.exists or status.state == "stopped":
                return True
            time.sleep(0.5)
        return False

    def start_service(self) -> None:
        try:
            win32serviceutil.StartService(SERVICE_NAME)
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_SERVICE_ALREADY_RUNNING:
                raise OpsError(f"{exc.strerror} ({exc.winerror})") from None

    def install_service(self, python_exe: str) -> None:
        spec = build_service_spec(python_exe, self.machine)
        try:
            host.install(spec, self.machine, peer_firewall=False)
        except host.ServiceInstallError as exc:
            raise OpsError(str(exc)) from None

    def uninstall_service(self) -> bool:
        try:
            return host.uninstall()
        except host.ServiceInstallError as exc:
            raise OpsError(str(exc)) from None

    # --------------------------------------------------- legacy dev task
    def legacy_task_state(self) -> str | None:
        done = run_tool(schtasks_argv("query"), ok_codes=frozenset(range(256)))
        if done.returncode != 0:
            return None  # "ERROR: The system cannot find the file specified."
        rows = list(csv.reader(done.stdout.splitlines()))
        # /V /FO CSV /NH: HostName, TaskName, Next Run Time, Status, ...
        for row in rows:
            if len(row) > 3 and row[1].strip().lstrip("\\").lower() == "meshforgeagent":
                return row[3].strip() or "registered"
        return "registered"

    def end_and_disable_legacy_task(self) -> None:
        # /End fails when it is not running: that is fine.
        run_tool(schtasks_argv("end"), ok_codes=frozenset(range(256)))
        run_tool(schtasks_argv("disable"))

    def delete_legacy_task(self) -> None:
        run_tool(schtasks_argv("delete"))

    # ----------------------------------------------------------- migration
    def migrate(self) -> tuple[str, list[str]]:
        from meshforge.agent.migration import migrate
        from meshforge.common.errors import MeshForgeError

        try:
            report = migrate(
                explicit=None,
                config_from=None,
                dry_run=False,
                service_user=None,
                sudo_user=os.environ.get("USERNAME"),
            )
        except MeshForgeError as exc:
            return "blocked", [f"identity migration failed: {exc.message}"]
        except OSError as exc:
            return "blocked", [
                f"identity migration failed on {exc.filename or 'a file'}: "
                f"{exc.strerror or exc}; nothing was committed - run the installer again"
            ]
        return report.plan.outcome.value, report.lines()

    # ------------------------------------------------- firewall + console
    def _settings(self) -> object | None:
        from meshforge.agent.settings import AgentSettings
        from meshforge.common.errors import MeshForgeError

        try:
            return AgentSettings.load()
        except (MeshForgeError, OSError):
            return None

    def peer_serving(self) -> bool | None:
        from meshforge.agent.settings import AgentSettings

        settings = self._settings()
        if not isinstance(settings, AgentSettings):
            return None
        return settings.agent.peer.bind is not None

    def sync_firewall(self, peer_serving: bool) -> None:
        rule = peer_firewall_rule(self._peer_port())
        run_tool(firewall_delete_argv(PEER_FIREWALL_RULE), ok_codes=frozenset({0, 1}))
        if peer_serving:
            run_tool(firewall_add_argv(rule))

    def _peer_port(self) -> int:
        from meshforge.agent.settings import DEFAULT_PEER_PORT, AgentSettings

        settings = self._settings()
        if isinstance(settings, AgentSettings):
            return settings.agent.peer.port
        return DEFAULT_PEER_PORT

    def console_readers(self) -> list[str]:
        readers = [current_user_sid()]
        session = session_user_sid()
        if session is not None:
            readers.insert(0, session)
        system = {service_sid(), SID_SYSTEM, SID_ADMINISTRATORS}
        return [sid for sid in dict.fromkeys(readers) if sid not in system]

    def grant_console(self, sids: Sequence[str]) -> None:
        Path(self.machine.console_dir).mkdir(parents=True, exist_ok=True)
        run_tool(console_grant_argv(self.machine.console_dir, sids))

    def revoke_console(self, sids: Sequence[str]) -> None:
        run_tool(console_revoke_argv(self.machine.console_dir, sids))

    def console_enabled(self) -> bool:
        from meshforge.agent.settings import AgentSettings

        settings = self._settings()
        if not isinstance(settings, AgentSettings):
            return True  # the agent itself refuses a bad config: health fails anyway
        return settings.agent.console.enabled is not False

    def console_healthy(self) -> bool:
        import httpx

        from meshforge.agent.settings import DEFAULT_CONSOLE_PORT, AgentSettings

        settings = self._settings()
        port = (
            settings.agent.console.port
            if isinstance(settings, AgentSettings)
            else DEFAULT_CONSOLE_PORT
        )
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                return client.get(_HEALTH_URL.format(port=port)).status_code == 200
        except httpx.HTTPError:
            return False

    # ---------------------------------------------------------------- log
    def _agent_log(self) -> Path:
        return Path(self.machine.logs_dir) / AGENT_LOG_NAME

    def agent_log_offset(self) -> int:
        try:
            return self._agent_log().stat().st_size
        except OSError:
            return 0

    def agent_started_since(self, offset: int) -> bool:
        try:
            with self._agent_log().open("rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(offset if offset <= size else 0)
                return b'"agent.started"' in fh.read()
        except OSError:
            return False

    def enrolled(self) -> bool:
        from meshforge.agent.identity import NODE_CERT_FILE_NAME

        return (Path(self.machine.identity_dir) / NODE_CERT_FILE_NAME).is_file()

    def node_id(self) -> str | None:
        from meshforge.agent.identity import JSON_FILE_NAME

        try:
            data = json.loads(
                (Path(self.machine.identity_dir) / JSON_FILE_NAME).read_bytes()
            )
        except (OSError, ValueError):
            return None
        value = data.get("node_id") if isinstance(data, dict) else None
        return value if isinstance(value, str) else None

    # ------------------------------------------------- program dir + PATH
    def write_current(self, version_dir: str) -> None:
        atomic_write_bytes(
            Path(self.program.current_file), (version_dir + "\r\n").encode("ascii")
        )

    def write_shims(self, version_dir: str) -> None:
        target = ProgramLayout(self.program.root, version_dir)
        folder = Path(self.program.shim_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for command in SHIM_COMMANDS:
            atomic_write_bytes(
                folder / f"{command}.cmd", shim_text(target, command).encode("utf-8")
            )

    def _edit_machine_path(self, add: bool) -> None:
        entry = str(self.program.shim_dir)
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                _ENV_KEY,
                0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            ) as key:
                try:
                    value, kind = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    value, kind = "", winreg.REG_EXPAND_SZ
                new = path_add(value, entry) if add else path_remove(value, entry)
                if new != value:
                    winreg.SetValueEx(key, "Path", 0, kind, new)
        except OSError as exc:
            raise OpsError(f"cannot edit the machine PATH: {exc}") from None
        with contextlib.suppress(pywintypes.error):
            win32gui.SendMessageTimeout(
                win32con.HWND_BROADCAST,
                win32con.WM_SETTINGCHANGE,
                0,
                "Environment",  # type: ignore[arg-type]  # pywin32 takes a str
                win32con.SMTO_ABORTIFHUNG,
                5000,
            )

    def ensure_on_path(self) -> None:
        self._edit_machine_path(add=True)

    def remove_from_path(self) -> None:
        self._edit_machine_path(add=False)

    # --------------------------------------------------- docker, data
    def remove_docker_access(self) -> None:
        # Non-zero = not a member, or no docker-users group (no Docker Desktop).
        run_tool(docker_users_argv(add=False), ok_codes=frozenset(range(256)))

    def remove_data(self) -> list[str]:
        root = Path(self.machine.root)
        if not root.exists():
            return [f"no data folder {root}"]
        try:
            shutil.rmtree(root)
        except OSError:
            # A file the service made private to itself (or read-only job files):
            # Administrators take ownership and full control, then delete. icacls,
            # not takeown: takeown's "/D Y" answer is localised.
            # Exit codes ignored: the second rmtree below is the real check.
            admins, anything = f"*{SID_ADMINISTRATORS}", frozenset(range(256))
            run_tool(
                ["icacls", str(root), "/setowner", admins, "/T", "/C", "/Q"],
                ok_codes=anything,
            )
            run_tool(
                ["icacls", str(root), "/grant", f"{admins}:F", "/T", "/C", "/Q"],
                ok_codes=anything,
            )
            try:
                shutil.rmtree(root, onexc=_force_remove)
            except OSError as exc:
                raise OpsError(
                    f"{exc.filename or root}: {exc.strerror or exc}"
                ) from None
        return [f"removed all MeshForge data: {root} (identity, cache, logs)"]

    # ------------------------------------------------------------ time
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _force_remove(func: object, path: str, exc: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)  # Windows: only clears the read-only attribute
    if os.path.isdir(path) and not os.path.islink(path):
        os.rmdir(path)
    else:
        os.unlink(path)
