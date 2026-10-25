"""What the Windows installer does, in order, and what it tells the user (V0.17 G4).

The Inno Setup script (``packaging/windows/meshforge-agent.iss``) only copies the
bundle into ``C:\\Program Files\\MeshForge\\<version>\\`` and then runs, elevated,
from that new folder::

    python.exe -I -m meshforge.agent.winservice setup --result-file <file>
    python.exe -I -m meshforge.agent.winservice teardown [--remove-data]   (uninstall)

All decisions live here, against the small :class:`Ops` interface, so the whole
order - including the upgrade health check and the rollback - is unit-tested
with a fake on any OS; :mod:`meshforge.agent.winservice_ops` is the real one.

**setup** (fresh install, upgrade or repair - same steps, D385/D387/D389/D395):

1. the development scheduled task ``MeshForgeAgent`` (``deploy/windows``) is
   stopped and disabled, so its agent no longer holds the identity;
2. a running service is stopped (an upgrade: it runs the previous version);
3. the service is created or re-pointed at this version's ``python.exe``, the
   state folders and their ACLs are (re)applied;
4. the per-user identity is migrated (D387, same rules as Linux). A blocked
   migration (two identities, a damaged one, ...) leaves the service **stopped**
   and the result says exactly what to do - the install itself completes (Q13);
5. the user at this session (the one who ran the installer) may read the console
   token (Q30); the peer firewall rule follows ``[agent.peer] bind`` (D389);
6. the service is started and must become **healthy** within the timeout: SCM
   state RUNNING, the console answers ``/console/v1/health``, and - when the
   laptop has already joined - the agent logged ``agent.started``;
7. healthy -> ``current.txt`` and the PATH shims name this version. Not healthy
   on an upgrade -> the service is put back on the previous ``python.exe`` and
   started again (``rolled_back``); not healthy otherwise -> ``failed`` /
   ``needs_attention``, with the reason. Never "done" over a stopped service.

The result (:class:`SetupResult`) is JSON for the installer's last page; its
lines name paths and fixes, never a token or a key.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from meshforge.agent.wininstall import LEGACY_TASK_NAME, ProgramLayout
from meshforge.common.exit_codes import ExitCode
from meshforge.common.logging import get_logger
from meshforge.common.windows_service import SERVICE_NAME

__all__ = [
    "DEFAULT_HEALTH_TIMEOUT_S",
    "Health",
    "Ops",
    "OpsError",
    "ServiceStatus",
    "SetupOutcome",
    "SetupResult",
    "log_has_agent_started",
    "setup",
    "teardown",
    "wait_healthy",
]

log = get_logger(__name__)

DEFAULT_HEALTH_TIMEOUT_S: Final = 60.0
STOP_TIMEOUT_S: Final = 60.0
_POLL_S: Final = 1.0
NO_RESTART_SERVICE_EXIT_CODES: Final = frozenset({4, 5})
"""``classify_exit``'s STOPPED codes (config error, conflict)."""


class OpsError(Exception):
    """A Windows step failed; the message names the step (never a secret)."""


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    exists: bool
    state: str = "absent"
    """``running`` / ``stopped`` / ``start_pending`` / ``stop_pending`` / ... / ``absent``."""
    pid: int | None = None
    win32_exit_code: int = 0
    service_exit_code: int = 0
    python: str | None = None
    """The interpreter its ``binPath`` runs (``None``: not one of ours)."""


class Ops(Protocol):
    """The Windows side of :func:`setup` / :func:`teardown` (one method per step)."""

    def service_status(self) -> ServiceStatus: ...
    def stop_service(self, timeout_s: float) -> bool: ...
    def start_service(self) -> None: ...
    def install_service(self, python_exe: str) -> None: ...
    def uninstall_service(self) -> bool: ...
    def legacy_task_state(self) -> str | None: ...
    def end_and_disable_legacy_task(self) -> None: ...
    def delete_legacy_task(self) -> None: ...
    def migrate(self) -> tuple[str, list[str]]: ...
    def peer_serving(self) -> bool | None: ...
    def sync_firewall(self, peer_serving: bool) -> None: ...
    def console_readers(self) -> list[str]: ...
    def grant_console(self, sids: Sequence[str]) -> None: ...
    def console_enabled(self) -> bool: ...
    def console_healthy(self) -> bool: ...
    def agent_log_offset(self) -> int: ...
    def agent_started_since(self, offset: int) -> bool: ...
    def enrolled(self) -> bool: ...
    def node_id(self) -> str | None: ...
    def write_current(self, version_dir: str) -> None: ...
    def write_shims(self, version_dir: str) -> None: ...
    def ensure_on_path(self) -> None: ...
    def remove_from_path(self) -> None: ...
    def remove_docker_access(self) -> None: ...
    def remove_data(self) -> list[str]: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SetupOutcome(StrEnum):
    OK = "ok"
    """Installed (or upgraded) and the service is healthy."""
    NEEDS_ATTENTION = "needs_attention"
    """Installed, but the service is stopped on purpose (blocked migration, a
    configuration the agent refuses): the lines say what to do."""
    ROLLED_BACK = "rolled_back"
    """The new version did not become healthy; the previous one runs again."""
    FAILED = "failed"
    """The service is not running and could not be brought back."""


_EXIT: Final = {
    SetupOutcome.OK: ExitCode.OK,
    SetupOutcome.NEEDS_ATTENTION: ExitCode.CONFIG_ERROR,
    SetupOutcome.ROLLED_BACK: ExitCode.FAILURE,
    SetupOutcome.FAILED: ExitCode.FAILURE,
}


@dataclass(slots=True)
class SetupResult:
    outcome: SetupOutcome
    version: str
    lines: list[str] = field(default_factory=list)
    previous_python: str | None = None
    running_python: str | None = None
    node_id: str | None = None
    service_state: str = "absent"

    @property
    def exit_code(self) -> ExitCode:
        return _EXIT[self.outcome]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "version": self.version,
            "lines": list(self.lines),
            "previous_python": self.previous_python,
            "running_python": self.running_python,
            "node_id": self.node_id,
            "service_state": self.service_state,
        }

    def summary(self) -> str:
        """One headline for the installer's last page."""
        return {
            SetupOutcome.OK: "MeshForge is installed and running.",
            SetupOutcome.NEEDS_ATTENTION: (
                "MeshForge is installed but NOT running yet - see below."
            ),
            SetupOutcome.ROLLED_BACK: (
                "The new version did not start; the previous version runs again."
            ),
            SetupOutcome.FAILED: "MeshForge is installed but NOT running.",
        }[self.outcome]


# ------------------------------------------------------------------ health
_AGENT_STARTED: Final = re.compile(rb"(?<![\w.])agent\.started(?![\w.])")


def log_has_agent_started(data: bytes) -> bool:
    """The agent logged ``agent.started`` in ``data`` (a slice of ``agent.log``),
    in either log format: JSON (``"event": "agent.started"``, the default) or
    the console renderer a migrated developer config may still choose."""
    return _AGENT_STARTED.search(data) is not None


@dataclass(frozen=True, slots=True)
class Health:
    ok: bool
    reason: str
    status: ServiceStatus


def _stopped_reason(status: ServiceStatus) -> str:
    code = status.service_exit_code
    if code == ExitCode.CONFIG_ERROR:
        return (
            "the agent stopped on its own with exit code 4 (it refuses its "
            "configuration or identity) - see C:\\ProgramData\\MeshForge\\logs\\agent.log"
        )
    if code == ExitCode.CONFLICT:
        return (
            "the agent stopped with exit code 5 (another agent uses this identity) "
            "- stop the other one (e.g. a developer `meshforge-agent run`)"
        )
    return (
        f"the service stopped (exit {status.win32_exit_code}/{code}) - see "
        "C:\\ProgramData\\MeshForge\\logs\\service.log"
    )


def wait_healthy(
    ops: Ops,
    *,
    log_offset: int,
    timeout_s: float,
    need_console: bool,
    need_started: bool,
) -> Health:
    """Poll until healthy (module docstring step 6), the service stops, or the
    timeout passes. A STOPPED service ends the wait at once (exit 4/5 are not
    restarted, so waiting longer would only hide the reason)."""
    deadline = ops.monotonic() + timeout_s
    console_ok = not need_console
    started = not need_started
    status = ops.service_status()
    while True:
        status = ops.service_status()
        if not status.exists:
            return Health(False, "the service does not exist", status)
        if status.state == "stopped":
            return Health(False, _stopped_reason(status), status)
        if status.state == "running":
            console_ok = console_ok or ops.console_healthy()
            started = started or ops.agent_started_since(log_offset)
            if console_ok and started:
                return Health(True, "healthy", status)
        if ops.monotonic() >= deadline:
            missing = []
            if status.state != "running":
                missing.append(f"the service is {status.state}")
            if not console_ok:
                missing.append("the local console does not answer on 127.0.0.1:47130")
            if not started:
                missing.append("the agent did not log agent.started")
            return Health(
                False,
                f"not healthy within {timeout_s:.0f} s: " + "; ".join(missing),
                status,
            )
        ops.sleep(_POLL_S)


def _start_and_check(ops: Ops, timeout_s: float) -> Health:
    offset = ops.agent_log_offset()
    need_started = ops.enrolled()
    try:
        ops.start_service()
    except OpsError as exc:
        return Health(False, f"the service did not start: {exc}", ops.service_status())
    return wait_healthy(
        ops,
        log_offset=offset,
        timeout_s=timeout_s,
        need_console=ops.console_enabled(),
        need_started=need_started,
    )


# ------------------------------------------------------------------- setup
def _same_python(a: str | None, b: str) -> bool:
    return a is not None and a.strip().lower() == b.strip().lower()


def setup(
    ops: Ops,
    layout: ProgramLayout,
    *,
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
) -> SetupResult:
    """Install / upgrade / repair (module docstring). Never raises
    :class:`OpsError`: every failure is a result with its reason."""
    result = SetupResult(SetupOutcome.FAILED, layout.version_dir)
    try:
        return _setup(ops, layout, health_timeout_s, result)
    except OpsError as exc:
        result.lines.append(f"setup stopped: {exc}")
        try:
            result.service_state = ops.service_status().state
        except OpsError:
            result.service_state = "unknown"
        return result


def _setup(
    ops: Ops, layout: ProgramLayout, health_timeout_s: float, result: SetupResult
) -> SetupResult:
    new_python = str(layout.python_exe)
    lines = result.lines
    before = ops.service_status()
    previous = before.python if before.exists else None
    if previous is not None and _same_python(previous, new_python):
        previous = None  # a repair of the running version: nothing to go back to
    result.previous_python = previous
    _stop_legacy_task(ops, lines)
    running = before.exists and before.state not in ("stopped", "absent")
    if running and not ops.stop_service(STOP_TIMEOUT_S):
        lines.append(
            f"the running {SERVICE_NAME} service did not stop within "
            f"{STOP_TIMEOUT_S:.0f} s; nothing was changed - close MeshForge "
            "jobs and run the installer again"
        )
        result.service_state = ops.service_status().state
        return result
    ops.install_service(new_python)
    try:
        outcome, migration_lines = ops.migrate()
    except OpsError:
        raise  # a named Windows step failed: "setup stopped: ...", service stopped
    except Exception as exc:  # a bug: never start over an undecided identity
        log.exception("agent.service.setup_migration_error", cause=type(exc).__name__)
        outcome = "blocked"
        migration_lines = [
            f"identity migration failed unexpectedly ({type(exc).__name__}: {exc}); "
            "nothing was copied or started - run the installer again, and if it "
            "fails again send C:\\ProgramData\\MeshForge\\logs\\setup-result.json "
            "to the MeshForge team"
        ]
    lines.extend(migration_lines)
    _console_access(ops, lines)
    _firewall(ops, lines)
    if outcome == "blocked":
        # Q13: installed, but the service must not mint a new identity.
        _point_at(ops, layout.version_dir)
        result.outcome = SetupOutcome.NEEDS_ATTENTION
        result.service_state = ops.service_status().state
        lines.append(
            f"the {SERVICE_NAME} service was left STOPPED until the identity "
            "question above is resolved; then start it: "
            f"sc.exe start {SERVICE_NAME} (as administrator)"
        )
        return result
    health = _start_and_check(ops, health_timeout_s)
    if health.ok:
        return _finish_ok(ops, layout.version_dir, new_python, result)
    lines.append(f"version {layout.version_dir}: {health.reason}")
    if previous is not None:
        return _roll_back(ops, previous, health_timeout_s, result)
    result.service_state = health.status.state
    if (
        health.status.state == "stopped"
        and health.status.service_exit_code in NO_RESTART_SERVICE_EXIT_CODES
    ):
        _point_at(ops, layout.version_dir)
        result.outcome = SetupOutcome.NEEDS_ATTENTION
        lines.append(
            "fix the problem above, then start the service: "
            f"sc.exe start {SERVICE_NAME} (as administrator)"
        )
    return result


def _point_at(ops: Ops, version_dir: str) -> None:
    """``current.txt``, the PATH shims and the PATH entry name ``version_dir``."""
    ops.write_current(version_dir)
    ops.write_shims(version_dir)
    ops.ensure_on_path()


def _finish_ok(
    ops: Ops, version_dir: str, python: str, result: SetupResult
) -> SetupResult:
    _point_at(ops, version_dir)
    result.outcome = SetupOutcome.OK
    result.running_python = python
    result.service_state = "running"
    result.node_id = ops.node_id()
    if ops.enrolled():
        result.lines.append(
            f"MeshForge {version_dir} is running as node {result.node_id or '?'}."
        )
    else:
        result.lines.append(
            f"MeshForge {version_dir} is running. Open MeshForge from the Start "
            "menu to join a fabric."
        )
    return result


def _roll_back(
    ops: Ops, previous: str, timeout_s: float, result: SetupResult
) -> SetupResult:
    from meshforge.agent.wininstall import program_layout

    lines = result.lines
    try:
        previous_dir = program_layout(previous).version_dir
    except ValueError:
        previous_dir = None
    if not ops.stop_service(STOP_TIMEOUT_S):
        # Re-pointing a service that still runs the new version would leave the
        # SCM, current.txt and the running process disagreeing: stop here.
        result.outcome = SetupOutcome.FAILED
        result.service_state = ops.service_status().state
        lines.append(
            f"rolling back to {previous} was not possible: the {SERVICE_NAME} "
            f"service did not stop within {STOP_TIMEOUT_S:.0f} s; it still runs "
            "the new version - restart the computer, then run the installer again"
        )
        return result
    ops.install_service(previous)
    health = _start_and_check(ops, timeout_s)
    result.service_state = health.status.state
    if not health.ok:
        lines.append(f"rolling back to {previous} failed too: {health.reason}")
        result.outcome = SetupOutcome.FAILED
        return result
    if previous_dir is not None:
        _point_at(ops, previous_dir)
    result.outcome = SetupOutcome.ROLLED_BACK
    result.running_python = previous
    result.node_id = ops.node_id()
    lines.append(
        f"the previous version ({previous_dir or previous}) runs again; the new "
        "files stay installed - send the log above to the MeshForge team"
    )
    return result


def _stop_legacy_task(ops: Ops, lines: list[str]) -> None:
    state = ops.legacy_task_state()
    if state is None:
        return
    try:
        ops.end_and_disable_legacy_task()
    except OpsError as exc:
        lines.append(
            f"the development scheduled task \\{LEGACY_TASK_NAME} (not the service) "
            f"could not be stopped: {exc}; stop it in Task Scheduler - its agent "
            "would compete with the service for the same identity"
        )
        return
    lines.append(
        f"stopped and disabled the development scheduled task \\{LEGACY_TASK_NAME} "
        f"(was {state}; the {SERVICE_NAME} service replaces it; uninstall removes it)"
    )


def _console_access(ops: Ops, lines: list[str]) -> None:
    try:
        readers = ops.console_readers()
        if readers:
            ops.grant_console(readers)
    except OpsError as exc:
        lines.append(
            f"console access for your user could not be set: {exc}; an "
            "administrator can grant it: meshforge-agent console-access (see the docs)"
        )


def _firewall(ops: Ops, lines: list[str]) -> None:
    try:
        peer = ops.peer_serving()
        if peer is None:
            lines.append(
                "the agent configuration could not be read, so the peer firewall "
                "rule was not changed"
            )
            return
        ops.sync_firewall(peer)
    except OpsError as exc:
        lines.append(f"the peer firewall rule could not be set: {exc}")


# ---------------------------------------------------------------- teardown
@dataclass(slots=True)
class TeardownResult:
    ok: bool
    lines: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.OK if self.ok else ExitCode.FAILURE

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "lines": list(self.lines)}


def teardown(ops: Ops, *, remove_data: bool) -> TeardownResult:
    """Uninstall (D393): Docker access, the service, its firewall rule (by
    name), the legacy task, the PATH entry; data only with ``remove_data``.
    Each step runs even if an earlier best-effort one failed; only a service
    that cannot be removed fails the result (the files would be in use)."""
    result = TeardownResult(ok=True)
    lines = result.lines
    for what, step in (
        ("Docker access", ops.remove_docker_access),
        ("PATH entry", ops.remove_from_path),
    ):
        try:
            step()
        except OpsError as exc:
            lines.append(f"{what} not removed: {exc}")
    try:
        existed = ops.uninstall_service()
        lines.append(
            f"removed the {SERVICE_NAME} service and its firewall rule"
            if existed
            else f"no {SERVICE_NAME} service was installed"
        )
    except OpsError as exc:
        result.ok = False
        lines.append(f"the {SERVICE_NAME} service could not be removed: {exc}")
    try:
        if ops.legacy_task_state() is not None:
            ops.delete_legacy_task()
            lines.append(f"removed the development scheduled task \\{LEGACY_TASK_NAME}")
    except OpsError as exc:
        lines.append(f"the scheduled task \\{LEGACY_TASK_NAME} was not removed: {exc}")
    if remove_data and result.ok:
        try:
            lines.extend(ops.remove_data())
        except OpsError as exc:
            result.ok = False
            lines.append(f"the data could not be removed: {exc}")
    elif remove_data:
        lines.append("data kept: the service is still installed")
    else:
        lines.append(
            "data kept (identity, cache, logs) in C:\\ProgramData\\MeshForge - a "
            "reinstall keeps this laptop's node"
        )
    log.info("agent.service.teardown", ok=result.ok, remove_data=remove_data)
    return result
