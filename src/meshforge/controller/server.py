"""The controller process: one asyncio loop hosting gRPC + REST (ARCHITECTURE §3, D6).

:class:`ControllerRuntime` wires everything and owns every task it starts::

    settings -> fabric CA + server cert (V0.7 G1; only when `fabric create` ran)
             -> engine -> migrations (thread) -> start-up sweep
             -> SessionManager + LeaseTimer task
             -> ResourceTracker + SnapshotFlusher task      (roadmap 0.3.11)
             -> Dispatcher task (queued jobs -> attempts)   (V0.4 G4, D65)
             -> TLS enrolment listener (V0.7 G2, D150)  on [controller.enroll] bind
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
import functools
import signal
import socket
from collections.abc import Coroutine, Iterator
from datetime import timedelta
from types import TracebackType
from typing import Any

import grpc
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.config import INSECURE_DEV_BANNER
from meshforge.common.errors import ConfigError, ExitCode, MeshForgeError
from meshforge.common.gc_index import GcIndexBusyError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import BindAddress, parse_bind
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.api.auth_routes import AuthApi
from meshforge.controller.api.fabric_routes import InviteApi
from meshforge.controller.api.job_routes import JobApi
from meshforge.controller.api.network_routes import NetworkApi
from meshforge.controller.auth.live_streams import LiveStreams
from meshforge.controller.auth.login import LoginService, login_policy
from meshforge.controller.auth.passwords import PasswordHasher
from meshforge.controller.auth.users import UserStore
from meshforge.controller.blobs import BlobStore, FetchTokens
from meshforge.controller.cache_index import CacheIndex, CacheLocationStore
from meshforge.controller.discovery import ServiceAdvertiser
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.enrollment.listener import (
    EnrollListener,
    EnrollmentServicer,
    SourceRateLimiter,
)
from meshforge.controller.enrollment.renewal import CertRenewals
from meshforge.controller.enrollment.service import Enroller
from meshforge.controller.fabric_ca import (
    FabricCa,
    ServerCert,
    ensure_server_cert,
    fabric_present,
    load_fabric,
    recover_interrupted,
)
from meshforge.controller.fabric_token import (
    FabricTokenVerifier,
    fingerprint,
    load_or_create_token,
)
from meshforge.controller.gc import BlobScrubber, ControllerGc
from meshforge.controller.http import create_http_app
from meshforge.controller.http_tls import NodeCertPolicy, peer_cert_protocol
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.env_health import KnownBadEnvs
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.named_inputs import NamedInputStore, PinBroadcaster
from meshforge.controller.network import NetworkProber, NetworkStore
from meshforge.controller.peer_signing import (
    PeerTokenIssuer,
    load_or_create_signing_key,
    public_key_fingerprint,
)
from meshforge.controller.prepare import PrepareTracker
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.revocation import Revocations, RevocationStore
from meshforge.controller.scores import (
    expire_unbound_scores_forever,
    expire_unbound_scores_once,
)
from meshforge.controller.secret_store import SecretStore
from meshforge.controller.server_tls import ServerTls
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.live import LiveSession
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.session.peer_cert import NodeCertInterceptor, peer_cert
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
        self._enroll_bind: BindAddress = parse_bind(
            settings.controller.enroll_bind,
            allow_any=settings.controller.allow_any_bind,
        )
        self.grpc_port: int = 0
        self.http_port: int = 0
        self.enroll_port: int = 0
        """V0.7 G2 (D150): the bound port of the TLS enrolment listener; 0 when
        it is not running (no fabric CA, or ``[controller.enroll] enabled =
        false``)."""
        self.manager: SessionManager | None = None
        self.registry: Registry | None = None
        self.resources: ResourceTracker | None = None
        self.flusher: SnapshotFlusher | None = None
        self._engine: AsyncEngine | None = None
        self._grpc_server: grpc.aio.Server | None = None
        self._enroll_listener: EnrollListener | None = None
        self._http_server: _NoSignalUvicorn | None = None
        self._http_sock: socket.socket | None = None
        self._advertiser: ServiceAdvertiser | None = None
        self._lease_timer_task: asyncio.Task[None] | None = None
        self._flusher_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop_requested = asyncio.Event()
        self._stopped = False
        self.token_verifier: FabricTokenVerifier | None = None
        self.fabric: FabricCa | None = None
        """V0.7 D134: the fabric CA, when `fabric create` has run (None otherwise)."""
        self.server_cert: ServerCert | None = None
        """V0.7 D134: the server cert as issued/renewed at start."""
        self.server_tls: ServerTls | None = None
        """V0.7 G3: the server cert *in use*, renewed while running; every TLS
        listener takes its credentials from here."""
        self.blobs: BlobStore | None = None
        self.jobs: ExecutionStore | None = None
        """D49: the single writer of the job tables."""
        self.dispatcher: Dispatcher | None = None
        self.revocations: Revocations | None = None
        """V0.7 G3 (D138): the revocation set and its enforcement."""
        self.renewals: CertRenewals | None = None
        self.users: UserStore | None = None
        self.secrets: SecretStore | None = None
        self.login: LoginService | None = None
        """V0.7 G4 (D140): node certificate renewal (mtls with a CA only)."""
        self.live_streams: LiveStreams | None = None
        """D157 T-1: the token re-check of open log streams."""
        self._resyncs: set[asyncio.Task[None]] = set()
        self.known_bad_envs: KnownBadEnvs | None = None
        """D65: queued jobs -> attempts; the one task that writes attempts."""
        self._dispatcher_task: asyncio.Task[None] | None = None
        self.fetch_tokens: FetchTokens | None = None
        """D48: G4 mints per-attempt tokens here and hands them to agents."""
        self.cache_index: CacheIndex | None = None
        """D101: cache reports -> ``cache_location`` (peer listing, node detail)."""
        self.peer_tokens: PeerTokenIssuer | None = None
        """D98: signs peer blob tokens; its public key goes out in HelloAck."""
        self.upload_grants: UploadGrants | None = None
        """D73: per-attempt upload quotas (outputs + final logs)."""
        self.logs: LogHub | None = None
        self.inputs: NamedInputStore | None = None
        """V0.5 G5 (D92): named, versioned inputs (``/v1/data``)."""
        self.pins: PinBroadcaster | None = None
        self.prepare: PrepareTracker | None = None
        self.network: NetworkProber | None = None
        """V0.6 G4 (D111): peer probes -> ``network_link``."""
        self.network_store: NetworkStore | None = None
        self.gc: ControllerGc | None = None
        """V0.5 G5 (D91): LRU GC of the blob store under [controller.gc]."""
        self.scrubber: BlobScrubber | None = None
        """V0.5 G5: re-hashes stored blobs (suspects first), removes corrupt ones."""
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
        # D145 (d): never serve a fabric on the published dev database password.
        self.settings.check_database_password()
        if self.settings.security.insecure:
            # D139: every start, loud - this mode must never go unnoticed.
            log.warning(
                "security.insecure_dev",
                component="controller",
                banner=INSECURE_DEV_BANNER,
            )

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
        # V0.7 D134: the fabric CA (if created) and the server cert, renewed at
        # 2/3 life. A CA that exists but cannot be trusted stops the start-up.
        self.fabric, self.server_cert = await asyncio.to_thread(
            _prepare_fabric, self.settings
        )
        if self.fabric is not None and self.server_cert is not None:
            # D149 carry-over: renewed while running; listeners follow it.
            self.server_tls = ServerTls(
                self.fabric,
                self.server_cert,
                hosts=self.settings.server_cert_hosts,
                lifetime=timedelta(days=cfg.tls.server_cert_days),
            )
            self._spawn(
                self.server_tls.run(cfg.tls.check_interval_s), "server-cert-renewer"
            )
        # D48: the blob store (and a sweep of part files a crash left behind).
        blobs = BlobStore(
            self.settings.blobs_dir,
            max_blob_bytes=cfg.blobs.max_blob_bytes,
            max_concurrent_uploads=cfg.blobs.max_concurrent_uploads,
            upload_wait_s=cfg.blobs.upload_wait_s,
            free_disk_floor_bytes=cfg.blobs.free_disk_floor_mb * 1024 * 1024,
            max_resumable_per_owner=cfg.blobs.max_resumable_per_owner,
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

        if cfg.gc.enabled:
            gc = ControllerGc(
                blobs,
                engine,
                index_dir=self.settings.gc_dir,
                quota_bytes=cfg.gc.cas_bytes,
                min_age_s=cfg.gc.min_age_s,
                interval_s=cfg.gc.interval_s,
                batch=cfg.gc.batch,
            )
            try:
                await gc.start()
            except GcIndexBusyError as exc:
                # The index is unusable and another live process has it
                # open: never evict under it; GC is off until a restart.
                log.warning(
                    "controller.gc.unavailable", reason="index_in_use", error=str(exc)
                )
            else:
                self.gc = gc
                blobs.used = gc.touch
            self.scrubber = BlobScrubber(
                blobs,
                bytes_per_s=cfg.gc.scrub_bytes_per_s,
                interval_s=cfg.gc.scrub_interval_s,
            )
            self.scrubber.start()

        registry = Registry(engine)
        self.registry = registry
        self.cache_index = CacheIndex(CacheLocationStore(engine))
        self.jobs = ExecutionStore(engine)
        self.inputs = NamedInputStore(engine)
        # V0.7 G6 (D144): job secrets; the master key is loaded on first use.
        self.secrets = SecretStore(engine, self.settings.secrets_key_path)
        swept = await registry.sweep_open_sessions(SessionEndReason.CONTROLLER_RESTART)
        offline = await registry.mark_live_nodes_offline()
        log.info(
            "controller.startup_sweep", sessions_closed=swept, nodes_offline=offline
        )
        # D161 N3: unbound (pre-G5) score documents expire; once now, then
        # periodically (a node that never re-Hellos keeps none forever).
        await expire_unbound_scores_once(registry, cfg.planner.unbound_scores_max_age_s)
        self._spawn(
            expire_unbound_scores_forever(
                registry,
                max_age_s=cfg.planner.unbound_scores_max_age_s,
                interval_s=cfg.planner.unbound_scores_check_s,
            ),
            "unbound-scores-expiry",
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
            peer_token_key=self.peer_tokens.public_key,
            trust_peer_endpoints=cfg.trust_peer_endpoints,
            command_resend_s=cfg.command_resend_s,
            command_resend_tries=cfg.command_resend_tries,
        )
        self.manager = manager
        # D101: cache reports -> the location index (before any stream).
        manager.dispatch.register("cache_report", self.cache_index.on_message)
        # V0.5 G5 (0.5.8): `compute node prepare` progress reports.
        self.prepare = PrepareTracker(manager)
        manager.dispatch.register("prepare_env_status", self.prepare.on_message)
        # V0.6 G4 (D111): peer probes -> the network matrix.
        network_store = NetworkStore(engine)
        network = NetworkProber(
            manager,
            network_store,
            self.peer_tokens,
            cfg.network,
            clock=loop.time,
            resources=resources,
        )
        self.network_store, self.network = network_store, network
        manager.dispatch.register("net_probe_result", network.on_message)
        if cfg.network.enabled:
            self._spawn(network.run(), "network-prober")
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
        # V0.6 G1 (D116): G2's feasibility asks known_bad_envs.is_bad(node, key).
        self.known_bad_envs = KnownBadEnvs(
            ttl_s=cfg.jobs.env_bad_ttl_s,
            clock=loop.time,
            max_entries=cfg.jobs.known_bad_envs_max,
        )
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
            peers=PeerSources(
                manager,
                self.cache_index.store,
                self.peer_tokens,
                max_sources=cfg.jobs.max_peer_sources,
                index_live=self.cache_index.is_live,
                scheme="http" if self.settings.security.insecure else "https",
            ),
            inputs=self.inputs,
            secrets=self.secrets,
            insecure_secrets=self.settings.security.allow_insecure_secrets,
            known_bad_envs=self.known_bad_envs,
            planner=cfg.planner,
            network=cfg.network,
            cache_live=self.cache_index.is_live,
            on_integrity_failure=(
                functools.partial(
                    self.scrubber.suspect_tree_soon, why="attempt_integrity_error"
                )
                if self.scrubber is not None
                else None
            ),
        )
        self._dispatcher_task = self._spawn(self.dispatcher.run(), "dispatcher")
        # V0.5 G5 (D92): the pinned named inputs go to every agent after its
        # Reconcile (chained behind the dispatcher's hook) and on every change.
        self.pins = PinBroadcaster(self.inputs, manager)
        await self.pins.refresh(broadcast=False)
        dispatcher_reconcile = manager.hooks.on_reconcile
        dispatcher_offline = manager.hooks.on_node_offline
        pins = self.pins
        prepare = self.prepare

        def on_reconcile(live: LiveSession, reconcile: agent_pb2.Reconcile) -> None:
            if dispatcher_reconcile is not None:
                dispatcher_reconcile(live, reconcile)
            pins.on_reconcile(live)
            if prepare is not None:
                prepare.on_reconcile(live)

        def on_node_offline(live: LiveSession, event: LivenessEvent) -> None:
            if dispatcher_offline is not None:
                dispatcher_offline(live, event)
            if prepare is not None:
                prepare.on_node_offline(live, event)
            network.on_node_offline(live, event)

        manager.hooks.on_reconcile = on_reconcile
        manager.hooks.on_node_offline = on_node_offline
        # V0.7 G3 (D138): the revocation set, loaded before any stream is
        # accepted; every HelloAck carries it, the interceptor and the HTTPS
        # policy check against it, a periodic sync picks up CLI revocations.
        revocations = Revocations(RevocationStore(engine), manager)
        await revocations.sync()
        self.revocations = revocations
        manager.revoked_serials = revocations.sent_serials
        manager.refused_serials = revocations.revoked_serials
        self._spawn(revocations.run(cfg.revocation.poll_interval_s), "revocation-sync")
        # V0.7 G4 (D140): node certificate renewal; the first Hello on a renewed
        # certificate makes it current, and the old serial is pushed out at once.
        if self.fabric is not None and not self.settings.security.insecure:
            self.renewals = CertRenewals(
                engine,
                self.fabric,
                min_interval_s=cfg.renewal.min_interval_s,
                lifetime=timedelta(seconds=cfg.renewal.node_cert_lifetime_s),
            )
            manager.activate_cert = self.renewals.activate
            manager.on_cert_activated = self._resync_revocations
        # V0.7 G4 (D141): REST users and API tokens.
        self.users = UserStore(engine)
        self.login = LoginService(
            self.users,
            policy=login_policy(cfg.auth),
            hasher=PasswordHasher(max_concurrent=cfg.auth.max_concurrent_hashes),
            clock=loop.time,
        )
        # D157 T-1: open log streams re-check their token (one query a tick).
        self.live_streams = LiveStreams(
            self.users, interval_s=cfg.auth.stream_recheck_interval_s
        )
        self._spawn(self.live_streams.run(), "stream-auth-recheck")
        if cfg.auth.legacy_fabric_token:
            log.warning(
                "controller.auth.legacy_token_enabled",
                hint="[controller.auth] legacy_fabric_token = true: the fabric "
                "token is accepted as an admin (removed in V0.8)",
            )
        flusher = SnapshotFlusher(resources, registry, cfg.resources, clock=loop.time)
        self.flusher = flusher
        self._flusher_task = self._spawn(flusher.run(), "snapshot-flusher")

        await self._start_enrollment(engine, loop)

        port, grpc_server = self._session_server(manager)
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
                    users=self.users,
                    legacy_fabric_token=cfg.auth.legacy_fabric_token,
                    live_streams=self.live_streams,
                    verifier=self.token_verifier,
                    fetch_tokens=self.fetch_tokens,
                    upload_grants=self.upload_grants,
                    node_certs=(
                        NodeCertPolicy(
                            fabric_id=self.fabric.fabric_id,
                            revoked=revocations.revoked_serials,
                        )
                        if self.fabric is not None and self.server_tls is not None
                        else None
                    ),
                ),
                blobs=blobs,
                cache=self.cache_index.store,
                jobs=JobApi(
                    store=self.jobs,
                    blobs=blobs,
                    max_request_bytes=cfg.jobs.max_request_bytes,
                    dispatcher=self.dispatcher,
                    logs=log_hub,
                    inputs=self.inputs,
                    pins=self.pins,
                    prepare=self.prepare,
                    secrets=self.secrets,
                    max_job_secret_bytes=cfg.jobs.max_job_secret_bytes,
                ),
                network=NetworkApi(store=network_store, prober=network),
                invites=InviteApi(
                    store=InviteStore(engine),
                    settings=self.settings,
                    fabric=lambda: self.fabric,
                    enroll_port=lambda: self.enroll_port,
                ),
                revocations=revocations,
                users=AuthApi(users=self.users, login=self.login),
                secrets=self.secrets,
            ),
            host=self._http_bind.host,
            port=self.http_port,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=int(cfg.shutdown_drain_s) or 1,
            **self._https_options(),
        )
        self._http_server = _NoSignalUvicorn(http_config)
        self._spawn(self._http_server.serve(sockets=[self._http_sock]), "http-server")
        log.info(
            "controller.listening",
            grpc=BindAddress((self._grpc_bind.host, self.grpc_port)).grpc_target(),
            http=BindAddress((self._http_bind.host, self.http_port)).grpc_target(),
            enroll=(
                BindAddress((self._enroll_bind.host, self.enroll_port)).grpc_target()
                if self.enroll_port
                else None
            ),
        )

        if cfg.discovery.advertise and not self.settings.security.insecure:
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

    def _https_options(self) -> dict[str, Any]:
        """V0.7 G3 (D139): HTTPS on the REST + blob port - TLS 1.3 only, the
        renewable server certificate, client certificates optional (agents
        present their node certificate, the CLI none); the protocol class
        hands a presented certificate to the API. Plain HTTP only in
        ``insecure-dev`` (loopback)."""
        tls = self.server_tls
        if self.settings.security.insecure or tls is None:
            return {}
        context = tls.ssl_context(client_certs="optional")
        return {
            "ssl_context_factory": lambda _config, _default: context,
            "http": peer_cert_protocol(),
        }

    def _session_server(self, manager: SessionManager) -> tuple[int, grpc.aio.Server]:
        """The ``AgentControl`` server: mutual TLS (V0.7 G3, D137) - the client
        must present a fabric node certificate, every call is checked by
        :class:`NodeCertInterceptor` and the Hello's node must be the
        certificate's. Plaintext only in ``insecure-dev`` (loopback-only)."""
        cfg = self.settings.controller
        target = self._grpc_bind.grpc_target()
        tls, fabric = self.server_tls, self.fabric
        if self.settings.security.insecure or tls is None or fabric is None:
            server = grpc.aio.server(options=list(GRPC_SERVER_OPTIONS))
            agent_pb2_grpc.add_AgentControlServicer_to_server(
                AgentControlServicer(manager, hello_timeout_s=cfg.hello_timeout_s),
                server,
            )
            return server.add_insecure_port(target), server
        fabric_id = fabric.fabric_id
        server = grpc.aio.server(
            options=list(GRPC_SERVER_OPTIONS),
            interceptors=[
                NodeCertInterceptor(
                    fabric_id=fabric_id,
                    revoked=(
                        self.revocations.revoked_serials
                        if self.revocations is not None
                        else frozenset
                    ),
                )
            ],
        )
        agent_pb2_grpc.add_AgentControlServicer_to_server(
            AgentControlServicer(
                manager,
                hello_timeout_s=cfg.hello_timeout_s,
                identify=functools.partial(peer_cert, fabric_id=fabric_id),
                renewals=self.renewals,
            ),
            server,
        )
        credentials = tls.grpc_credentials(require_client_auth=True)
        return server.add_secure_port(target, credentials), server

    async def _start_enrollment(
        self, engine: AsyncEngine, loop: asyncio.AbstractEventLoop
    ) -> None:
        """V0.7 G2 (D150): the TLS ``Enroll`` listener, when a CA exists."""
        enroll = self.settings.controller.enroll
        if not enroll.enabled or self.fabric is None or self.server_tls is None:
            log.info(
                "controller.enroll.off",
                reason="disabled" if not enroll.enabled else "no_fabric",
            )
            return
        limiter = SourceRateLimiter(
            rate_per_minute=enroll.rate_per_minute,
            burst=enroll.rate_burst,
            max_sources=enroll.max_tracked_sources,
            clock=loop.time,
        )
        listener = EnrollListener(
            EnrollmentServicer(Enroller(engine, self.fabric), limiter)
        )
        self.enroll_port = await listener.start(
            self._enroll_bind,
            self.server_tls.grpc_credentials(require_client_auth=False),
        )
        self._enroll_listener = listener

    def _resync_revocations(self, node_id: str) -> None:
        """A renewed certificate went into use: re-read the revocation set now
        (its predecessor is superseded) instead of at the next poll. Best
        effort - the periodic sync is the backstop; a failure is logged."""
        revocations = self.revocations
        if revocations is None:
            return

        async def resync() -> None:
            try:
                await revocations.sync()
            except Exception as exc:  # noqa: BLE001 - the periodic sync retries
                log.warning(
                    "controller.revocation.resync_failed",
                    node_id=node_id,
                    cause=type(exc).__name__,
                )

        task = asyncio.create_task(resync(), name=f"revocation-resync:{node_id}")
        self._resyncs.add(task)
        task.add_done_callback(self._resyncs.discard)

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
        if self._enroll_listener is not None:
            await self._enroll_listener.stop(grace=remaining())
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
        if self.scrubber is not None:
            await self.scrubber.close()
        if self.gc is not None:
            await self.gc.close()
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


def _prepare_fabric(
    settings: ControllerSettings,
) -> tuple[FabricCa | None, ServerCert | None]:
    """Load the fabric CA and ensure the server cert (V0.7 G1, D134).

    No CA yet: a typed ``config_error`` in ``mtls`` mode (V0.7 G3), ``(None,
    None)`` in ``insecure-dev``. An interrupted ``fabric create --force`` is
    repaired first (D149c).
    """
    recover_interrupted(settings.ca_dir)
    if not fabric_present(settings.ca_dir):
        if not settings.security.insecure:
            raise ConfigError(
                "this controller has no fabric CA, and [security] mode = 'mtls' "
                "needs one: run `meshforge-controller fabric create` once on this "
                "machine (or, for development on one machine only, set "
                "[security] mode = 'insecure-dev')",
                details={"path": str(settings.ca_dir)},
            )
        log.info("controller.fabric.absent", hint="meshforge-controller fabric create")
        return None, None
    fabric = load_fabric(settings.ca_dir)
    server = ensure_server_cert(
        settings.tls_dir,
        fabric,
        hosts=settings.server_cert_hosts,
        lifetime=timedelta(days=settings.controller.tls.server_cert_days),
    )
    log.info(
        "controller.fabric.ready",
        fabric_id=fabric.fabric_id,
        ca_fingerprint=fabric.short_fingerprint,
        server_cert=server.action.value,
        server_cert_not_after=server.cert.not_valid_after_utc.isoformat(),
        security_mode=settings.security.mode.value,
    )
    return fabric, server


def _bind_socket(bind: BindAddress) -> socket.socket:
    family = socket.AF_INET6 if ":" in bind.host else socket.AF_INET
    # proto IPPROTO_TCP, not 0: accepted sockets inherit it, and asyncio sets
    # TCP_NODELAY only on sockets whose proto is TCP. With proto 0 every response
    # whose head and body are separate writes (all of them but HEAD) waited for
    # the client's delayed ACK - ~40 ms per small request (V0.5 G4, D97).
    sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
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
