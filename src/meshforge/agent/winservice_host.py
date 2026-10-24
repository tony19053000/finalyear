# ruff: noqa: E402 - the platform assert must precede the pywin32 imports
"""pywin32 side of the agent Windows service (V0.17 D386, spike SP1). Windows only.

Imported lazily by :mod:`meshforge.agent.winservice` (``host`` / ``install`` /
``uninstall``); never imported on Linux, so pywin32 stays a Windows-only
dependency. Everything decided here comes from the pure helpers in
:mod:`meshforge.agent.winservice` (spec, exit-code mapping, ACL plan, firewall
argv); this module only applies them.

Hosting: the service's binary is the bundled ``python.exe`` (``-I -m
meshforge.agent.winservice host ...``), which calls
``servicemanager.PrepareToHostSingle`` + ``StartServiceCtrlDispatcher`` - the
documented way to host a pywin32 service in an interpreter other than
``pythonservice.exe`` (no registry ``PythonClass``, no DLL-path setup).
"""

from __future__ import annotations

import sys

assert sys.platform == "win32"  # noqa: S101 - tells mypy this module is Windows-only

import contextlib
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, TextIO

import pywintypes
import servicemanager
import win32service
import win32serviceutil

from meshforge.agent.winservice import (
    AGENT_LOG_NAME,
    SERVICE_LOG_NAME,
    SERVICE_NAME,
    SHUTDOWN_GRACE_S,
    ExitDecision,
    MachineLayout,
    ServiceSpec,
    StopKind,
    acl_plan,
    agent_child_argv,
    build_service_spec,
    firewall_add_argv,
    firewall_delete_argv,
    icacls_argv,
    machine_layout,
    peer_firewall_rule,
    service_sid,
)
from meshforge.agent.winservice_supervisor import Supervisor
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.exit_codes import ExitCode
from meshforge.common.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from _win32typing import PySC_HANDLE

__all__ = [
    "MeshForgeAgentService",
    "ServiceInstallError",
    "install",
    "install_cli",
    "run_host",
    "uninstall",
    "uninstall_cli",
]

log = get_logger(__name__)

_ERROR_SERVICE_EXISTS: Final = 1073
_ERROR_SERVICE_DOES_NOT_EXIST: Final = 1060
_ERROR_SERVICE_NOT_ACTIVE: Final = 1062
_ERROR_FAILED_SERVICE_CONTROLLER_CONNECT: Final = 1063
_ERROR_SERVICE_MARKED_FOR_DELETE: Final = 1072
_TOOL_TIMEOUT_S: Final = 60.0
_STOP_WAIT_EXTRA_S: Final = 15.0


class ServiceInstallError(MeshForgeError):
    """Install/uninstall could not finish; the message names the step."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.FAILURE


@dataclass(frozen=True)
class HostOptions:
    argv: tuple[str, ...]
    log_path: Path
    cwd: Path
    grace_s: float


class MeshForgeAgentService(win32serviceutil.ServiceFramework):
    """``MeshForgeAgent``: supervises the agent child, maps its exit to the SCM."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "MeshForge Agent"
    options: ClassVar[HostOptions | None] = None

    def __init__(self, args: Sequence[str]) -> None:
        super().__init__(args)
        opts = self.options
        if opts is None:  # pragma: no cover - run_host always sets it
            raise RuntimeError("MeshForgeAgentService.options not set")
        self._grace_s = opts.grace_s
        self._supervisor = Supervisor(
            opts.argv,
            log_path=opts.log_path,
            grace_s=opts.grace_s,
            cwd=opts.cwd,
            on_progress=self._stop_progress,
        )

    def _stop_progress(self) -> None:
        with contextlib.suppress(pywintypes.error):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=5000)

    def SvcStop(self) -> None:  # noqa: N802 - pywin32 callback name
        self.ReportServiceStatus(
            win32service.SERVICE_STOP_PENDING,
            waitHint=int((self._grace_s + _STOP_WAIT_EXTRA_S) * 1000),
        )
        self._supervisor.request_stop()

    def SvcShutdown(self) -> None:  # noqa: N802 - pywin32 callback name
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=10_000)
        self._supervisor.request_stop(SHUTDOWN_GRACE_S)

    def SvcRun(self) -> None:  # noqa: N802 - pywin32 callback name
        """Own status reporting (not ``SvcDoRun``): STOPPED carries the agent's
        service-specific exit code, and a restartable exit must *not* report
        STOPPED at all (module docstring of :mod:`meshforge.agent.winservice`)."""
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        log.info("agent.service.running", service=SERVICE_NAME, host_pid=os.getpid())
        try:
            decision = self._supervisor.run()
        except Exception as exc:  # last line of defence: a host bug -> crash -> restart
            log.exception("agent.service.host_error", cause=type(exc).__name__)
            decision = ExitDecision(StopKind.RESTART, "host_error", process_exit_code=1)
        if decision.kind is StopKind.RESTART:
            log.warning(
                "agent.service.exit_for_restart",
                reason=decision.reason,
                exit_code=decision.process_exit_code,
            )
            _flush_and_exit(decision.process_exit_code)
        log.info(
            "agent.service.stopped",
            reason=decision.reason,
            win32_exit_code=decision.win32_exit_code,
            service_exit_code=decision.service_exit_code,
        )
        self.ReportServiceStatus(
            win32service.SERVICE_STOPPED,
            win32ExitCode=decision.win32_exit_code,
            svcExitCode=decision.service_exit_code,
        )
        # Exit now: the C framework would otherwise report STOPPED again with 0.
        _flush_and_exit(0)


def _flush_and_exit(code: int) -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            stream.flush()
    os._exit(code)


def _open_service_log(log_dir: Path) -> TextIO:
    log_dir.mkdir(parents=True, exist_ok=True)
    return (log_dir / SERVICE_LOG_NAME).open("a", encoding="utf-8", buffering=1)


def run_host(
    *, config: str, log_dir: str, grace_s: float, child_argv: Sequence[str]
) -> int:
    """Entry of the service process (``winservice host``); returns only on error."""
    logs = Path(log_dir)
    stream = _open_service_log(logs)
    # A service has no console: anything printed must land in the log, not vanish.
    sys.stdout = sys.stderr = stream
    configure_logging("INFO", fmt="json", component="agent-service", stream=stream)
    argv = tuple(child_argv) or tuple(agent_child_argv(sys.executable, config))
    MeshForgeAgentService.options = HostOptions(
        argv=argv,
        log_path=logs / AGENT_LOG_NAME,
        cwd=logs.parent,
        grace_s=grace_s,
    )
    log.info(
        "agent.service.host_starting",
        host_pid=os.getpid(),
        python=sys.executable,
        child_is_agent=not child_argv,
        grace_s=grace_s,
    )
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(MeshForgeAgentService)
    # Typed alias: the stubs leave this one untyped.
    dispatch: Callable[[], None] = servicemanager.StartServiceCtrlDispatcher
    try:
        dispatch()
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            log.error("agent.service.not_started_by_scm")  # noqa: TRY400
            print(  # noqa: T201 - stdout is the log here
                "winservice host must be started by the Service Control Manager "
                f"(sc start {SERVICE_NAME})"
            )
            return int(ExitCode.USAGE_ERROR)
        raise
    return int(ExitCode.OK)


# ------------------------------------------------------------- install
def _run_tool(argv: list[str], *, ok_codes: frozenset[int] = frozenset({0})) -> int:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv from the pure helpers
            argv,
            capture_output=True,
            timeout=_TOOL_TIMEOUT_S,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceInstallError(f"{argv[0]} could not run: {exc}") from exc
    if result.returncode not in ok_codes:
        out = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
        raise ServiceInstallError(
            f"{' '.join(argv[:2])} failed (exit {result.returncode}): {out[:400]}"
        )
    return result.returncode


def _configure(handle: PySC_HANDLE, spec: ServiceSpec) -> None:
    win32service.ChangeServiceConfig2(
        handle, win32service.SERVICE_CONFIG_DESCRIPTION, spec.description
    )
    win32service.ChangeServiceConfig2(
        handle,
        win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
        spec.delayed_auto_start,
    )
    actions = [
        (
            win32service.SC_ACTION_RESTART
            if a.kind == "restart"
            else win32service.SC_ACTION_NONE,
            a.delay_ms,
        )
        for a in spec.failure_actions
    ]
    win32service.ChangeServiceConfig2(
        handle,
        win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
        {
            "ResetPeriod": spec.failure_reset_s,
            "RebootMsg": None,
            "Command": None,
            "Actions": actions,
        },
    )
    win32service.ChangeServiceConfig2(
        handle,
        win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,
        spec.failure_actions_on_non_crash_failures,
    )
    win32service.ChangeServiceConfig2(
        handle,
        win32service.SERVICE_CONFIG_SERVICE_SID_INFO,
        win32service.SERVICE_SID_TYPE_UNRESTRICTED,
    )


def _create_or_update(spec: ServiceSpec) -> None:
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        try:
            handle = win32service.CreateService(
                scm,
                spec.name,
                spec.display_name,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                spec.bin_path,
                None,
                False,
                None,
                spec.account,
                None,
            )
        except pywintypes.error as exc:
            if exc.winerror != _ERROR_SERVICE_EXISTS:
                raise
            handle = win32service.OpenService(
                scm, spec.name, win32service.SERVICE_ALL_ACCESS
            )
            win32service.ChangeServiceConfig(
                handle,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                spec.bin_path,
                None,
                False,
                None,
                spec.account,
                None,
                spec.display_name,
            )
        try:
            _configure(handle, spec)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(scm)


def install(
    spec: ServiceSpec, layout: MachineLayout, *, peer_firewall: bool = False
) -> None:
    """Create (or re-configure) the service, the state folders and their ACLs, and -
    only with peer serving on - the named firewall rule. Idempotent."""
    for folder in (
        layout.root,
        layout.config_dir,
        layout.identity_dir,
        layout.cache_dir,
        layout.logs_dir,
    ):
        Path(folder).mkdir(parents=True, exist_ok=True)
    try:
        _create_or_update(spec)
    except pywintypes.error as exc:
        raise ServiceInstallError(
            f"creating service {spec.name} failed: {exc.strerror} ({exc.winerror})"
        ) from exc
    # After the service exists: its SID only resolves once it does.
    for acl in acl_plan(layout, service_sid(spec.name)):
        for argv in icacls_argv(acl):
            _run_tool(argv)
    if peer_firewall:
        rule = peer_firewall_rule()
        _run_tool(firewall_delete_argv(rule.name), ok_codes=frozenset({0, 1}))
        _run_tool(firewall_add_argv(rule))
    log.info(
        "agent.service.installed",
        service=spec.name,
        account=spec.account,
        peer_firewall=peer_firewall,
    )


def _wait_stopped(name: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = win32serviceutil.QueryServiceStatus(name)[1]
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_SERVICE_DOES_NOT_EXIST:
                return True
            raise
        if state == win32service.SERVICE_STOPPED:
            return True
        time.sleep(0.5)
    return False


def uninstall(name: str = SERVICE_NAME, *, stop_timeout_s: float = 60.0) -> bool:
    """Stop and delete the service, delete our firewall rules **by name** (D389).
    Data under ProgramData stays (D393: uninstall keeps data by default).
    Returns False when there was no service to remove."""
    existed = True
    try:
        with contextlib.suppress(pywintypes.error):
            win32serviceutil.StopService(name)
        if not _wait_stopped(name, stop_timeout_s):
            raise ServiceInstallError(
                f"service {name} did not stop within {stop_timeout_s:.0f} s"
            )
        win32serviceutil.RemoveService(name)
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_SERVICE_DOES_NOT_EXIST:
            existed = False
        elif exc.winerror != _ERROR_SERVICE_MARKED_FOR_DELETE:
            raise ServiceInstallError(
                f"removing service {name} failed: {exc.strerror} ({exc.winerror})"
            ) from exc
    # netsh exits 1 when no rule has that name: already gone is fine.
    _run_tool(
        firewall_delete_argv(peer_firewall_rule().name), ok_codes=frozenset({0, 1})
    )
    log.info("agent.service.uninstalled", service=name, existed=existed)
    return existed


def install_cli(
    *,
    python_exe: str,
    program_data: str | None,
    grace_s: float | None,
    peer_firewall: bool,
    start: bool,
) -> int:
    configure_logging("INFO", fmt="console", component="agent-service")
    layout = machine_layout(
        program_data or os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    )
    spec = build_service_spec(python_exe, layout, grace_s=grace_s)
    try:
        install(spec, layout, peer_firewall=peer_firewall)
        if start:
            win32serviceutil.StartService(spec.name)
    except (ServiceInstallError, pywintypes.error) as exc:
        print(f"install failed: {exc}", file=sys.stderr)  # noqa: T201 - CLI output
        return int(ExitCode.FAILURE)
    print(f"installed {spec.name} as {spec.account}")  # noqa: T201 - CLI output
    return int(ExitCode.OK)


def uninstall_cli() -> int:
    configure_logging("INFO", fmt="console", component="agent-service")
    try:
        existed = uninstall()
    except ServiceInstallError as exc:
        print(f"uninstall failed: {exc}", file=sys.stderr)  # noqa: T201 - CLI output
        return int(ExitCode.FAILURE)
    print("removed" if existed else "not installed")  # noqa: T201 - CLI output
    return int(ExitCode.OK)
