"""Look only, act later: the first "Run jobs on" of an agent started with jobs off
(V1.0 G8 F5, D428 - team decision 2026-11-03).

An agent whose owner has ``accept_jobs`` off at start builds a **look-only**
execution setup (:func:`.offer.build_execution` ``side_effects=False``): read-only
checks, no container started, no cgroup written. It advertises what those checks
found, and its executor refuses every attempt.

When the live policy first says ``accept_jobs`` on, :meth:`ExecutionActivator.activate`
runs **once**:

1. :func:`.offer.build_execution` with ``side_effects=True`` - the GPU-probe
   container (when its image is local), the CPU-share cgroup probe (may enable
   the ``cpu`` controller) and its leftover sweep, the unpinned-controller
   warning;
2. the new setup is swapped into the executor and the prepare service
   (``replace_setup``) and the executor's start-up work runs (work root, Docker
   orphan sweep, condition sweep);
3. the live policy recomputes what it enforces (``max_cpu_percent`` is enforced
   once the cgroup exists) - the caller sends that ``PolicyUpdate``;
4. the ``exec*`` tags in ``Hello.hardware.capabilities`` are replaced and the
   session is **handed over** (:meth:`AgentSessionClient.request_handover`, the
   V0.7 G4 certificate hand-over path): the stream ends without a Goodbye and
   reconnects at once, so the controller reads the new capabilities from a
   fresh Hello. Chosen over a new "capabilities update" message: it is an
   existing, tested path the controller already handles (also an older one),
   it keeps the node row's hardware written in one place (Hello admission),
   and nothing is lost - no attempt runs while jobs were off, and attempts the
   controller holds survive a hand-over anyway.

Until it finishes, ``exec-gpu`` may be missing or merely *assumed* (read from the
daemon's runtimes) and the CPU cap is advertised not enforced; the planner refuses
with its usual typed reasons (``no_gpu_runtime``, ``policy_cpu``) and the agent
refuses an attempt that arrives meanwhile as retryable
(``execution_setup_pending``). Turning jobs off and on again later does not
repeat it.

A failed activation (the build or the start-up work raising) keeps the agent
look-only and is retried with a backoff (:data:`RETRY_BASE_S` doubling up to
:data:`MAX_RETRY_S`) on the policy watch's ticks while jobs stay on; the
``PolicyUpdate`` is sent regardless, so the controller knows the owner's choice.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Protocol

from meshforge.agent.execution.offer import ExecutionSetup, build_execution
from meshforge.agent.hardware import clip_capabilities
from meshforge.agent.settings import AgentSettings
from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_SPLIT_CAPABILITY
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["HANDOVER_REASON", "ExecutionActivator", "exec_capabilities"]

log = get_logger(__name__)

HANDOVER_REASON: Final = "capabilities_changed"
RETRY_BASE_S: Final = 2.0
"""First retry after a failed activation; doubles per failure."""
MAX_RETRY_S: Final = 60.0
"""Longest wait between activation retries."""


def exec_capabilities(setup: ExecutionSetup) -> list[str]:
    """The ``exec*`` tags a setup puts into Hello (V0.10 G4: an executing node
    runs Mode 3 lanes too, ``exec-split``)."""
    tags = list(setup.offer.capabilities)
    return [*tags, EXEC_SPLIT_CAPABILITY] if tags else []


def _is_exec_tag(tag: str) -> bool:
    return tag == EXEC_CAPABILITY or tag.startswith(f"{EXEC_CAPABILITY}-")


class _Swappable(Protocol):
    def replace_setup(self, setup: ExecutionSetup) -> None: ...


class ExecutionActivator:
    """See module docstring. One per agent process; one event loop."""

    def __init__(
        self,
        settings: AgentSettings,
        setup: ExecutionSetup,
        *,
        node_id: str,
        host_has_nvidia_gpu: bool,
        hardware: agent_pb2.HardwareSummary,
        targets: Sequence[_Swappable],
        start: Callable[[], Awaitable[None]],
        on_cgroup: Callable[[bool], bool],
        request_handover: Callable[[str], bool],
        build: Callable[..., Awaitable[ExecutionSetup]] = build_execution,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``start``: the executor's start-up work; ``on_cgroup(available)``:
        recompute the live policy's enforcement facts (True = it changed);
        ``build``: :func:`.offer.build_execution` (tests inject one)."""
        self._settings = settings
        self.setup = setup
        self._node_id = node_id
        self._gpu = host_has_nvidia_gpu
        self._hardware = hardware
        self._targets = tuple(targets)
        self._start = start
        self._on_cgroup = on_cgroup
        self._handover = request_handover
        self._build = build
        self._lock = asyncio.Lock()
        """One activation at a time: the policy watch's change callback and its
        retry tick both call :meth:`activate`; the second caller waits, then
        finds the setup complete (or the retry not due) and does nothing."""
        self._clock = clock
        self._retry_at = 0.0
        self.activations = 0
        self.failures = 0

    @property
    def pending(self) -> bool:
        """The setup is look-only: the first enable has not happened yet."""
        return not self.setup.complete

    def due(self) -> bool:
        """A retry after a failed activation may run now (backoff elapsed)."""
        return self.pending and self._clock() >= self._retry_at

    async def activate(self) -> bool:
        """Build the full setup once (module docstring). True when this call
        did it (the live policy may have changed: send it, then hand over).

        A failure - the build raising, or the executor's start-up work failing
        - leaves the agent **look-only** (still :attr:`pending`, every attempt
        refused), logs ``agent.execution.activation_failed`` and arms a retry
        after a backoff (:meth:`due`; the policy watch retries on its ticks
        while jobs stay on - no owner action needed)."""
        async with self._lock:
            if not self.pending:
                return False
            log.info("agent.execution.activating", node_id=self._node_id)
            look_only = self.setup
            try:
                setup = await self._build(
                    self._settings,
                    node_id=self._node_id,
                    host_has_nvidia_gpu=self._gpu,
                    side_effects=True,
                )
                for target in self._targets:
                    target.replace_setup(setup)
                await self._start()
            except Exception as exc:  # noqa: BLE001 - logged, retried, typed reason
                for target in self._targets:
                    target.replace_setup(look_only)  # never run on a half setup
                self.failures += 1
                backoff = min(MAX_RETRY_S, RETRY_BASE_S * 2 ** (self.failures - 1))
                self._retry_at = self._clock() + backoff
                log.error(  # noqa: TRY400 - typed reason; the build never raises by contract
                    "agent.execution.activation_failed",
                    node_id=self._node_id,
                    cause=type(exc).__name__,
                    failures=self.failures,
                    retry_in_s=backoff,
                )
                return False
            self.setup = setup
            self.activations += 1
            self._on_cgroup(setup.cpu_cgroups is not None)
            kept = [t for t in self._hardware.capabilities if not _is_exec_tag(t)]
            self._hardware.capabilities[:] = clip_capabilities(
                [*kept, *exec_capabilities(setup)]
            )
            log.info(
                "agent.execution.activated",
                node_id=self._node_id,
                capabilities=list(self._hardware.capabilities),
                cpu_cgroup=setup.cpu_cgroups is not None,
            )
            return True

    def readvertise(self) -> bool:
        """Hand the session over so a fresh Hello carries the new capabilities
        (False = no stream now: the next connection carries them anyway)."""
        return self._handover(HANDOVER_REASON)
