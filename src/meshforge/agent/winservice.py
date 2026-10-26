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
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Final, Literal, Protocol

from meshforge.common.exit_codes import ExitCode
from meshforge.common.windows_service import (
    INSTALL_MODE_ENV_VAR,
    SERVICE_ACCOUNT,
    SERVICE_NAME,
    SID_ADMINISTRATORS,
    SID_SYSTEM,
    service_sid,
)

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
    "RawReader",
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

DISPLAY_NAME: Final = "MeshForge Agent"
DESCRIPTION: Final = (
    "MeshForge compute-fabric node agent: shares this computer's CPU/GPU with the "
    "fabric, within the owner's policy."
)
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

AGENT_LOG_NAME: Final = "agent.log"
SERVICE_LOG_NAME: Final = "service.log"

PEER_FIREWALL_RULE: Final = "MeshForge-Agent-Peer"
PEER_PORT: Final = 47120
FIREWALL_PROFILES: Final = "private,domain"
"""D389: never the Public profile."""


# ------------------------------------------------------------------ layout
@dataclass(frozen=True)
class MachineLayout:
    """D385 machine state: ``C:\\ProgramData\\MeshForge\\{config,identity,cache,logs,console}``.

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

    @property
    def console_dir(self) -> PureWindowsPath:
        """The local console's token folder (V0.17 G4, D391): the agent's own
        default ``<data_dir>/console`` (``settings.console_token_dir``)."""
        return self.root / "console"

    @property
    def folders(self) -> tuple[PureWindowsPath, ...]:
        """Every state folder, root first (creation and ACL order)."""
        return (
            self.root,
            self.config_dir,
            self.identity_dir,
            self.cache_dir,
            self.logs_dir,
            self.console_dir,
        )


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


def agent_child_argv(python_exe: str, config_file: str | None) -> list[str]:
    """The supervised agent: the normal ``run`` command behind the stdin stop watcher.

    ``config_file`` only when it exists: like the Linux unit, a fresh install has
    no ``config.toml`` and runs on the system-mode defaults (an explicit
    ``--config`` must exist, ``common.config.resolve_config_file``)."""
    argv = [
        python_exe,
        "-I",
        "-m",
        "meshforge.agent.winservice",
        "child",
        "--",
        "run",
    ]
    return [*argv, "--config", config_file] if config_file else argv


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The agent child's environment: the service's own, plus
    ``MESHFORGE_INSTALL_MODE=system`` (D385, Q11: set by the service, never
    guessed) - the Windows twin of the systemd unit's ``Environment=``."""
    return {**environ, INSTALL_MODE_ENV_VAR: "system"}


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


def acl_plan(
    layout: MachineLayout, svc_sid: str, console_readers: Sequence[str] = ()
) -> tuple[DirAcl, ...]:
    """D385: SYSTEM + Administrators + the service SID only, nothing inherited.

    The root holds the agent's own state files (``scores.json``, ``policy.toml``,
    ``node_name``, ``work``), so the service may modify it - the files the console
    edits (owner policy, node name) live there, not in ``config``; ``config`` is
    read-only to the service (the owner/installer writes it); ``identity`` is
    owned by the service account (ADR-0013; the agent then restricts each file,
    ``common.windows_service.windows_private_sids``).

    ``console`` (V0.17 G4, Q30) also lets ``console_readers`` - the user who ran
    the installer and anyone an administrator added - **read**: its token file
    inherits that ACL, which is the whole permission to open the console (the
    Windows twin of the Linux ``meshforge-admin`` group).
    """
    admin = (Grant(SID_SYSTEM, Access.FULL), Grant(SID_ADMINISTRATORS, Access.FULL))
    readers = tuple(
        Grant(sid, Access.READ)
        for sid in dict.fromkeys(console_readers)
        if sid not in (svc_sid, SID_SYSTEM, SID_ADMINISTRATORS)
    )
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
        DirAcl(layout.console_dir, (*admin, Grant(svc_sid, Access.MODIFY), *readers)),
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


class RawReader(Protocol):
    """An *unbuffered* byte source (``sys.stdin.buffer.raw``)."""

    def read(self, size: int = -1, /) -> bytes | None: ...


def watch_for_stop(
    stream: RawReader,
    *,
    signum: signal.Signals,
    interval_s: float = 1.0,
    interrupt: Callable[[signal.Signals], None] | None = None,
) -> None:
    """Block until ``stream`` (the child's stdin) reaches EOF, then deliver
    ``signum`` to the main thread every ``interval_s`` until the process exits.

    ``stream`` must be **unbuffered**: a daemon thread blocked in a
    ``BufferedReader.read`` holds the reader's lock, and when the agent exits on
    its own (exit 4 on a bad config) the interpreter's shutdown flush of stdin
    cannot take that lock -> ``Fatal Python error: _enter_buffered_busy`` and a
    crash exit code instead of 4 (seen in SP1 run 40097330471). A raw read holds
    no Python-level lock.

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


def start_stop_watcher(stream: RawReader) -> threading.Thread:
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
    # Unbuffered fd 0 (see watch_for_stop), not sys.stdin.buffer.
    start_stop_watcher(open(0, "rb", buffering=0, closefd=False))  # noqa: SIM115
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
        description=(
            "MeshForge agent Windows service: the service host, and the installer's "
            "and administrators' commands (all but host/child need an elevated prompt)."
        ),
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
    # V0.17 G4: the installer's steps and the administrators' tools.
    setup = sub.add_parser(
        "setup",
        help="Installer: install/upgrade with this bundle, migrate, start, check.",
    )
    setup.add_argument("--result-file", default=None, help="JSON result (tests).")
    setup.add_argument(
        "--message-file", default=None, help="Text for the installer's last page."
    )
    setup.add_argument("--health-timeout", type=float, default=60.0)
    teardown = sub.add_parser(
        "teardown", help="Uninstaller: remove the service, rules, task, PATH entry."
    )
    teardown.add_argument(
        "--remove-data",
        action="store_true",
        help="Also delete C:\\ProgramData\\MeshForge (identity, cache, logs).",
    )
    teardown.add_argument("--result-file", default=None)
    teardown.add_argument("--message-file", default=None)
    update = sub.add_parser(
        "update-path", help="Point the service at another version's python.exe."
    )
    update.add_argument("--python-exe", default=sys.executable)
    health = sub.add_parser("health", help="Is the running service healthy?")
    health.add_argument("--timeout", type=float, default=60.0)
    access = sub.add_parser(
        "console-access", help="Who may open the local MeshForge console (Q30)."
    )
    access.add_argument("action", choices=["add", "remove"])
    access.add_argument("--user", action="append", help="DOMAIN\\name or name")
    access.add_argument("--sid", action="append", help="S-1-5-21-...")
    access.add_argument(
        "--session-user",
        action="store_true",
        help="The user signed in to this Windows session (the installer uses it).",
    )
    sub.add_parser(
        "docker-access",
        help="Add the service account to docker-users and restart it (Q31).",
    )
    sub.add_parser(
        "firewall-sync", help="Make the peer firewall rule follow [agent.peer] bind."
    )
    return p


ADMIN_COMMANDS: Final = frozenset(
    {
        "setup",
        "teardown",
        "update-path",
        "health",
        "console-access",
        "docker-access",
        "firewall-sync",
    }
)


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
        if args.command in ADMIN_COMMANDS:
            from meshforge.agent.winservice_admin import run_admin_command

            return run_admin_command(args)
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
