"""The agent process with the local console: console always up, fabric part on demand.

Without the console, ``meshforge-agent run`` is :func:`~meshforge.agent.runtime.
run_agent`: connect, serve, exit with a code (4 = not enrolled / config, 5 =
conflict) that tells the service manager whether to restart. A laptop owner who
has only a browser cannot act on an exit code, so with the console on
:class:`Supervisor` runs instead::

    console server (always) + fabric part = runtime.serve() (when enrolled)

    WAITING_FOR_JOIN --enrolled (console join, or `meshforge-agent join`)--> STARTING
    STARTING --client exists--> RUNNING
    RUNNING --restart--> STOPPING --> STARTING          (settings re-read)
    RUNNING --leave--> STOPPING --> identity reset --> WAITING_FOR_JOIN
    RUNNING --typed stop (revoked, conflict, config)--> STOPPED (reason kept)
    STOPPED --restart--> STARTING ; STOPPED --leave--> WAITING_FOR_JOIN
    any --SIGINT/SIGTERM--> fabric part stopped, console closed, exit 0

The enrolment is polled every ``poll_s`` (the CLI join writes ``node.crt``
from another process), so no path unit is needed. A typed stop keeps the
process - and the console that explains it - up; only an **unexpected** crash
of the fabric part still exits ``FAILURE`` so the service manager restarts a
clean process. ``restart_process()`` exits ``OK`` on purpose: the system unit
has ``Restart=always``, and a new process is the only way to pick up new
supplementary groups (``docker``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from meshforge.agent.console.setup_state import clear_setup
from meshforge.agent.enrollment import is_enrolled
from meshforge.agent.identity import reset as reset_identity
from meshforge.agent.runtime import install_signal_handlers, serve
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.errors import NodeRevokedError
from meshforge.agent.settings import AgentSettings
from meshforge.common.errors import ExitCode, MeshForgeError
from meshforge.common.logging import get_logger

__all__ = [
    "FabricPhase",
    "ServeFn",
    "StopReason",
    "Supervisor",
]

log = get_logger(__name__)

DEFAULT_POLL_S: Final = 2.0
STOP_WAIT_S: Final = 60.0
"""How long a stop waits for the fabric part to finish its Goodbye before the
task is cancelled (its own shutdown is bounded well below this)."""


class FabricPhase(StrEnum):
    WAITING_FOR_JOIN = "waiting_for_join"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class StopReason:
    """Why the fabric part is not running (``STOPPED``)."""

    code: str
    """``revoked`` | ``conflict`` | ``config`` | ``identity`` | ``error`` |
    ``ended``."""
    message: str
    failure_type: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "failure_type": self.failure_type,
        }


class ServeFn(Protocol):
    def __call__(
        self,
        settings: AgentSettings,
        *,
        install_signals: bool = ...,
        on_client: Callable[[AgentSessionClient], None] | None = ...,
    ) -> Coroutine[Any, Any, AgentSessionClient]: ...


def _reason_for(exc: MeshForgeError) -> StopReason:
    if isinstance(exc, NodeRevokedError):
        code = "revoked"
    elif int(exc.exit_code) == int(ExitCode.CONFLICT):
        code = "conflict"
    elif exc.failure_type.value == "config_error":
        code = "config"
    else:
        code = "error"
    return StopReason(code, exc.message, exc.failure_type.value)


class Supervisor:
    """See module docstring. One event loop; ``run()`` once."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        reload_settings: Callable[[], AgentSettings] | None = None,
        serve_fn: ServeFn = serve,
        poll_s: float = DEFAULT_POLL_S,
        install_signals: bool = True,
    ) -> None:
        self.settings = settings
        self._reload = reload_settings
        self._serve: ServeFn = serve_fn
        self._poll_s = poll_s
        self._install_signals = install_signals
        self.phase = FabricPhase.WAITING_FOR_JOIN
        self.stop_reason: StopReason | None = None
        self.client: AgentSessionClient | None = None
        self.phase_since = time.time()
        self.exit_code = ExitCode.OK
        self._wake = asyncio.Event()
        self._attached = asyncio.Event()
        self._shutdown = False
        self._pending: str | None = None
        self.on_phase: Callable[[FabricPhase], None] | None = None

    # ------------------------------------------------------------- commands
    def request_shutdown(self, exit_code: ExitCode = ExitCode.OK) -> None:
        """Stop the fabric part and end :meth:`run` (signals, tests)."""
        if not self._shutdown:
            log.info("agent.console.shutdown_requested", exit_code=int(exit_code))
        self._shutdown = True
        self.exit_code = exit_code
        self._wake.set()

    def restart_process(self) -> None:
        """End the process with ``OK`` so the service manager starts a new one
        (``Restart=always``) - new supplementary groups need a new process."""
        self.request_shutdown(ExitCode.OK)

    def request(self, command: str) -> None:
        """``"restart"`` | ``"leave"`` | ``"enrolled"`` (a console join just
        finished: start now instead of at the next poll)."""
        if command not in ("restart", "leave", "enrolled"):
            raise ValueError(f"unknown supervisor command {command!r}")
        if command != "enrolled" or self._pending is None:
            self._pending = command
        self._wake.set()

    async def wait_phase(
        self, *phases: FabricPhase, timeout_s: float = 30.0
    ) -> FabricPhase:
        """Tests and the API: wait until the phase is one of ``phases``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.phase not in phases:
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"phase is {self.phase.value}, wanted "
                    f"{', '.join(p.value for p in phases)}"
                )
            await asyncio.sleep(0.02)
        return self.phase

    # ---------------------------------------------------------------- state
    def _set_phase(self, phase: FabricPhase) -> None:
        if phase is self.phase:
            return
        log.info(
            "agent.console.phase",
            old=self.phase.value,
            new=phase.value,
            reason=None if self.stop_reason is None else self.stop_reason.code,
        )
        self.phase = phase
        self.phase_since = time.time()
        if self.on_phase is not None:
            self.on_phase(phase)

    def ready(self) -> bool:
        """Can the fabric part start? Enrolled (or ``insecure-dev``)."""
        if self.settings.security.insecure:
            return True
        try:
            return is_enrolled(self.settings.identity_dir)
        except OSError:
            return False

    def _attach(self, client: AgentSessionClient) -> None:
        self.client = client
        self._attached.set()
        self._set_phase(FabricPhase.RUNNING)

    async def _sleep_or_wake(self, timeout_s: float | None) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout_s)
        self._wake.clear()

    def _take(self) -> str | None:
        command, self._pending = self._pending, None
        return command

    # ------------------------------------------------------------------ run
    async def run(self) -> ExitCode:
        """Until shutdown; returns the process exit code."""
        if self._install_signals:
            install_signal_handlers(self.request_shutdown)
        try:
            while not self._shutdown:
                await self._step()
        finally:
            self._set_phase(FabricPhase.STOPPED if self._shutdown else self.phase)
        return self.exit_code

    async def _step(self) -> None:
        if self.phase is FabricPhase.STOPPED:
            await self._sleep_or_wake(None)
            command = self._take()
            if command == "restart" and self._reload_settings():
                self.stop_reason = None
                self._set_phase(
                    FabricPhase.STARTING
                    if self.ready()
                    else FabricPhase.WAITING_FOR_JOIN
                )
            elif command == "leave":
                await self._leave()
            return
        if not self.ready():
            self._set_phase(FabricPhase.WAITING_FOR_JOIN)
            await self._sleep_or_wake(self._poll_s)
            command = self._take()
            if command == "restart":
                self._reload_settings()
            elif command == "leave":
                await self._leave()
            return
        await self._run_fabric()

    def _reload_settings(self) -> bool:
        """Re-read the configuration (restart); False = it is broken now (the
        old settings stay for the console, the fabric part stays stopped)."""
        if self._reload is None:
            return True
        try:
            self.settings = self._reload()
        except MeshForgeError as exc:
            log.error(  # noqa: TRY400 - a config problem, not a fault
                "agent.console.reload_failed",
                error=exc.message,
                failure_type=exc.failure_type.value,
            )
            self.stop_reason = StopReason(
                "config_reload",
                f"the configuration could not be read: {exc.message}",
                exc.failure_type.value,
            )
            self._set_phase(FabricPhase.STOPPED)
            return False
        return True

    async def _run_fabric(self) -> None:
        self.stop_reason = None
        self.client = None
        self._attached.clear()
        self._set_phase(FabricPhase.STARTING)
        task = asyncio.create_task(
            self._serve(self.settings, install_signals=False, on_client=self._attach),
            name="agent-fabric",
        )
        command: str | None = None
        while not task.done():
            waker = asyncio.create_task(self._wake.wait())
            await asyncio.wait({task, waker}, return_when=asyncio.FIRST_COMPLETED)
            waker.cancel()
            await asyncio.gather(waker, return_exceptions=True)
            if self._wake.is_set():
                self._wake.clear()
                command = self._take()
                if self._shutdown or command in ("restart", "leave"):
                    break
                command = None
        asked = not task.done()
        if asked:
            self._set_phase(FabricPhase.STOPPING)
            await self._stop(task)
        outcome = self._outcome(task, asked=asked)
        self.client = None
        if self._shutdown:
            return
        if command == "restart":
            if self._reload_settings():
                self._set_phase(FabricPhase.STARTING)
            return
        if command == "leave":
            await self._leave()
            return
        if outcome is not None:
            self.stop_reason = outcome
            self._set_phase(FabricPhase.STOPPED)

    async def _stop(self, task: asyncio.Task[AgentSessionClient]) -> None:
        """Ask the fabric part to finish (Goodbye, jobs stopped); a part still
        starting is waited for until it has a client, then stopped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_WAIT_S
        while not task.done() and self.client is None and loop.time() < deadline:
            attached = asyncio.create_task(self._attached.wait())
            await asyncio.wait(
                {task, attached},
                timeout=deadline - loop.time(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            attached.cancel()
            await asyncio.gather(attached, return_exceptions=True)
        if self.client is not None:
            self.client.request_stop()
        remaining = max(0.1, deadline - loop.time())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            log.warning("agent.console.fabric_stop_timeout", timeout_s=STOP_WAIT_S)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _outcome(
        self, task: asyncio.Task[AgentSessionClient], *, asked: bool
    ) -> StopReason | None:
        """None = stopped because we asked; else why it stopped on its own.
        An unexpected exception ends the process (``FAILURE``)."""
        if task.cancelled():
            return None
        exc = task.exception()
        if exc is None:
            if asked or self._shutdown:
                return None
            return StopReason("ended", "the fabric connection ended")
        if isinstance(exc, MeshForgeError):
            reason = _reason_for(exc)
            if isinstance(exc, NodeRevokedError):
                log.error(
                    "agent.revoked",
                    error=str(exc),
                    failure_type=exc.failure_type.value,
                    node_id=exc.details.get("node_id"),
                )
            else:
                log.error(
                    "agent.exited_with_error",
                    error=str(exc),
                    failure_type=exc.failure_type.value,
                    exit_code=int(exc.exit_code),
                    details=exc.details,
                    exc_info=exc,
                )
            return reason
        log.error("agent.crashed", cause=type(exc).__name__, exc_info=exc)
        self.request_shutdown(ExitCode.FAILURE)
        return None

    async def _leave(self) -> None:
        """Forget the fabric: the identity (key, certificate) is deleted through
        the same code path as ``identity reset``; the next join mints a new
        node. The console (and its session) stays."""
        directory = self.settings.identity_dir
        try:
            await asyncio.to_thread(
                reset_identity,
                directory,
                lock_timeout_s=self.settings.agent.identity_lock_timeout_s,
            )
        except MeshForgeError as exc:
            log.error(  # noqa: TRY400 - typed, explained in the console
                "agent.console.leave_failed",
                error=exc.message,
                failure_type=exc.failure_type.value,
            )
            self.stop_reason = StopReason(
                "identity",
                f"leaving failed: {exc.message}",
                exc.failure_type.value,
            )
            self._set_phase(FabricPhase.STOPPED)
            return
        with contextlib.suppress(OSError):
            clear_setup(self.settings)
        log.info("agent.console.left_fabric", identity_dir=str(directory))
        self.stop_reason = None
        self._set_phase(FabricPhase.WAITING_FOR_JOIN)
