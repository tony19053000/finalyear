"""The agent as a native Windows service (V0.17 D386, spike SP1).

``MeshForgeAgent`` is a pywin32 ``ServiceFramework`` service hosted by the bundled
``python.exe`` (no third-party wrapper, ADR-0009/ADR-0022). This module holds the
**pure** parts - everything that can be decided and unit-tested on Linux - and the
command line; the pywin32 code lives in :mod:`meshforge.agent.winservice_host`
(Windows only, imported lazily) and the child supervision in
:mod:`meshforge.agent.winservice_supervisor` (cross-platform, real-process tested).

**Shape: a host process supervising the agent as a child**, not the agent loop
inside the service thread. Reasons:

* pywin32 runs ``SvcDoRun`` on the service thread, not the interpreter's main
  thread, so the agent's signal handlers (``signal.signal`` needs the main thread)
  and its graceful-stop path would have to be rewritten for the service case.
  As a child, ``meshforge-agent run`` is exactly the process every other start
  path runs - one tested code path.
* The child's **exit code** is the agent's real verdict (4/5 = do not restart),
  read by the host and turned into SCM terms (:func:`classify_exit`) - the same
  contract systemd's ``RestartPreventExitStatus=4 5`` gets on Linux.
* A native crash in a driver library (NVML, CUDA) kills the child, not the
  process that talks to the SCM.
* The child sits in a **job object** with ``KILL_ON_JOB_CLOSE``
  (:mod:`meshforge.agent.process` - reused, not re-implemented): stop terminates
  the whole job (agent + every job tree it started, whose own jobs are nested in
  it), and if the host itself is killed the kernel closes the handle and kills the
  tree - no orphan holds the identity lock when the SCM restarts the service.

**Graceful stop** without a console: the host keeps the child's stdin as a pipe
and closes it on stop. The child (``winservice child -- run ...``) watches stdin
in a thread and, on EOF, delivers ``SIGBREAK`` (``SIGTERM`` off Windows) to its own
main thread with :func:`_thread.interrupt_main` - the agent's existing stop
handler then stops attempts, says Goodbye and exits 0. After ``grace_s`` the host
terminates the job.

**Restart policy** (D386: restart x3 with 5 s delay, then nothing): SCM failure
actions fire on a *crash* (the process ends without reporting STOPPED). With
``FailureActionsOnNonCrashFailures`` **off**, a service that reports STOPPED with an
error code is *not* restarted. So: exit 4/5 -> report STOPPED with
``ERROR_SERVICE_SPECIFIC_ERROR`` and the agent's code (visible in ``sc query``, no
restart); any other unrequested exit -> the host exits without reporting STOPPED
(a crash to the SCM) -> restart.
"""

from __future__ import annotations

import argparse
import hashlib
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import BinaryIO, Final, Literal

from meshforge.common.exit_codes import ExitCode

__all__ = [
    "AGENT_LOG_NAME",
    "ERROR_SERVICE_SPECIFIC_ERROR",
    "FAILURE_RESET_S",
    "NO_RESTART_EXIT_CODES",
    "PEER_FIREWALL_RULE",
    "PEER_PORT",
    "RESTART_ATTEMPTS",
    "RESTART_DELAY_MS",
    "SERVICE_ACCOUNT",
    "SERVICE_LOG_NAME",
    "SERVICE_NAME",
    "SHUTDOWN_GRACE_S",
    "SID_ADMINISTRATORS",
    "SID_SYSTEM",
    "STOP_GRACE_S",
    "Access",
    "DirAcl",
    "ExitDecision",
    "FailureAction",
    "FirewallRule",
    "Grant",
    "MachineLayout",
    "ServiceSpec",
    "StopKind",
    "acl_plan",
    "agent_child_argv",
    "build_service_spec",
    "classify_exit",
    "firewall_add_argv",
    "firewall_delete_argv",
    "host_argv",
    "icacls_argv",
    "machine_layout",
    "main",
    "peer_firewall_rule",
    "service_sid",
    "start_stop_watcher",
    "stop_signal",
    "watch_for_stop",
]

SERVICE_NAME: Final = "MeshForgeAgent"
DISPLAY_NAME: Final = "MeshForge Agent"
DESCRIPTION: Final = (
    "MeshForge compute-fabric node agent: shares this computer's CPU/GPU with the "
    "fabric, within the owner's policy."
)
SERVICE_ACCOUNT: Final = rf"NT SERVICE\{SERVICE_NAME}"
"""Virtual account (D386): no password, no rights beyond what install grants."""

RESTART_DELAY_MS: Final = 5_000
RESTART_ATTEMPTS: Final = 3
FAILURE_RESET_S: Final = 3_600
"""The SCM's failure counter resets after an hour without a failure: three crashes
within an hour stop the service (a crash loop) but a laptop that crashed once
last week still gets its three restarts."""

STOP_GRACE_S: Final = 30.0
"""How long a stop waits for the agent's own graceful path (attempts report
``agent_shutdown``, Goodbye) before the job object is terminated."""
SHUTDOWN_GRACE_S: Final = 5.0
"""System shutdown gives services only a few seconds: shorter grace there."""

NO_RESTART_EXIT_CODES: Final = frozenset({ExitCode.CONFIG_ERROR, ExitCode.CONFLICT})
"""Exit codes a restart cannot fix (bad config / revoked; another instance owns
the identity) - OPERATIONS §2's ``RestartPreventExitStatus=4 5``."""

ERROR_SERVICE_SPECIFIC_ERROR: Final = 1066
"""``dwWin32ExitCode`` meaning "see ``dwServiceSpecificExitCode``"."""

SID_SYSTEM: Final = "S-1-5-18"
SID_ADMINISTRATORS: Final = "S-1-5-32-544"

AGENT_LOG_NAME: Final = "agent.log"
SERVICE_LOG_NAME: Final = "service.log"

PEER_FIREWALL_RULE: Final = "MeshForge-Agent-Peer"
PEER_PORT: Final = 47120
FIREWALL_PROFILES: Final = "private,domain"
"""D389: never the Public profile."""


# ------------------------------------------------------------------ layout
@dataclass(frozen=True)
class MachineLayout:
    """D385 machine state: ``C:\\ProgramData\\MeshForge\\{config,identity,cache,logs}``.

    ``root`` is the agent's ``data_dir`` (so ``identity`` is ``data_dir/identity``,
    ADR-0013); environments and caches go under ``cache`` via the config file.
    """

    root: PureWindowsPath

    @property
    def config_dir(self) -> PureWindowsPath:
        return self.root / "config"

    @property
    def config_file(self) -> PureWindowsPath:
        return self.config_dir / "config.toml"

    @property
    def identity_dir(self) -> PureWindowsPath:
        return self.root / "identity"

    @property
    def cache_dir(self) -> PureWindowsPath:
        return self.root / "cache"

    @property
    def logs_dir(self) -> PureWindowsPath:
        return self.root / "logs"


def machine_layout(program_data: str | PureWindowsPath) -> MachineLayout:
    """The layout under ``%ProgramData%`` (normally ``C:\\ProgramData``)."""
    return MachineLayout(PureWindowsPath(program_data) / "MeshForge")


# ------------------------------------------------------------- service spec
@dataclass(frozen=True)
class FailureAction:
    """One SCM recovery action: ``restart`` after ``delay_ms``, or ``none``."""

    kind: Literal["restart", "none"]
    delay_ms: int


@dataclass(frozen=True)
class ServiceSpec:
    """Everything ``install`` configures, decided here so it is testable."""

    name: str
    display_name: str
    description: str
    account: str
    bin_path: str
    delayed_auto_start: bool
    failure_actions: tuple[FailureAction, ...]
    failure_reset_s: int
    failure_actions_on_non_crash_failures: bool
    sid_type: Literal["unrestricted"]


def host_argv(
    python_exe: str,
    layout: MachineLayout,
    *,
    grace_s: float | None = None,
    child_argv: Sequence[str] = (),
) -> list[str]:
    """The service's command line: ``python -I -m meshforge.agent.winservice host``.

    ``-I`` (isolated): no ``PYTHON*`` environment variables, no user site, no
    current directory on ``sys.path`` (the service starts in ``System32``).
    ``child_argv`` replaces the agent with another program (diagnostics, SP1's
    process-tree probe); empty = the agent.
    """
    argv = [
        python_exe,
        "-I",
        "-m",
        "meshforge.agent.winservice",
        "host",
        "--config",
        str(layout.config_file),
        "--log-dir",
        str(layout.logs_dir),
    ]
    if grace_s is not None:
        argv += ["--grace-s", f"{grace_s:g}"]
    if child_argv:
        argv += ["--", *child_argv]
    return argv


def agent_child_argv(python_exe: str, config_file: str) -> list[str]:
    """The supervised agent: the normal ``run`` command behind the stdin stop watcher."""
    return [
        python_exe,
        "-I",
        "-m",
        "meshforge.agent.winservice",
        "child",
        "--",
        "run",
        "--config",
        config_file,
    ]


def recovery_actions() -> tuple[FailureAction, ...]:
    """Restart x3 (5 s apart), then nothing. The explicit trailing ``none`` matters:
    the SCM repeats the *last* action for every later failure."""
    restart = FailureAction("restart", RESTART_DELAY_MS)
    return (*([restart] * RESTART_ATTEMPTS), FailureAction("none", 0))


def build_service_spec(
    python_exe: str,
    layout: MachineLayout,
    *,
    grace_s: float | None = None,
    child_argv: Sequence[str] = (),
) -> ServiceSpec:
    return ServiceSpec(
        name=SERVICE_NAME,
        display_name=DISPLAY_NAME,
        description=DESCRIPTION,
        account=SERVICE_ACCOUNT,
        bin_path=subprocess.list2cmdline(
            host_argv(python_exe, layout, grace_s=grace_s, child_argv=child_argv)
        ),
        delayed_auto_start=True,
        failure_actions=recovery_actions(),
        failure_reset_s=FAILURE_RESET_S,
        # Off: a reported STOPPED with an error code (exit 4/5) is not a failure
        # to recover from; only a crash (no STOPPED) triggers the restarts.
        failure_actions_on_non_crash_failures=False,
        # The service SID is in the token, so ACLs can name the service exactly.
        sid_type="unrestricted",
    )


# --------------------------------------------------------------- exit codes
class StopKind(StrEnum):
    STOPPED = "stopped"
    """Report SERVICE_STOPPED: no recovery action runs."""
    RESTART = "restart"
    """Exit without reporting STOPPED: the SCM sees a crash and restarts."""


@dataclass(frozen=True)
class ExitDecision:
    kind: StopKind
    reason: str
    win32_exit_code: int = 0
    service_exit_code: int = 0
    process_exit_code: int = 0
    """Only for :attr:`StopKind.RESTART`: the host's own exit code (1..255)."""


def classify_exit(returncode: int, *, stop_requested: bool) -> ExitDecision:
    """The agent child's exit -> what the host tells the SCM.

    * stop requested (SvcStop / shutdown): STOPPED, code 0, whatever the child did.
    * 4 CONFIG_ERROR / 5 CONFLICT: STOPPED with the code as service-specific exit
      code - no restart (a restart cannot fix it), and ``sc query`` shows why.
    * anything else, 0 included (an agent that stopped by itself is restarted, like
      systemd ``Restart=always``): RESTART.
    """
    if stop_requested:
        return ExitDecision(StopKind.STOPPED, "stop_requested")
    if returncode in NO_RESTART_EXIT_CODES:
        return ExitDecision(
            StopKind.STOPPED,
            "no_restart_exit_code",
            win32_exit_code=ERROR_SERVICE_SPECIFIC_ERROR,
            service_exit_code=returncode,
        )
    code = returncode if 0 < returncode < 256 else 1
    return ExitDecision(StopKind.RESTART, "unexpected_exit", process_exit_code=code)


# --------------------------------------------------------------------- ACLs
class Access(StrEnum):
    """icacls simple rights, inherited by files and sub-folders (``(OI)(CI)``)."""

    FULL = "F"
    MODIFY = "M"
    READ = "RX"


@dataclass(frozen=True)
class Grant:
    sid: str
    access: Access


@dataclass(frozen=True)
class DirAcl:
    path: PureWindowsPath
    grants: tuple[Grant, ...]
    owner_sid: str | None = None


def service_sid(name: str = SERVICE_NAME) -> str:
    """The per-service SID ``NT SERVICE\\<name>``: ``S-1-5-80-`` + the SHA-1 of the
    upper-cased name in UTF-16LE, as five little-endian 32-bit sub-authorities.
    Deterministic, so the ACL can be computed before (and checked against
    ``sc showsid``) without a name lookup that depends on the system language."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()  # noqa: S324
    subs = struct.unpack("<5I", digest)
    return "S-1-5-80-" + "-".join(str(s) for s in subs)


def acl_plan(layout: MachineLayout, svc_sid: str) -> tuple[DirAcl, ...]:
    """D385: SYSTEM + Administrators + the service SID only, nothing inherited.

    The root holds the agent's own state files (``scores.json``, ``policy.toml``,
    ``work``), so the service may modify it; ``config`` is read-only to the service
    (the owner/installer writes it); ``identity`` is owned by the service account
    (ADR-0013 owner-only rule; the agent then restricts the key file to itself).
    """
    admin = (Grant(SID_SYSTEM, Access.FULL), Grant(SID_ADMINISTRATORS, Access.FULL))
    return (
        DirAcl(layout.root, (*admin, Grant(svc_sid, Access.MODIFY))),
        DirAcl(layout.config_dir, (*admin, Grant(svc_sid, Access.READ))),
        DirAcl(
            layout.identity_dir,
            (*admin, Grant(svc_sid, Access.FULL)),
            owner_sid=svc_sid,
        ),
        DirAcl(layout.cache_dir, (*admin, Grant(svc_sid, Access.MODIFY))),
        DirAcl(layout.logs_dir, (*admin, Grant(svc_sid, Access.MODIFY))),
    )


def icacls_argv(acl: DirAcl) -> list[list[str]]:
    """The ``icacls`` commands for one folder: remove inherited ACEs and replace
    every grant (``/grant:r``), SIDs as ``*S-...`` (no localised account names);
    then the owner, if any."""
    grant = ["icacls", str(acl.path), "/inheritance:r"]
    for g in acl.grants:
        grant += ["/grant:r", f"*{g.sid}:(OI)(CI){g.access.value}"]
    cmds = [grant]
    if acl.owner_sid is not None:
        cmds.append(["icacls", str(acl.path), "/setowner", f"*{acl.owner_sid}"])
    return cmds


# ----------------------------------------------------------------- firewall
@dataclass(frozen=True)
class FirewallRule:
    name: str
    port: int
    protocol: Literal["TCP", "UDP"] = "TCP"
    profiles: str = FIREWALL_PROFILES


def peer_firewall_rule(port: int = PEER_PORT) -> FirewallRule:
    """D389: only installed when peer serving is on."""
    return FirewallRule(PEER_FIREWALL_RULE, port)


def firewall_add_argv(rule: FirewallRule) -> list[str]:
    return [
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name={rule.name}", "dir=in", "action=allow",
        f"protocol={rule.protocol}", f"localport={rule.port}",
        f"profile={rule.profiles}",
    ]  # fmt: skip


def firewall_delete_argv(name: str) -> list[str]:
    """Delete **by name** (D389): never by port, never a rule we did not create."""
    return ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"]


# ------------------------------------------------- child-side stop watcher
def stop_signal() -> signal.Signals:
    """The signal the child delivers to itself on stop: ``SIGBREAK`` on Windows
    (the agent's console-stop signal), ``SIGTERM`` elsewhere."""
    sig: signal.Signals = getattr(signal, "SIGBREAK", signal.SIGTERM)
    return sig


def watch_for_stop(
    stream: BinaryIO,
    *,
    signum: signal.Signals,
    interval_s: float = 1.0,
    interrupt: Callable[[signal.Signals], None] | None = None,
) -> None:
    """Block until ``stream`` (the child's stdin) reaches EOF, then deliver
    ``signum`` to the main thread every ``interval_s`` until the process exits.

    Repeated on purpose: :func:`_thread.interrupt_main` does nothing while the
    signal still has its default handler, i.e. before the agent installed its stop
    handler (start-up); ``request_stop`` is idempotent, so repeats are harmless.
    """
    import _thread

    deliver = interrupt or (lambda s: _thread.interrupt_main(s))
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):
        pass  # a broken pipe is the host going away: also a stop
    while True:
        deliver(signum)
        time.sleep(interval_s)


def start_stop_watcher(stream: BinaryIO) -> threading.Thread:
    thread = threading.Thread(
        target=watch_for_stop,
        args=(stream,),
        kwargs={"signum": stop_signal()},
        name="service-stop-watcher",
        daemon=True,
    )
    thread.start()
    return thread


def _child_main(agent_args: Sequence[str]) -> int:
    start_stop_watcher(sys.stdin.buffer)
    from meshforge.agent.main import PROG_NAME, app

    try:
        app(args=list(agent_args), prog_name=PROG_NAME)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return int(ExitCode.OK)


# ---------------------------------------------------------------- command line
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m meshforge.agent.winservice",
        description="MeshForge agent Windows service (host, install, uninstall).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    host = sub.add_parser("host", help="Run as the service (started by the SCM).")
    host.add_argument("--config", required=True)
    host.add_argument("--log-dir", required=True)
    host.add_argument("--grace-s", type=float, default=STOP_GRACE_S)
    host.add_argument("child_argv", nargs=argparse.REMAINDER)
    child = sub.add_parser("child", help="Run the agent behind the stdin stop watcher.")
    child.add_argument("agent_args", nargs=argparse.REMAINDER)
    inst = sub.add_parser("install", help="Create and configure the service (admin).")
    inst.add_argument("--python-exe", default=sys.executable)
    inst.add_argument("--program-data", default=None)
    inst.add_argument("--grace-s", type=float, default=None)
    inst.add_argument(
        "--peer-firewall",
        action="store_true",
        help=f"Add the {PEER_FIREWALL_RULE} rule (only when peer serving is on).",
    )
    inst.add_argument("--start", action="store_true", help="Start after install.")
    sub.add_parser("uninstall", help="Stop and delete the service and its rules.")
    return p


def _strip_dashdash(argv: Sequence[str]) -> list[str]:
    rest = list(argv)
    return rest[1:] if rest[:1] == ["--"] else rest


if sys.platform == "win32":

    def _windows_command(args: argparse.Namespace) -> int:
        from meshforge.agent import winservice_host as host

        if args.command == "host":
            return host.run_host(
                config=args.config,
                log_dir=args.log_dir,
                grace_s=args.grace_s,
                child_argv=_strip_dashdash(args.child_argv),
            )
        if args.command == "install":
            return host.install_cli(
                python_exe=args.python_exe,
                program_data=args.program_data,
                grace_s=args.grace_s,
                peer_firewall=args.peer_firewall,
                start=args.start,
            )
        return host.uninstall_cli()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "child":
        return _child_main(_strip_dashdash(args.agent_args))
    if sys.platform == "win32":
        return _windows_command(args)
    else:  # both branches platform-checked: mypy reachability on either OS
        print(  # noqa: T201 - CLI output
            f"{args.command}: the MeshForge Windows service exists only on Windows",
            file=sys.stderr,
        )
        return int(ExitCode.USAGE_ERROR)


if __name__ == "__main__":
    sys.exit(main())
