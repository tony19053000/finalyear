"""The agent process (roadmap 0.3.8 E): identity -> session client -> signals -> exit code.

:func:`run_agent` is the blocking entry used by ``meshforge-agent run``::

    settings -> identity.load_or_create (thread) -> hardware.discover (thread)
             -> AgentSessionClient.run() + ResourceMonitor.run()
             -> SIGINT/SIGTERM/SIGBREAK -> client.request_stop() -> exit 0

Hardware discovery (roadmap 0.3.3) runs once, before the first Hello, on a
:class:`~meshforge.agent.hardware.deadline.DeadlineThread` bounded by
``agent.hardware.timeout_s`` plus :data:`DISCOVERY_GRACE_S`. The deadline is
*outside* the probes on purpose: their own budget cannot interrupt a wedged NVML
call, and an agent that never reaches its first Hello is a node that never appears
at all. On expiry (or any failure) the agent still enrols, with
:func:`~meshforge.agent.session.hello.basic_hardware` as the documented fallback -
a node that cannot describe its GPUs is still a node - and the abandoned thread is
a daemon, so it cannot hold up process exit either.

Hardware scores (roadmap 0.3.4) are the third task: :func:`start_scores` runs
:class:`~meshforge.agent.benchmarks.service.ScoreService`, which waits for the first
ONLINE, scores once in a child process if ``<data_dir>/scores.json`` is invalid, and
then polls the cache. It is cancelled first on the way out (its child is killed).
A finished run goes to the controller through ``send_report("hardware_scores")``
(:func:`~meshforge.agent.benchmarks.service.session_reporter`), and the cache that
is already valid at start-up rides in the first ``Hello`` (D37) - read here, in a
thread, before connecting, whether or not background scoring is enabled (a run
made with ``meshforge-agent hardware score`` is still this machine's truth). With
scoring enabled the Hello also carries the ``hardware-scores`` capability tag, which
tells the controller "pending" from "this node does not score" (``none``).

The resource monitor (roadmap 0.3.5) is the second task in this process. It is
referenced, stopped and awaited before ``serve()`` returns, and its NVML session is
shared with discovery so the driver is initialised once. That sharing is why both
use the **same** deadline thread: NVML is not thread-safe here, and one wedged call
must leave at most one abandoned thread, not one per sample.

Signals are Windows-safe: ``loop.add_signal_handler`` where it exists, else
``signal.signal`` + ``call_soon_threadsafe`` (ADR-0009: the agent runs natively on
Windows). On Windows ``SIGBREAK`` (CTRL_BREAK_EVENT, which a supervisor sends
with ``GenerateConsoleCtrlEvent`` to stop a console process group gracefully) is a
stop too; unhandled, it would kill the agent without the half-close. On stop,
running attempts are stopped and report ``node_lost/agent_shutdown`` (V0.4 D52),
then the agent says ``Goodbye`` (D40), half-closes and exits 0.

The executor (V0.4 G4, :mod:`meshforge.agent.execution.executor`) is wired into
the session client here: commands before their Ack, held attempts into every
``Reconcile``, report Acks, and the pre-Goodbye stop.

Exit codes: ``OK`` after a requested stop; the error's own code for a typed failure
(:class:`~meshforge.common.errors.IdentityError` / ``ConfigError`` ->
``CONFIG_ERROR``; :class:`~meshforge.agent.session.errors.SupersededError` ->
``CONFLICT``); ``FAILURE`` for anything else, which is logged with its traceback.
"""

from __future__ import annotations

import asyncio
import functools
import signal
from collections.abc import Callable
from typing import Final

from meshforge.agent.benchmarks.service import (
    ScoreService,
    session_reporter,
    valid_cached_scores,
)
from meshforge.agent.benchmarks.wire import to_proto
from meshforge.agent.cache import CacheInventory, CacheReporter, scan_envs
from meshforge.agent.cache.images import (
    list_env_image_hashes,
    list_env_images,
    remove_env_image,
)
from meshforge.agent.cas import AgentCas
from meshforge.agent.envs.node_prepare import PrepareService
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import build_execution
from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.agent.gc.service import GcService
from meshforge.agent.hardware import (
    HardwareFacts,
    HardwareProvider,
    NvmlSession,
    clip_capabilities,
)
from meshforge.agent.hardware.deadline import (
    BlockingCallError,
    BlockingCallTimeoutError,
    DeadlineThread,
)
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.probe import ProbeService
from meshforge.agent.resources import ResourceMonitor, ResourceSampler
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings, parse_controller_address
from meshforge.common.capabilities import HARDWARE_SCORES_CAPABILITY
from meshforge.common.errors import ExitCode, MeshForgeError
from meshforge.common.gc_index import GcIndex, GcIndexBusyError
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "DISCOVERY_GRACE_S",
    "discover_hardware",
    "hello_scores",
    "install_signal_handlers",
    "route_commands",
    "run_agent",
    "serve",
    "start_cache",
    "start_gc",
    "start_peer",
    "start_scores",
    "stop_signals",
    "stop_task",
]

log = get_logger(__name__)

DISCOVERY_GRACE_S: Final = 1.0
"""Slack over ``agent.hardware.timeout_s`` for thread start-up and the final log."""

_NVML_CLOSE_TIMEOUT_S: Final = 2.0
"""``nvmlShutdown`` is a driver call too; shutdown never waits longer than this."""


def controller_host(target: str) -> str | None:
    """The host part of the ``host:port`` the agent dialled (None if unparsable)."""
    if not target:
        return None
    try:
        return parse_controller_address(target)[0]
    except ValueError:
        return None


def stop_signals() -> tuple[signal.Signals, ...]:
    """The signals that request a graceful stop: SIGINT, SIGTERM and, where the
    platform has it (Windows), SIGBREAK."""
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    return tuple(sigs)


def install_signal_handlers(on_signal: Callable[[], None]) -> None:
    """:func:`stop_signals` -> ``on_signal()`` on the running loop; works on Windows."""
    loop = asyncio.get_running_loop()
    for sig in stop_signals():
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))


async def discover_hardware(
    settings: AgentSettings, node_id: str, nvml: NvmlSession, guard: DeadlineThread
) -> tuple[agent_pb2.HardwareSummary, HardwareFacts | None]:
    """One bounded discovery pass on ``guard``; the Hello summary and the rich facts.

    Never raises: ``discover()`` guards every probe itself, ``guard`` bounds the
    pass as a whole (a probe that cannot be interrupted, i.e. NVML), and the
    last-resort ``except`` covers a bug in this package rather than letting it stop
    a node from enrolling. The fallback summary is G3's :func:`basic_hardware`.
    """
    timeout_s = settings.agent.hardware.timeout_s
    provider = HardwareProvider(timeout_s=timeout_s, nvml=nvml)
    try:
        facts = await guard.call(provider.get, timeout_s=timeout_s + DISCOVERY_GRACE_S)
    except BlockingCallTimeoutError:
        log.warning(
            "agent.hardware.discovery_timeout",
            node_id=node_id,
            failure_type="timeout",
            timeout_s=timeout_s,
            grace_s=DISCOVERY_GRACE_S,
            reason="a probe did not return inside the budget (NVML cannot be killed)",
        )
        return basic_hardware(), None
    except Exception as exc:  # noqa: BLE001 - enrolment must survive a broken probe
        log.warning(
            "agent.hardware.discovery_failed",
            node_id=node_id,
            failure_type="error",
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


def route_commands(
    executor: Executor,
    *,
    gc: GcService | None = None,
    prepare: Callable[[agent_pb2.PrepareEnv], None] | None = None,
    probe: Callable[[agent_pb2.ProbeRequest], None] | None = None,
) -> Callable[[agent_pb2.ControllerMessage], None]:
    """One synchronous command handler: attempts to the executor, ``GcRequest``
    to GC (V0.5 G5), ``PrepareEnv`` to the prepare service (0.5.8),
    ``ProbeRequest`` to the network prober (V0.6 G4, D111). A command for a
    part that is off is logged and dropped (it is still acked)."""

    def handle(msg: agent_pb2.ControllerMessage) -> None:
        which = msg.WhichOneof("payload")
        if which == "gc":
            if gc is not None:
                gc.handle(msg.gc)
            else:
                log.info("agent.gc.request_ignored", reason="gc_disabled")
        elif which == "prepare_env":
            if prepare is not None:
                prepare(msg.prepare_env)
            else:
                log.info("agent.prepare.request_ignored", reason="prepare_unavailable")
        elif which == "probe_request":
            if probe is not None:
                probe(msg.probe_request)
            else:
                log.info("agent.probe.request_ignored", reason="probe_unavailable")
        else:
            executor.handle(msg)

    return handle


async def start_gc(
    settings: AgentSettings,
    cas: AgentCas,
    *,
    node_id: str,
    docker: bool = False,
    cache: CacheReporter | None = None,
) -> GcService | None:
    """The GC service (V0.5 G5, D91) when ``[agent.gc] enabled``: index opened,
    a pass at start, then every ``interval_s``; the CAS observer keeps last-use
    rows; a pass that removed envs makes the cache reporter re-list them.
    None (GC off for this process) also when the index is unusable while
    another live process has it open (:class:`GcIndexBusyError`)."""
    cfg = settings.agent.gc
    if not cfg.enabled:
        log.info("agent.gc.disabled", node_id=node_id)
        return None
    await cas.prepare()
    collector = GarbageCollector(
        GcIndex(settings.gc_dir),
        cas.store,
        GcConfig(
            cas_bytes=cfg.cas_bytes,
            envs_bytes=cfg.envs_bytes,
            min_age_s=cfg.min_age_s,
            batch=cfg.batch,
        ),
        envs_root=settings.envs_dir,
        list_images=list_env_image_hashes if docker else None,
        remove_image=remove_env_image if docker else None,
    )
    service = GcService(
        collector,
        interval_s=cfg.interval_s,
        node_id=node_id,
        on_envs_changed=cache.rescan_now if cache is not None else None,
    )
    try:
        await service.start()
    except GcIndexBusyError as exc:
        # Another live process has the (unusable) index open: never evict
        # under it. GC is off in this process; the next start retries.
        log.warning(
            "agent.gc.unavailable",
            node_id=node_id,
            reason="index_in_use",
            error=str(exc),
        )
        return None
    cas.add_observer(service.on_cas_change)
    return service


def wire_executor(client: AgentSessionClient, executor: Executor) -> None:
    """Connect the executor to the session client's hooks.

    D45/D52/D66: every StartAttempt reaches the executor before its Ack (a
    disabled agent answers FAILED(unauthorized)); Reconcile lists what it holds;
    a requested stop lets running attempts report agent_shutdown, then Goodbye.
    D75: every Reconcile is followed by a re-send of held final reports.
    """
    client.command_handler = executor.handle
    client.held_attempts = executor.held
    client.on_report_acked = executor.report_acked
    client.before_goodbye = executor.shutdown
    client.after_reconcile = executor.after_reconcile


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
    guard = DeadlineThread("hardware")
    hardware, facts = await discover_hardware(settings, node_id, nvml, guard)
    if settings.agent.scores.enabled and facts is not None:
        hardware.capabilities[:] = clip_capabilities(
            [*hardware.capabilities, HARDWARE_SCORES_CAPABILITY]
        )
    # D45: the exec* tags only when the owner enabled execution and a runtime
    # was found; the controller never dispatches to a node without ``exec``.
    execution = await build_execution(
        settings, node_id=node_id, host_has_nvidia_gpu=bool(hardware.gpus)
    )
    if execution.offer.capabilities:
        hardware.capabilities[:] = clip_capabilities(
            [*hardware.capabilities, *execution.offer.capabilities]
        )
    client = AgentSessionClient(
        settings,
        identity,
        hardware=hardware,
        hardware_scores=await hello_scores(settings, node_id, facts),
    )
    cas = AgentCas.at(settings.data_dir)
    peer = await start_peer(settings, cas, client)
    cache = await start_cache(
        settings, cas, client, docker=execution.docker is not None
    )
    gc = await start_gc(
        settings,
        cas,
        node_id=node_id,
        docker=execution.docker is not None,
        cache=cache,
    )
    executor = Executor(
        settings,
        execution,
        cas=cas,
        node_id=node_id,
        report=client.send_report,
        resend=client.resend_report,
        log_sink=client.send_log,
        controller_host=lambda: controller_host(client.controller_target),
        declared_gpus=[gpu.index for gpu in hardware.gpus],
        gc=gc,
    )
    wire_executor(client, executor)
    # 0.5.8 controller part: `compute node prepare` (only where jobs may run).
    preparer = (
        PrepareService(settings, execution, client.send_report, node_id=node_id)
        if execution.enabled
        else None
    )
    # V0.6 G4 (D111): measure links when the controller asks.
    prober = ProbeService(settings.agent.probe, client.send_report, node_id=node_id)
    client.command_handler = route_commands(
        executor,
        gc=gc,
        prepare=preparer.handle if preparer is not None else None,
        probe=prober.handle,
    )
    if execution.enabled:
        await executor.start()
    install_signal_handlers(client.request_stop)
    log.info(
        "agent.started",
        node_id=client.node_id,
        agent_version=settings.agent.agent_version,
        controller_address=settings.agent.controller_address or None,
        discovery=settings.agent.uses_discovery,
    )
    monitor = _build_monitor(settings, client, facts, nvml, guard)
    monitor_task: asyncio.Task[None] | None = None
    if monitor is None:
        await _close_nvml(nvml, guard)
    else:
        monitor_task = asyncio.create_task(
            monitor.run(), name=f"agent-resources:{node_id}"
        )
        monitor_task.add_done_callback(_on_monitor_done)
    scores_task = start_scores(settings, client, facts)
    try:
        await client.run()
    finally:
        # Attempts first: their runtimes stop their containers/process trees.
        await executor.close()
        if peer is not None:
            await peer.close()
        if preparer is not None:
            await preparer.close()
        await prober.close()
        if gc is not None:
            await gc.close()
        if cache is not None:
            await cache.close()
        # Cancelling the scoring task kills its benchmark child tree (runner).
        await stop_task(scores_task)
        if monitor is not None and monitor_task is not None:
            monitor.request_stop()
            # The monitor's own loop closes the sampler (and the NVML session) in
            # its finally block; gather so a crash in it is reported, never swallowed.
            await asyncio.gather(monitor_task, return_exceptions=True)
    return client


async def start_peer(
    settings: AgentSettings, cas: AgentCas, client: AgentSessionClient
) -> PeerBlobServer | None:
    """Start the peer blob server (V0.5 D97) when ``[agent.peer] bind`` is set.

    Wires the session hooks: every accepted HelloAck installs the controller's
    peer-token key (D98, current session only) and every Hello advertises the
    endpoint (D99). A server that cannot start (port taken) is logged and the
    node runs without it - peers are an optimisation, the controller still
    serves every blob.
    """
    cfg = settings.agent.peer
    if cfg.bind is None:
        log.info("agent.peer.disabled", node_id=client.node_id)
        return None
    authorizer = PeerAuthorizer(
        cas, node_id=client.node_id, cache_entries=cfg.tree_cache_entries
    )
    server = PeerBlobServer(cas, authorizer, cfg, node_id=client.node_id)
    try:
        await cas.prepare()
        await server.start()
    except OSError as exc:
        log.error(  # noqa: TRY400 - typed reason, no traceback needed
            "agent.peer.start_failed",
            node_id=client.node_id,
            failure_type="config_error",
            bind=cfg.bind,
            port=cfg.port,
            error=exc.strerror or type(exc).__name__,
        )
        return None

    def on_ack(ack: agent_pb2.HelloAck) -> None:
        if not authorizer.set_key(ack.peer_token_key):
            log.warning(
                "agent.peer.no_token_key",
                node_id=client.node_id,
                session_id=ack.session_id,
            )

    client.on_hello_ack = on_ack
    client.peer_endpoint = lambda: server.endpoint(
        controller_host(client.controller_target)
    )
    return server


async def start_cache(
    settings: AgentSettings,
    cas: AgentCas,
    client: AgentSessionClient,
    *,
    docker: bool = False,
) -> CacheReporter | None:
    """The cache inventory + reporter (V0.5 D90/D101), wired to the CAS and the
    session: a snapshot after every Reconcile, deltas after. None when
    ``[agent.cache] enabled`` is off."""
    cfg = settings.agent.cache
    if not cfg.enabled:
        log.info("agent.cache.reports_disabled", node_id=client.node_id)
        return None
    await cas.prepare()
    inventory = CacheInventory(cas.store, max_trees=cfg.max_trees)
    cas.add_observer(inventory.on_cas_change)
    cas.on_tree_complete = inventory.tree_complete
    reporter = CacheReporter(
        inventory,
        client.send_report,
        cfg,
        node_id=client.node_id,
        online=lambda: client.state is AgentState.ONLINE,
        scan_envs=functools.partial(scan_envs, settings.envs_dir),
        list_images=list_env_images if docker else None,
    )
    await reporter.start()
    client.after_reconcile_hooks.append(reporter.request_snapshot)
    return reporter


async def hello_scores(
    settings: AgentSettings,
    node_id: str,
    facts: HardwareFacts | None,
    *,
    backends: Callable[[], dict[str, str | None]] | None = None,
) -> agent_pb2.HardwareScores | None:
    """The cached scores for the first ``Hello`` if valid for this machine (D37).

    Read in a thread (file I/O, ``importlib.metadata``). ``None`` - Hello without
    scores, "pending" on the controller - when there is no valid cache; never
    raises, because a broken cache must not stop a node from enrolling.
    """
    try:
        scores = await asyncio.to_thread(
            valid_cached_scores,
            settings.data_dir,
            facts,
            backends,
            node_id=node_id,
        )
    except Exception as exc:  # noqa: BLE001 - enrolment must survive a broken cache
        log.warning(
            "agent.scores.hello_cache_failed",
            node_id=node_id,
            cause=type(exc).__name__,
        )
        return None
    if scores is None:
        return None
    log.info(
        "agent.scores.hello_cache_valid",
        node_id=node_id,
        run_id=scores.run_id,
        measured_at=scores.measured_at.isoformat(),
    )
    return to_proto(scores)


def start_scores(
    settings: AgentSettings,
    client: AgentSessionClient,
    facts: HardwareFacts | None,
    *,
    service: ScoreService | None = None,
) -> asyncio.Task[None] | None:
    """Start the background hardware-score task (roadmap 0.3.4) unless disabled.

    The task waits for the first ONLINE itself, so starting it here never delays
    connect or Hello. The reference is returned (and kept by :func:`serve`) and a
    done-callback logs a crash - no fire-and-forget task.
    """
    if not settings.agent.scores.enabled:
        log.info("agent.scores.disabled", node_id=client.node_id)
        return None
    service = service or ScoreService(
        settings.agent.scores,
        settings.data_dir,
        client,
        facts,
        report=session_reporter(client),
    )
    task = asyncio.create_task(service.run(), name=f"agent-scores:{client.node_id}")
    task.add_done_callback(_on_scores_done)
    return task


async def stop_task(task: asyncio.Task[None] | None) -> None:
    """Cancel ``task`` and wait for it (its ``finally`` blocks run to the end)."""
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _on_scores_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "agent.scores.task_failed",
            task=task.get_name(),
            cause=type(exc).__name__,
            error=str(exc)[:200],
        )


async def _close_nvml(nvml: NvmlSession, guard: DeadlineThread) -> None:
    """Shut NVML down without ever waiting on it: a wedged driver is left alone."""
    try:
        await guard.call(nvml.close, timeout_s=_NVML_CLOSE_TIMEOUT_S)
    except BlockingCallError as exc:
        log.warning("agent.hardware.nvml_close_skipped", reason=str(exc))


def _build_monitor(
    settings: AgentSettings,
    client: AgentSessionClient,
    facts: HardwareFacts | None,
    nvml: NvmlSession,
    guard: DeadlineThread,
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
    return ResourceMonitor(client, sampler, resources, guard=guard)


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
