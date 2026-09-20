"""The agent process (roadmap 0.3.8 E): identity -> session client -> signals -> exit code.

:func:`run_agent` is the blocking entry used by ``meshforge-agent run``::

    settings -> identity.load_or_create (thread) -> hardware.discover (thread)
             -> AgentSessionClient.run() + ResourceMonitor.run()
             -> SIGINT/SIGTERM -> client.request_stop() -> exit 0

Hardware discovery (roadmap 0.3.3) runs once, before the first Hello, in a worker
thread and bounded by ``agent.hardware.timeout_s``; its summary is what the Hello
carries. If it fails outright the agent still enrols, with
:func:`~meshforge.agent.session.hello.basic_hardware` as the documented fallback -
a node that cannot describe its GPUs is still a node.

The resource monitor (roadmap 0.3.5) is the second task in this process. It is
referenced, stopped and awaited before ``serve()`` returns, and its NVML session is
shared with discovery so the driver is initialised once.

Signals are Windows-safe: ``loop.add_signal_handler`` where it exists, else
``signal.signal`` + ``call_soon_threadsafe`` (ADR-0009: the agent runs natively on
Windows). On stop the agent half-closes its stream and exits 0; the controller
records ``agent_closed`` from the stream end - no farewell message is needed.

Exit codes: ``OK`` after a requested stop; the error's own code for a typed failure
(:class:`~meshforge.common.errors.IdentityError` / ``ConfigError`` ->
``CONFIG_ERROR``; :class:`~meshforge.agent.session.errors.SupersededError` ->
``CONFLICT``); ``FAILURE`` for anything else, which is logged with its traceback.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from meshforge.agent.hardware import HardwareFacts, HardwareProvider, NvmlSession
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.resources import ResourceMonitor, ResourceSampler
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.settings import AgentSettings
from meshforge.common.errors import ExitCode, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "discover_hardware",
    "install_signal_handlers",
    "run_agent",
    "serve",
]

log = get_logger(__name__)


def install_signal_handlers(on_signal: Callable[[], None]) -> None:
    """SIGINT/SIGTERM -> ``on_signal()`` on the running loop; works on Windows too."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))


async def discover_hardware(
    settings: AgentSettings, node_id: str, nvml: NvmlSession
) -> tuple[agent_pb2.HardwareSummary, HardwareFacts | None]:
    """One bounded discovery pass in a thread; the Hello summary and the rich facts.

    Never raises: ``discover()`` guards every probe itself, and the last-resort
    ``except`` here covers a bug in this package rather than letting it stop a node
    from enrolling. The fallback summary is G3's :func:`basic_hardware`.
    """
    provider = HardwareProvider(timeout_s=settings.agent.hardware.timeout_s, nvml=nvml)
    try:
        facts = await asyncio.to_thread(provider.get)
    except Exception as exc:  # noqa: BLE001 - enrolment must survive a broken probe
        log.warning(
            "agent.hardware.discovery_failed",
            node_id=node_id,
            cause=type(exc).__name__,
            error=str(exc)[:200],
        )
        return basic_hardware(), None
    summary = facts.to_summary()
    log.info(
        "agent.hardware.ready",
        node_id=node_id,
        cpu_cores=summary.cpu_cores,
        cpu_threads=summary.cpu_threads,
        ram_bytes=summary.ram_bytes,
        gpu_count=len(summary.gpus),
        capabilities=list(summary.capabilities),
        degraded=list(facts.degraded),
    )
    return summary, facts


async def serve(
    settings: AgentSettings, *, identity: NodeIdentity | None = None
) -> AgentSessionClient:
    """Run the agent until a signal stops it; returns the finished client.

    Raises the typed error that stopped it, if any (see :func:`run_agent`).
    """
    if identity is None:
        identity = await asyncio.to_thread(
            load_or_create,
            settings.identity_dir,
            lock_timeout_s=settings.agent.identity_lock_timeout_s,
        )
    node_id = str(identity.node_id)
    nvml = NvmlSession()
    hardware, facts = await discover_hardware(settings, node_id, nvml)
    client = AgentSessionClient(settings, identity, hardware=hardware)
    install_signal_handlers(client.request_stop)
    log.info(
        "agent.started",
        node_id=client.node_id,
        agent_version=settings.agent.agent_version,
        controller_address=settings.agent.controller_address or None,
        discovery=settings.agent.uses_discovery,
    )
    monitor = _build_monitor(settings, client, facts, nvml)
    if monitor is None:
        await asyncio.to_thread(nvml.close)
        await client.run()
        return client
    monitor_task = asyncio.create_task(monitor.run(), name=f"agent-resources:{node_id}")
    monitor_task.add_done_callback(_on_monitor_done)
    try:
        await client.run()
    finally:
        monitor.request_stop()
        # The monitor's own loop closes the sampler (and the NVML session) in its
        # finally block; gather so a crash in it is reported, never swallowed.
        await asyncio.gather(monitor_task, return_exceptions=True)
    return client


def _build_monitor(
    settings: AgentSettings,
    client: AgentSessionClient,
    facts: HardwareFacts | None,
    nvml: NvmlSession,
) -> ResourceMonitor | None:
    """The monitor for this process, or None when ``agent.resources.enabled`` is off."""
    resources = settings.agent.resources
    if not resources.enabled:
        log.info("agent.resources.disabled", node_id=client.node_id)
        return None
    mounts = [disk.mount for disk in facts.disks] if facts is not None else None
    sampler = ResourceSampler(
        mounts=mounts or None,
        nvml=nvml,
        max_gpus=resources.max_gpus,
        max_disks=resources.max_disks,
    )
    return ResourceMonitor(client, sampler, resources)


def _on_monitor_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "agent.resources.task_failed",
            task=task.get_name(),
            cause=type(exc).__name__,
            error=str(exc),
        )


def run_agent(settings: AgentSettings) -> int:
    """Blocking entry used by ``meshforge-agent run``; returns the exit code."""
    try:
        asyncio.run(serve(settings))
    except MeshForgeError as exc:
        log.exception(
            "agent.exited_with_error",
            error=str(exc),
            failure_type=exc.failure_type.value,
            exit_code=int(exc.exit_code),
            details=exc.details,
        )
        return int(exc.exit_code)
    except Exception as exc:  # last line of defence: log with traceback, exit FAILURE
        log.exception("agent.crashed", cause=type(exc).__name__)
        return int(ExitCode.FAILURE)
    log.info("agent.exited", exit_code=int(ExitCode.OK))
    return int(ExitCode.OK)
