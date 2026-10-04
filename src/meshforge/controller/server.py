"""The controller process: one asyncio loop hosting gRPC + REST (ARCHITECTURE §3, D6).

:class:`ControllerRuntime` wires everything and owns every task it starts::

    settings -> engine -> migrations (thread) -> start-up sweep
             -> SessionManager + LeaseTimer task
             -> ResourceTracker + SnapshotFlusher task      (roadmap 0.3.11)
             -> Dispatcher task (queued jobs -> attempts)   (V0.4 G4, D65)
             -> grpc.aio server (AgentControl)         on controller.grpc_bind
             -> uvicorn (FastAPI: /healthz + read-only /v1) on controller.http_bind
             -> mDNS advertisement of the gRPC port    if controller.discovery.advertise

Shutdown (SIGINT/SIGTERM or :meth:`ControllerRuntime.stop`): stop accepting,
close every live session as ``controller_shutdown`` (their streams end), stop
both servers, drain at most ``shutdown_drain_s`` **in total** (one shared
deadline), dispose the engine.

Once both listeners accept connections the runtime logs **one**
``controller.listening`` event with the *actually bound* addresses
(``grpc="host:port"``, ``http="host:port"``; port ``0`` resolved), before the mDNS
registration. A test or cluster harness binds ``127.0.0.1:0`` and reads the ports
from that line instead of guessing free ones (V0.3 G6, D24: no TOCTOU).

Start-up failures are typed: :class:`~meshforge.common.errors.ConfigError` (bind
rules), :class:`~meshforge.controller.store.errors.DatabaseUnavailableError`,
:class:`~meshforge.controller.store.errors.MigrationError` - each carries its exit
code. Tests bind port ``0`` on both listeners and read :attr:`grpc_port` /
:attr:`http_port`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
from collections.abc import Coroutine, Iterator
from types import TracebackType
from typing import Any

import grpc
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ConfigError, ExitCode, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import BindAddress, parse_bind
from meshforge.common.proto.meshforge.v1 import agent_pb2_grpc
from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.api.job_routes import JobApi
from meshforge.controller.blobs import BlobStore, FetchTokens
from meshforge.controller.discovery import ServiceAdvertiser
from meshforge.controller.fabric_token import (
    FabricTokenVerifier,
    fingerprint,
    load_or_create_token,
)
from meshforge.controller.http import create_http_app
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.peer_signing import (
    PeerTokenIssuer,
    load_or_create_signing_key,
    public_key_fingerprint,
)
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.session.servicer import AgentControlServicer
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.snapshots import SnapshotFlusher
from meshforge.controller.store.engine import check_connection, create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.upload_grants import UploadGrants

__all__ = ["GRPC_SERVER_OPTIONS", "ControllerRuntime", "run_controller"]

log = get_logger(__name__)

#: gRPC keepalive per ADR-0002 Updates §3: a dead TCP path is torn down by the
#: transport, not only by the application lease.
GRPC_SERVER_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.keepalive_time_ms", 10_000),
    ("grpc.keepalive_timeout_ms", 5_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.http2.min_recv_ping_interval_without_data_ms", 5_000),
)


_HTTP_BACKLOG = 2048
"""uvicorn's default ``backlog``; the HTTP socket listens with it before serving."""


class _NoSignalUvicorn(uvicorn.Server):
    """uvicorn's ``serve()`` installs its own SIGINT/SIGTERM handlers and re-raises
    the signal on exit; the runtime owns signals, so that hook is disabled."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class ControllerRuntime:
    """See module docstring. Use ``async with ControllerRuntime.start(settings)``."""

    def __init__(self, settings: ControllerSettings) -> None:
        self.settings = settings
        self._grpc_bind: BindAddress = parse_bind(
            settings.controller.grpc_bind, allow_any=settings.controller.allow_any_bind
        )
        self._http_bind: BindAddress = parse_bind(
            settings.controller.http_bind, allow_any=settings.controller.allow_any_bind
        )
        self.grpc_port: int = 0
        self.http_port: int = 0
        self.manager: SessionManager | None = None
        self.registry: Registry | None = None
        self.resources: ResourceTracker | None = None
        self.flusher: SnapshotFlusher | None = None
        self._engine: AsyncEngine | None = None
        self._grpc_server: grpc.aio.Server | None = None
        self._http_server: _NoSignalUvicorn | None = None
        self._http_sock: socket.socket | None = None
        self._advertiser: ServiceAdvertiser | None = None
        self._lease_timer_task: asyncio.Task[None] | None = None
        self._flusher_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop_requested = asyncio.Event()
        self._stopped = False
        self.token_verifier: FabricTokenVerifier | None = None
        self.blobs: BlobStore | None = None
        self.jobs: ExecutionStore | None = None
        """D49: the single writer of the job tables."""
        self.dispatcher: Dispatcher | None = None
        """D65: queued jobs -> attempts; the one task that writes attempts."""
        self._dispatcher_task: asyncio.Task[None] | None = None
        self.fetch_tokens: FetchTokens | None = None
        """D48: G4 mints per-attempt tokens here and hands them to agents."""
        self.peer_tokens: PeerTokenIssuer | None = None
        """D98: signs peer blob tokens; its public key goes out in HelloAck."""
        self.upload_grants: UploadGrants | None = None
        """D73: per-attempt upload quotas (outputs + final logs)."""
        self.logs: LogHub | None = None
        """D71/D72: live + final attempt logs and their followers."""
        self._logs_task: asyncio.Task[None] | None = None
        self.failed_task: str | None = None
        """Name of the owned task whose crash caused the stop, if any (NB3): a
        controller that stopped because its lease timer died exits ``FAILURE``."""

    # -------------------------------------------------------------- lifecycle

    @classmethod
    def start(cls, settings: ControllerSettings) -> _RuntimeContext:
        return _RuntimeContext(cls(settings))

    async def _start(self) -> None:
        cfg = self.settings.controller
        loop = asyncio.get_running_loop()

        # D44: before anything listens - a controller whose token file cannot be
        # trusted refuses to start (config_error) instead of serving unprotected.
        token_path = self.settings.token_path
        token, created = await asyncio.to_thread(load_or_create_token, token_path)
        self.token_verifier = FabricTokenVerifier(
            token_path, clock=loop.time, initial=token
        )
        log.info(
            "controller.token.ready", created=created, fingerprint=fingerprint(token)
        )
        # D98: the peer-token signing key, same trust rules as the token file.
        peer_key, key_created = await asyncio.to_thread(
            load_or_create_signing_key, self.settings.peer_key_path
        )
        self.peer_tokens = PeerTokenIssuer(peer_key, ttl_s=cfg.jobs.peer_token_ttl_s)
        log.info(
            "controller.peer_key.ready",
            created=key_created,
            fingerprint=public_key_fingerprint(peer_key),
        )
        # D48: the blob store (and a sweep of part files a crash left behind).
        blobs = BlobStore(
            self.settings.blobs_dir,
            max_blob_bytes=cfg.blobs.max_blob_bytes,
            max_concurrent_uploads=cfg.blobs.max_concurrent_uploads,
            upload_wait_s=cfg.blobs.upload_wait_s,
            free_disk_floor_bytes=cfg.blobs.free_disk_floor_mb * 1024 * 1024,
        )
        swept_parts = await asyncio.to_thread(blobs.prepare)
        self.blobs = blobs
        self.fetch_tokens = FetchTokens(
            clock=loop.time, capacity=cfg.jobs.fetch_token_capacity
        )
        self.upload_grants = UploadGrants(
            clock=loop.time, capacity=cfg.jobs.fetch_token_capacity
        )
        log.info("controller.blobs.ready", parts_swept=swept_parts)

        engine = create_engine(self.settings.database)
        self._engine = engine
        await check_connection(engine, self.settings.database)
        # Alembic is synchronous (and runs its own loop inside env.py).
        await asyncio.to_thread(upgrade_to_head, self.settings.database)

        registry = Registry(engine)
        self.registry = registry
        self.jobs = ExecutionStore(engine)
        swept = await registry.sweep_open_sessions(SessionEndReason.CONTROLLER_RESTART)
        offline = await registry.mark_live_nodes_offline()
        log.info(
            "controller.startup_sweep", sessions_closed=swept, nodes_offline=offline
        )

        resources = ResourceTracker(
            live_ring_size=cfg.resources.live_ring_size,
            bucket_s=cfg.resources.bucket_s,
            bucket_grace_s=cfg.resources.bucket_grace_s,
            max_tracked_nodes=cfg.resources.max_tracked_nodes,
            max_cursor_nodes=cfg.resources.max_cursor_nodes,
            max_open_buckets=cfg.resources.max_open_buckets,
        )
        self.resources = resources

        manager = SessionManager(
            registry,
            cfg.liveness.to_config(),
            clock=loop.time,
            accepted_protocol_versions=cfg.accepted_protocol_versions,
            outbound_queue_size=cfg.outbound_queue_size,
            dedupe_window=cfg.dedupe_window,
            resources=resources,
            admission_timeout_s=cfg.admission_timeout_s,
        )
        self.manager = manager
        self._lease_timer_task = self._spawn(
            LeaseTimer(manager, clock=loop.time).run(), "lease-timer"
        )
        # D71: attempt logs - one writer task; the log_chunk handler registers
        # before the gRPC server accepts a stream.
        log_hub = LogHub(
            self.settings.logs_dir,
            blobs=blobs,
            max_bytes=cfg.jobs.max_log_bytes,
            queue_chunks=cfg.jobs.log_queue_chunks,
            subscriber_queue=cfg.jobs.log_subscriber_queue,
            max_subscribers=cfg.jobs.max_log_subscribers,
        )
        await asyncio.to_thread(log_hub.prepare)
        manager.dispatch.register("log_chunk", log_hub.on_chunk)
        self.logs = log_hub
        self._logs_task = self._spawn(log_hub.run(), "log-writer")
        # Before the gRPC server accepts a stream: the dispatcher registers the
        # attempt_status handler and the session hooks (D64-D66).
        self.dispatcher = Dispatcher(
            self.jobs,
            manager,
            self.fetch_tokens,
            cfg.jobs,
            upload_grants=self.upload_grants,
            logs=log_hub,
            clock=loop.time,
            blob_port=lambda: self.http_port,
            blob_url=cfg.api.advertise_url,
            resources=resources,
        )
        self._dispatcher_task = self._spawn(self.dispatcher.run(), "dispatcher")
        flusher = SnapshotFlusher(resources, registry, cfg.resources, clock=loop.time)
        self.flusher = flusher
        self._flusher_task = self._spawn(flusher.run(), "snapshot-flusher")

        grpc_server = grpc.aio.server(options=list(GRPC_SERVER_OPTIONS))
        agent_pb2_grpc.add_AgentControlServicer_to_server(
            AgentControlServicer(manager, hello_timeout_s=cfg.hello_timeout_s),
            grpc_server,
        )
        port = grpc_server.add_insecure_port(self._grpc_bind.grpc_target())
        if port == 0:
            raise ConfigError(
                f"cannot bind gRPC listener on {self._grpc_bind.grpc_target()}",
                details={"bind": self._grpc_bind.grpc_target()},
            )
        self.grpc_port = port
        await grpc_server.start()
        self._grpc_server = grpc_server

        self._http_sock = _bind_socket(self._http_bind)
        # Listening now, not when uvicorn's task first runs: connections made as
        # soon as ``controller.listening`` is logged wait in the backlog instead
        # of being refused. (asyncio's create_server calls listen() again; that
        # is harmless.)
        self._http_sock.listen(_HTTP_BACKLOG)
        self.http_port = int(self._http_sock.getsockname()[1])
        http_config = uvicorn.Config(
            create_http_app(
                manager,
                registry=registry,
                resources=resources,
                # The API reads sample ages against the same monotonic clock the
                # session manager and the leases use (D16).
                clock=loop.time,
                auth=ApiAuth(
                    verifier=self.token_verifier,
                    fetch_tokens=self.fetch_tokens,
                    upload_grants=self.upload_grants,
                ),
                blobs=blobs,
                jobs=JobApi(
                    store=self.jobs,
                    blobs=blobs,
                    max_request_bytes=cfg.jobs.max_request_bytes,
                    dispatcher=self.dispatcher,
                    logs=log_hub,
                ),
            ),
            host=self._http_bind.host,
            port=self.http_port,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=int(cfg.shutdown_drain_s) or 1,
        )
        self._http_server = _NoSignalUvicorn(http_config)
        self._spawn(self._http_server.serve(sockets=[self._http_sock]), "http-server")
        log.info(
            "controller.listening",
            grpc=BindAddress((self._grpc_bind.host, self.grpc_port)).grpc_target(),
            http=BindAddress((self._http_bind.host, self.http_port)).grpc_target(),
        )

        if cfg.discovery.advertise:
            # Roadmap 0.3.9: agents with an empty controller_address browse for this.
            self._advertiser = ServiceAdvertiser(
                service_type=cfg.discovery.service_type,
                host=self._grpc_bind.host,
                port=self.grpc_port,
            )
            await self._advertiser.start()

        log.info(
            "controller.started",
            grpc_bind=f"{self._grpc_bind.host}:{self.grpc_port}",
            http_bind=f"{self._http_bind.host}:{self.http_port}",
            heartbeat_interval_s=cfg.liveness.heartbeat_interval_s,
            lease_s=cfg.liveness.to_config().offline_after_s,
        )

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "controller.task_failed",
                task=task.get_name(),
                cause=type(exc).__name__,
                error=str(exc),
            )
            # A dead lease timer or HTTP server is not a running controller.
            if self.failed_task is None:
                self.failed_task = task.get_name()
            self.request_stop()

    # ---------------------------------------------------------------- signals

    def install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM -> graceful stop. Works on Windows via ``signal.signal``."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):  # Windows / non-main thread
                signal.signal(
                    sig, lambda *_: loop.call_soon_threadsafe(self.request_stop)
                )

    def request_stop(self) -> None:
        if not self._stop_requested.is_set():
            log.info("controller.stop_requested")
            self._stop_requested.set()

    async def wait_for_stop(self) -> None:
        await self._stop_requested.wait()

    @property
    def exit_code(self) -> ExitCode:
        """``OK`` for a requested stop; ``FAILURE`` if an owned task crashed."""
        return ExitCode.OK if self.failed_task is None else ExitCode.FAILURE

    # --------------------------------------------------------------- shutdown

    async def stop(self) -> None:
        """Graceful shutdown; idempotent; bounded by ``shutdown_drain_s`` **in total**.

        One deadline (``loop.time() + shutdown_drain_s``) is shared by every step
        below - the session manager's write chains, the gRPC server's grace and
        the owned tasks - so a stop never takes 3 x the budget when each step in
        turn uses all of it.
        """
        if self._stopped:
            return
        self._stopped = True
        loop = asyncio.get_running_loop()
        drain = self.settings.controller.shutdown_drain_s
        deadline = loop.time() + drain

        def remaining() -> float:
            return max(deadline - loop.time(), 0.0)

        log.info("controller.stopping", drain_s=drain)
        if self._advertiser is not None:
            # Withdraw the record first so agents stop finding a dying controller.
            await self._advertiser.stop()
        if self.dispatcher is not None:
            # Ask, do not wait: it finishes its current step (never a query
            # cancelled mid-flight - that leaves driver tasks behind) and is joined
            # with the other owned tasks below, inside the same drain deadline.
            # Items still queued are reports not yet acked: the agents replay them.
            self.dispatcher.request_stop()
        if self.manager is not None:
            # Sessions first: their streams end with the reason persisted before the
            # transport is torn down under them.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.manager.shutdown(drain_s=remaining()), remaining()
                )
        if self._logs_task is not None and not self._logs_task.done():
            self._logs_task.cancel()  # forever by design; nothing left to write
            await asyncio.gather(self._logs_task, return_exceptions=True)
        if self._lease_timer_task is not None and not self._lease_timer_task.done():
            # Runs forever by design; nothing to drain once the sessions are closed.
            self._lease_timer_task.cancel()
            await asyncio.gather(self._lease_timer_task, return_exceptions=True)
        if self._flusher_task is not None and not self._flusher_task.done():
            # Stop the periodic loop first, then write the closed buckets once -
            # inside the shared deadline, so history never delays the drain.
            self._flusher_task.cancel()
            await asyncio.gather(self._flusher_task, return_exceptions=True)
        if self.flusher is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.flusher.final_flush(), remaining())
        if self._grpc_server is not None:
            await self._grpc_server.stop(grace=remaining())
        if self._http_server is not None:
            self._http_server.should_exit = True
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=remaining())
            for task in still:  # anything still running after the drain is late
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
        if self._http_sock is not None:
            self._http_sock.close()
        if self._engine is not None:
            await self._engine.dispose()
        log.info("controller.stopped", took_s=round(drain - remaining(), 3))


class _RuntimeContext:
    """``async with ControllerRuntime.start(settings) as runtime``."""

    def __init__(self, runtime: ControllerRuntime) -> None:
        self._runtime = runtime

    async def __aenter__(self) -> ControllerRuntime:
        try:
            await self._runtime._start()
        except BaseException:
            await self._runtime.stop()
            raise
        return self._runtime

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._runtime.stop()


def _bind_socket(bind: BindAddress) -> socket.socket:
    family = socket.AF_INET6 if ":" in bind.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind.host, bind.port))
        sock.set_inheritable(True)
    except OSError as exc:
        sock.close()
        raise ConfigError(
            f"cannot bind HTTP listener on {bind.host}:{bind.port}: {exc.strerror or exc}",
            details={"bind": f"{bind.host}:{bind.port}"},
        ) from exc
    return sock


async def _serve(settings: ControllerSettings) -> int:
    runtime = ControllerRuntime(settings)
    # Before start-up, not after: start-up awaits migrations and the mDNS
    # registration (~1 s), and a SIGTERM in that window must still be a graceful
    # stop, not the default "killed by signal" exit (G3 finding).
    runtime.install_signal_handlers()
    async with _RuntimeContext(runtime):
        await runtime.wait_for_stop()
    if runtime.failed_task is not None:
        log.error("controller.exited_after_task_failure", task=runtime.failed_task)
    return int(runtime.exit_code)


def run_controller(settings: ControllerSettings) -> int:
    """Blocking entry used by ``meshforge-controller run``; returns the exit code."""
    try:
        return asyncio.run(_serve(settings))
    except MeshForgeError as exc:
        log.exception(
            "controller.start_failed",
            error=str(exc),
            failure_type=exc.failure_type.value,
            details=exc.details,
        )
        return int(exc.exit_code)
