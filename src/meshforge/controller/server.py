"""The controller process: one asyncio loop hosting gRPC + REST (ARCHITECTURE §3, D6).

:class:`ControllerRuntime` wires everything and owns every task it starts::

    settings -> fabric CA + server cert (V0.7 G1; only when `fabric create` ran)
             -> managed PostgreSQL ([controller.db] mode = "managed", V0.17 G2)
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
import ipaddress
import signal
import socket
from collections.abc import Coroutine, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import grpc
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge import __version__
from meshforge.common.config import INSECURE_DEV_BANNER
from meshforge.common.errors import ConfigError, ExitCode, MeshForgeError
from meshforge.common.gc_index import GcIndexBusyError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import BindAddress, parse_bind
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.common.split_items import SplitLimits
from meshforge.controller.alerts import AlertLog
from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.api.auth_routes import AuthApi
from meshforge.controller.api.bench_routes import BenchApi

# V0.15 G3 (D339, D361): ML model registry, model source, retrain task
from meshforge.controller.api.decision_replay_routes import DecisionReplayApi
from meshforge.controller.api.fabric_routes import InviteApi
from meshforge.controller.api.job_routes import JobApi
from meshforge.controller.api.ml_classify_routes import MlClassifyApi
from meshforge.controller.api.ml_model_routes import MlModelsApi
from meshforge.controller.api.ml_prediction_routes import MlPredictionsApi
from meshforge.controller.api.ml_routes import MlDatasetApi
from meshforge.controller.api.network_routes import NetworkApi
from meshforge.controller.api.planner_eval_routes import PlannerEvalApi
from meshforge.controller.api.planner_routes import PlannerApi
from meshforge.controller.api.telemetry_routes import TelemetryExport
from meshforge.controller.auth.live_streams import LiveStreams
from meshforge.controller.auth.login import LoginService, login_policy
from meshforge.controller.auth.passwords import PasswordHasher
from meshforge.controller.auth.users import UserStore
from meshforge.controller.bench.conditions import (
    BenchConditions,
    NodePauses,
    bench_identities,
)
from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.bench.submit import BenchSubmitter
from meshforge.controller.blobs import BlobStore, FetchTokens
from meshforge.controller.cache_index import CacheIndex, CacheLocationStore

# V0.14 G2 (D293): planner calibration (store, per-tick source, refresh task)
from meshforge.controller.calibration_store import CalibrationSource, CalibrationStore
from meshforge.controller.db_managed.backup import (
    UpgradeBackup,
    clear_marker,
    clear_rollback_failed,
    take_upgrade_backup,
    write_marker,
    write_rollback_failed,
)
from meshforge.controller.db_managed.cluster import ManagedCluster
from meshforge.controller.db_managed.errors import ManagedDbError, ManagedDbReason
from meshforge.controller.db_managed.runtime import cluster_for
from meshforge.controller.discovery import ServiceAdvertiser
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.enrollment.listener import (
    EnrollListener,
    EnrollmentServicer,
    SourceRateLimiter,
)
from meshforge.controller.enrollment.renewal import CertRenewals
from meshforge.controller.enrollment.service import Enroller
from meshforge.controller.events import changes
from meshforge.controller.events.bus import Audience, EventBus, EventType
from meshforge.controller.events.watcher import NodeWatcher
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
from meshforge.controller.http_tls import (
    BlobPortSecurity,
    blob_port_security,
    peer_cert_protocol,
)
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.env_health import KnownBadEnvs
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.ml import export as ml_export
from meshforge.controller.ml.decision_replay import DecisionReplayer
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.registry_loader import RegistryLoader

# V0.15 G3 + G4 (D336/D339): the registry's model set, as the planner's input
from meshforge.controller.ml.source import ModelSource, RegistryMlInputSource
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.named_inputs import NamedInputStore, PinBroadcaster
from meshforge.controller.network import NetworkProber, NetworkStore
from meshforge.controller.peer_signing import (
    PeerTokenIssuer,
    load_or_create_signing_key,
    public_key_fingerprint,
)

# V0.14 G4 (D297/D318): the offline evaluator
from meshforge.controller.planner.calibration import Calibration
from meshforge.controller.planner_calibration_task import (
    CalibrationRefresher,
    calibration_config,
)
from meshforge.controller.planner_eval.classifier_service import (
    ClassifierExamplesService,
)
from meshforge.controller.planner_eval.latency import LatencyConfig
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
from meshforge.controller.planner_eval.model import EvalConfig
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
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
from meshforge.controller.settings import (
    LOOPBACK_PLAIN_HOSTS,
    ControllerSection,
    ControllerSettings,
)
from meshforge.controller.snapshots import SnapshotFlusher
from meshforge.controller.split.combine import CombineBusy
from meshforge.controller.split.progress import SplitProgress
from meshforge.controller.split.progress_events import ProgressTracker
from meshforge.controller.split.progress_reads import ProgressReads
from meshforge.controller.store.engine import check_connection, create_engine
from meshforge.controller.store.migrate import (
    current_revision,
    head_revision,
    upgrade_to_head,
)
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry.recorder import RunRecorder
from meshforge.controller.upload_grants import UploadGrants
from meshforge.controller.web.host_guard import LoopbackHostGuard

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
        self.recorder: RunRecorder | None = None
        self.calibration: CalibrationSource | None = None
        """V0.14 G2 (D293): the calibration the dispatcher plans with."""
        self.calibration_refresher: CalibrationRefresher | None = None
        self.ml_source: RegistryMlInputSource | None = None
        """V0.15 G3 + G4 (D336/D339): the ML model set the dispatcher plans
        with, read once per tick (``ml_source.models`` = the registry-side
        :class:`~.ml.source.ModelSource`; None before start)."""
        self.ml_retrainer: MlRetrainer | None = None
        self.planner_eval: EvalService | None = None
        """V0.15 G6b (D378): the evaluator's background runs (stopped
        first at shutdown, so a running evaluation's fold child is killed)."""
        self.bench: BenchStore | None = None
        self.bench_submitter: BenchSubmitter | None = None
        """V0.9 G1: the run-record sweep (:mod:`.telemetry.recorder`)."""
        self._grpc_server: grpc.aio.Server | None = None
        self._enroll_listener: EnrollListener | None = None
        self._http_server: _NoSignalUvicorn | None = None
        self._http_sock: socket.socket | None = None
        self._plain_sock: socket.socket | None = None
        self._plain_server: _NoSignalUvicorn | None = None
        self.plain_port: int | None = None
        """Q1: the plain-HTTP loopback port (``[controller.http] loopback_plain``)."""
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
        self.events: EventBus | None = None
        """V0.8 G1 (D172): the dashboard's event bus."""
        self.split_progress: ProgressTracker | None = None
        """V0.10 G7 (D270): ``split.progress`` events (re-reads on change)."""
        self.node_watcher: NodeWatcher | None = None
        self._watcher_task: asyncio.Task[None] | None = None
        self.blob_port = BlobPortSecurity(blob_port_is_tls=False)
        """D168 N2: set once in ``_start`` (see :class:`BlobPortSecurity`)."""
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
        self.managed_db: ManagedCluster | None = None
        """V0.17 G2 (D390): the controller's own PostgreSQL in ``[controller.db]
        mode = "managed"`` (started first, stopped last); None in external mode."""
        self._db_watch: asyncio.Task[None] | None = None
        self._rollback: UpgradeBackup | None = None
        """D393: the pre-upgrade backup :meth:`stop` restores (failed upgrade)."""
        self.setup_pending = False
        """V0.17 G2 (D392): running the setup page only (no CA yet)."""
        self.setup_port: int = 0
        self.restart_requested = False
        """Set when setup completed: :func:`_serve` starts a new runtime."""
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
        # V0.17 G2 (D392): no CA + a setup token -> only the setup page runs.
        self.setup_pending = await asyncio.to_thread(_setup_offered, self.settings)
        self.fabric, self.server_cert = await asyncio.to_thread(
            _prepare_fabric, self.settings, self.setup_pending
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
        # D168 N2: one value for "is the REST + blob port TLS" - listener,
        # URL scheme and the D139 node-certificate lock all read it.
        self.blob_port = blob_port_security(
            insecure=self.settings.security.insecure or self.setup_pending,
            server_tls_ready=self.server_tls is not None,
        )
        self.blob_port.check_url(
            cfg.api.advertise_url, setting="[controller.api] advertise_url"
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

        database, migrate_as = await self._start_database()
        engine = create_engine(database)
        self._engine = engine
        await check_connection(engine, database)
        upgrade = await self._migrate(migrate_as)
        # D393: after an upgrade's migrations the controller must be serving
        # within health_timeout_s; otherwise the pre-upgrade backup is
        # restored (in stop(), once nothing uses the database) and it exits 4.
        health_s = cfg.db.health_timeout_s if upgrade is not None else None
        try:
            async with asyncio.timeout(health_s):
                if self.setup_pending:
                    await self._start_setup(engine)
                else:
                    await self._start_services(cfg, loop, blobs, engine)
                await check_connection(engine, database)
        except Exception as exc:
            if upgrade is None:
                raise
            raise self._upgrade_failed(upgrade, exc) from exc
        await self._record_good_start(migrate_as)

    async def _start_setup(self, engine: AsyncEngine) -> None:
        """V0.17 G2 (D392): setup mode - only the "create fabric" page, plain
        HTTP on a loopback socket, until the fabric exists; then a restart."""
        from meshforge.controller.doctor import run_checks
        from meshforge.controller.setup.routes import create_setup_app
        from meshforge.controller.setup.service import SetupService

        users = await UserStore(engine).list_users()
        if users:
            # Users without a CA: the fabric was made by hand once - setup is
            # over (410 forever); the CLI must recreate the CA.
            raise ConfigError(
                "this controller has users but no fabric CA: run `meshforge-"
                "controller fabric create` on it (the setup page is only for a "
                "new controller)",
                details={"path": str(self.settings.ca_dir)},
            )
        settings = self.settings
        service = SetupService(
            settings,
            engine,
            on_done=self._setup_done,
            checks=lambda: run_checks(settings, cluster_running=lambda: True).as_dict(),
        )
        bind = parse_bind(settings.setup_bind, allow_any=False)
        self._plain_sock = _bind_socket(bind)
        self.setup_port = self._plain_sock.getsockname()[1]
        config = uvicorn.Config(
            LoopbackHostGuard(create_setup_app(service), port=self.setup_port),
            host=bind.host,
            port=self.setup_port,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=int(settings.controller.shutdown_drain_s) or 1,
        )
        self._plain_server = _NoSignalUvicorn(config)
        self._spawn(self._plain_server.serve(sockets=[self._plain_sock]), "setup-page")
        log.warning(
            "controller.setup.waiting",
            url=f"http://{BindAddress((bind.host, self.setup_port)).grpc_target()}/setup",
            hint="sudo meshforge-controller setup url  (prints the link with its token)",
        )

    def _setup_done(self) -> None:
        log.warning("controller.setup.restarting")
        self.restart_requested = True
        self.request_stop()

    async def _start_services(
        self,
        cfg: ControllerSection,
        loop: asyncio.AbstractEventLoop,
        blobs: BlobStore,
        engine: AsyncEngine,
    ) -> None:
        """Everything after the migrations: stores, tasks, listeners."""
        # Set by _start before the migrations.
        assert self.peer_tokens is not None  # noqa: S101 - narrowing
        assert self.fetch_tokens is not None  # noqa: S101 - narrowing
        if cfg.gc.enabled:
            gc = ControllerGc(
                blobs,
                engine,
                index_dir=self.settings.gc_dir,
                quota_bytes=cfg.gc.cas_bytes,
                min_age_s=cfg.gc.min_age_s,
                interval_s=cfg.gc.interval_s,
                batch=cfg.gc.batch,
                ml_keep_superseded=cfg.planner.ml_keep_superseded,  # V0.15 G3
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
        self.jobs = ExecutionStore(
            engine,
            combine_busy=CombineBusy(
                backoff_s=cfg.split.combine_busy_backoff_s,
                max_free=cfg.split.combine_busy_max,
            ),
        )
        # V0.8 G1 (D172): the dashboard's event bus; job/attempt changes are
        # published once their transaction commits (events.changes).
        ev = cfg.events
        self.events = EventBus(
            ring_size=ev.ring_size,
            queue_size=ev.queue_size,
            max_subscribers=ev.max_subscribers,
        )
        changes.attach(engine, self._publish_changes)
        # V0.10 G7 (D270): split jobs' progress, read from their rows when
        # they change (chunk messages, state changes, expiry) and on a resync.
        progress_reads = ProgressReads(
            engine, rate_window=cfg.split.progress_rate_window
        )
        tracker = ProgressTracker(
            progress_reads, publish=self._publish_split_progress, settings=cfg.split
        )
        self.split_progress = tracker
        self._spawn(tracker.run(), "split-progress")
        self.inputs = NamedInputStore(engine)
        # V0.7 G6 (D144): job secrets; the master key is loaded on first use.
        self.secrets = SecretStore(engine, self.settings.secrets_key_path)
        swept = await registry.sweep_open_sessions(SessionEndReason.CONTROLLER_RESTART)
        offline = await registry.mark_live_nodes_offline()
        # V0.15 G6b (D378): an evaluation never outlives its controller
        eval_reports = EvalReportStore(engine)
        evals_failed = await eval_reports.sweep_running()
        log.info(
            "controller.startup_sweep",
            sessions_closed=swept,
            nodes_offline=offline,
            evaluations_failed=evals_failed,
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
        # V0.9 G1 (D213): an untrustworthy node clock is an operator alert.
        manager.heartbeats.on_clock_alert = self._publish_alert
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
        # V0.9 G4 (D217): benchmark conditions - the agent's kind rides in
        # StartAttempt; an ``unavailable:T`` run pauses its origin here.
        self.bench_conditions = BenchConditions(engine)
        self.node_pauses = NodePauses(loop.time)
        # V0.14 G2 (D293/D301): the planner calibration. A pinned version
        # that does not exist stops the start (never a silent fallback).
        calibration_store = CalibrationStore(
            engine,
            max_label_rows=self.settings.controller.telemetry.export_max_rows,
        )
        self.calibration = CalibrationSource(calibration_store, cfg.planner.calibration)
        await self.calibration.check()
        self.calibration_refresher = CalibrationRefresher(
            calibration_store, cfg.planner
        )
        if self.calibration.mode == "auto":
            self._spawn(self.calibration_refresher.run(), "planner-calibration")
        # V0.15 G3 (D339/D361): the model registry; a configured pin that does
        # not exist, was rejected or cannot load stops the start
        ml_registry = ModelRegistry(engine, blobs.cas, used=blobs.used)
        ml_models = ModelSource(ml_registry, cfg.planner.ml)
        await ml_models.check()
        # G3 + G4 merge: the dispatcher's model = the serving set, built with
        # ml_input (warm-up; None = no usable model) per loaded set version
        self.ml_source = RegistryMlInputSource(
            ml_models,
            max_interval_log=cfg.planner.ml_max_interval_log,
            budget_ms=cfg.planner.ml_predict_budget_ms,
        )
        await self.ml_source.current()
        # V0.15 G5a: stored sets by version (any status, LRU of 4) for the
        # D371 regret check and the evaluator's allow_leak rows
        ml_sets = RegistryLoader(
            ml_registry, max_interval_log=cfg.planner.ml_max_interval_log
        )
        calibration_source = self.calibration

        async def live_calibration() -> Calibration | None:
            return calibration_source.current

        self.ml_retrainer = MlRetrainer(
            engine,
            ml_registry,
            ml_models,
            cfg.planner,
            set_loader=ml_sets,
            calibration=live_calibration,
            max_label_rows=self.settings.controller.telemetry.export_max_rows,
        )
        if ml_models.mode == "auto":
            self._spawn(self.ml_retrainer.run(), "ml-retrain")
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
            on_alert=self._publish_alert,
            known_bad_envs=self.known_bad_envs,
            planner=cfg.planner,
            network=cfg.network,
            cache_live=self.cache_index.is_live,
            bench_condition=self.bench_conditions.agent_condition,
            node_paused=self.node_pauses.paused,
            blobs=self.blobs,
            split=cfg.split,
            on_split_change=tracker.touch,
            calibration=self.calibration,
            ml=self.ml_source,  # V0.15 G4 (D336)
            on_integrity_failure=(
                functools.partial(
                    self.scrubber.suspect_tree_soon, why="attempt_integrity_error"
                )
                if self.scrubber is not None
                else None
            ),
        )
        self._dispatcher_task = self._spawn(self.dispatcher.run(), "dispatcher")
        # V0.9 G1 (D211/D212): a run record for every terminal attempt.
        self.recorder = RunRecorder(
            engine,
            interval_s=cfg.telemetry.record_interval_s,
            batch=cfg.telemetry.record_batch,
            # V0.9 G3 (D232): benchmark identity = the bench_run.job_id link;
            # G4: + the run's condition, session label, controller-side facts
            campaign_of=functools.partial(
                bench_identities, started_at=datetime.now(UTC)
            ),
        )
        # V0.9 G3 (D215/D216): campaigns + forced runs (no background loop
        # until G5; `POST .../submit?count=k` drives them).
        self.bench = BenchStore(engine)
        self.bench_submitter = BenchSubmitter(
            store=self.bench,
            jobs=self.jobs,
            blobs=blobs,
            registry=registry,
            manager=manager,
            engine=engine,
            wake=self.dispatcher.wake,
            pauses=self.node_pauses,
            # V0.10 G8 (D278): split families resolve their items like any
            # split job, under the same [controller.split] caps
            split_limits=SplitLimits(
                max_items=cfg.split.max_items, max_chunks=cfg.split.max_chunks
            ),
        )
        # V0.9 G5 fix r1 (D250 D-1): orphan benchmark jobs are cancelled
        self.bench.cancel_job = self.dispatcher.cancel
        self.bench.orphan_scan_s = cfg.bench.orphan_scan_s
        self.bench_submitter.cancel_job = self.dispatcher.cancel
        self._spawn(self.recorder.run(), "telemetry-recorder")
        # V0.9 G5 (D219/D246): the unattended campaign runner (supervised)
        self.bench_control = CampaignControl(engine)
        events = self.events

        def campaign_progress(data: Mapping[str, Any]) -> None:
            if events is not None:
                events.publish(
                    EventType.CAMPAIGN_PROGRESS, data, audience=Audience.ADMIN
                )

        bench_cfg = cfg.bench
        self.bench_runner = BenchRunner(
            store=self.bench,
            control=self.bench_control,
            submitter=self.bench_submitter,
            settings=RunnerSettings(
                tick_s=bench_cfg.tick_s,
                max_submit_per_tick=bench_cfg.max_submit_per_tick,
                max_examine_per_tick=bench_cfg.max_examine_per_tick,
            ),
            sessions=manager,
            publish=campaign_progress,
            clock=loop.time,
            exclusive=True,  # G5c item 0.5: one runner per database
        )
        if bench_cfg.runner:
            self._spawn(self.bench_runner.run(), "bench-runner")
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
        self.users = UserStore(
            engine, session_idle_s=round(cfg.auth.session_idle_hours * 3600)
        )
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
        self.node_watcher = NodeWatcher(
            self.events,
            manager,
            resources,
            clock=loop.time,
            interval_s=cfg.events.watch_interval_s,
            metrics_interval_s=cfg.events.metrics_interval_s,
        )
        self._watcher_task = self._spawn(self.node_watcher.run(), "event-node-watcher")

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
        self.planner_eval = EvalService(
            engine,
            calibration_store,
            eval_reports,
            calibration_config(cfg.planner),
            max_rows=self.settings.controller.telemetry.export_max_rows,
            # V0.14 G6 (D324): criterion 3's budget is the dispatcher's
            config=EvalConfig(
                latency=LatencyConfig(budget_ms=cfg.planner.decision_latency_budget_ms)
            ),
            # V0.15 G5a (D368): the ML rows' fold models
            ml=FoldMlInputs(
                cfg.planner,
                ml_models,
                ml_sets,
                functools.partial(
                    ml_export.build_dataset,
                    engine,
                    max_runs=cfg.planner.ml_dataset_max_runs,
                ),
            ),
        )
        http_app = create_http_app(
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
                # D139 two locks - only where the port is TLS: plain HTTP
                # (insecure-dev, loopback) never carries a client
                # certificate, so requiring one there refused every agent
                # fetch once a fabric CA existed (V0.7 G7).
                node_certs=self.blob_port.node_cert_policy(
                    None if self.fabric is None else self.fabric.fabric_id,
                    revocations.revoked_serials,
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
                bench=self.bench,
                split_limits=SplitLimits(
                    max_items=cfg.split.max_items, max_chunks=cfg.split.max_chunks
                ),
                split_progress=progress_reads,
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
            events=self.events,
            alerts=AlertLog(engine),
            telemetry=TelemetryExport(
                engine, max_rows=self.settings.controller.telemetry.export_max_rows
            ),
            bench=BenchApi(
                store=self.bench,
                submitter=self.bench_submitter,
                registry=registry,
                control=self.bench_control,
                runner=self.bench_runner,
                cancel_job=self.dispatcher.cancel,
                quality_cv_threshold=cfg.bench.quality_cv_threshold,
            ),
            planner=PlannerApi(
                store=calibration_store,
                source=self.calibration,
                refresher=self.calibration_refresher,
            ),
            planner_eval=PlannerEvalApi(service=self.planner_eval),
            # V0.15 G1 (D333): the ML training dataset
            ml_dataset=MlDatasetApi(engine, max_runs=cfg.planner.ml_dataset_max_runs),
            # V0.15 G3 (D339): the model registry
            ml_models=MlModelsApi(
                registry=ml_registry,
                source=ml_models,
                retrainer=self.ml_retrainer,
                # V0.15 G5b (G4 reviewer): live predict time + error count
                serving_stats=None
                if self.dispatcher is None
                else self.dispatcher.ml_serving_stats,
            ),
            # V0.15 G5b (D374): predicted vs actual per term
            ml_predictions=MlPredictionsApi(engine, calibration_store),
            # V0.15 G6a (D375): the 0.15.10 classifier experiment's examples
            ml_classify=MlClassifyApi(
                ClassifierExamplesService(
                    engine,
                    calibration_store,
                    calibration_config(cfg.planner),
                    max_rows=self.settings.controller.telemetry.export_max_rows,
                    timeout_s=cfg.planner.ml_classifier_timeout_s,
                )
            ),
            # V0.15 G6a (D364): re-plan a stored decision with its model set
            decision_replay=DecisionReplayApi(
                DecisionReplayer(
                    engine,
                    ml_sets,
                    load_timeout_s=cfg.planner.ml_replay_load_timeout_s,
                )
            ),
        )
        http_config = uvicorn.Config(
            http_app,
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
        if cfg.http.loopback_plain is not None:
            self._serve_loopback_plain(http_app, cfg.http.loopback_plain)
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

    def _serve_loopback_plain(self, http_app: Any, bind_text: str) -> None:
        """Q1 (V0.8 G1): the same app in plain HTTP on 127.0.0.1 / ::1 only, for
        a browser on this host. The settings refuse any other address; the
        bound socket is checked again here (fail closed: never a LAN port)."""
        bind = parse_bind(bind_text)
        if bind.host not in LOOPBACK_PLAIN_HOSTS:  # the settings already refuse it
            raise ConfigError(
                "[controller.http] loopback_plain must bind 127.0.0.1 or [::1]",
                details={"bind": bind_text},
            )
        sock = _bind_socket(bind)
        bound_host = str(sock.getsockname()[0])
        if not ipaddress.ip_address(bound_host).is_loopback:
            sock.close()
            raise ConfigError(
                "[controller.http] loopback_plain did not bind a loopback address",
                details={"bind": bind_text},
            )
        sock.listen(_HTTP_BACKLOG)
        self._plain_sock = sock
        self.plain_port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            # D182 O-1: only Host localhost / 127.0.0.1 / [::1] (DNS rebinding).
            LoopbackHostGuard(http_app, port=self.plain_port),
            host=bind.host,
            port=self.plain_port,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=int(self.settings.controller.shutdown_drain_s)
            or 1,
        )
        self._plain_server = _NoSignalUvicorn(config)
        self._spawn(self._plain_server.serve(sockets=[sock]), "http-loopback-plain")
        log.info(
            "controller.listening_loopback_plain",
            http=BindAddress((bind.host, self.plain_port)).grpc_target(),
        )

    def _https_options(self) -> dict[str, Any]:
        """V0.7 G3 (D139): HTTPS on the REST + blob port - TLS 1.3 only, the
        renewable server certificate, client certificates optional (agents
        present their node certificate, the CLI none); the protocol class
        hands a presented certificate to the API. Plain HTTP only in
        ``insecure-dev`` (loopback)."""
        tls = self.server_tls
        if not self.blob_port.blob_port_is_tls or tls is None:
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

    def _publish_changes(self, notes: list[changes.Change]) -> None:
        """Committed job/attempt changes -> bus events (D172; owner-scoped)."""
        if self.recorder is not None and any(n.kind != "job" for n in notes):
            self.recorder.nudge()  # an attempt changed: maybe it just ended
        if self.split_progress is not None:
            # V0.10 G7: a lane / combine / split job changed state (Mode 1
            # jobs are recognised and ignored by the tracker)
            for job_id in {n.data.get("job_id") for n in notes}:
                if job_id:  # a note without a job is not a split change
                    self.split_progress.touch(str(job_id))
        if self.events is None:
            return
        for note in notes:
            self.events.publish(
                EventType.JOB_STATE if note.kind == "job" else EventType.ATTEMPT_STATE,
                note.data,
                audience=Audience.OWNER,
                owner_user_id=note.owner_user_id,
            )

    def _publish_split_progress(self, progress: SplitProgress) -> None:
        """V0.10 G7 (D270): ``split.progress`` for the job's owner and admins."""
        if self.events is not None:
            self.events.publish(
                EventType.SPLIT_PROGRESS,
                progress.to_data(),
                audience=Audience.OWNER,
                owner_user_id=progress.owner_user_id,
            )

    def _publish_alert(self, data: dict[str, str]) -> None:
        """D180: a security refusal -> an admin-only ``alert`` event."""
        if self.events is not None:
            self.events.publish(EventType.ALERT, data, audience=Audience.ADMIN)

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

    # --------------------------------------------------------------- database

    async def _start_database(self) -> tuple[DatabaseSettings, DatabaseSettings]:
        """``(engine settings, migration settings)``.

        External mode: ``[database]`` for both. Managed mode (V0.17 G2, D390):
        start the bundled PostgreSQL **here, on the event-loop thread** (its
        parent-death signal follows the forking thread), then connect as the
        application role and migrate as the admin role acting as it (Q18).
        """
        base = self.settings.database
        if not self.settings.managed_db:
            return base, base
        cluster = cluster_for(self.settings)
        self.managed_db = cluster
        await cluster.start()
        self._db_watch = asyncio.create_task(
            self._watch_database(cluster), name="managed-db-watch"
        )
        return (
            cluster.database_settings(base=base),
            cluster.admin_settings(base=base),
        )

    async def _migrate(self, migrate_as: DatabaseSettings) -> UpgradeBackup | None:
        """Migrate to head; managed mode backs up first when needed (D393).

        A failed migration after a backup rolls back like a failed health
        check. Alembic is synchronous (and runs its own loop inside env.py).
        """
        backup: UpgradeBackup | None = None
        if self.managed_db is not None:
            db_revision = await asyncio.to_thread(current_revision, migrate_as)
            backup = await take_upgrade_backup(
                self.managed_db,
                version=__version__,
                db_revision=db_revision,
                head_revision=head_revision(),
                keep=self.settings.controller.db.backups_keep,
            )
        try:
            await asyncio.to_thread(upgrade_to_head, migrate_as)
        except Exception as exc:
            if backup is None:
                raise
            raise self._upgrade_failed(backup, exc) from exc
        return backup

    def _upgrade_failed(
        self, backup: UpgradeBackup, exc: BaseException
    ) -> ManagedDbError:
        """Arrange the D393 rollback (done by :meth:`stop`) and say so."""
        self._rollback = backup
        log.critical(
            "controller.db.upgrade_failed",
            cause=type(exc).__name__,
            error=str(exc)[:500],
            backup=str(backup.path),
            from_version=backup.from_version,
            to_version=backup.to_version,
        )
        return ManagedDbError(
            f"the controller did not start after upgrading to {backup.to_version} "
            f"({type(exc).__name__}: {str(exc)[:200]}); the database is being "
            f"restored from the pre-upgrade backup {backup.path} (`meshforge-"
            "controller db status` reports it if that fails too) - start the "
            "previous version, or fix the problem and start again",
            reason=ManagedDbReason.UPGRADE_ROLLED_BACK,
            details={
                "backup": str(backup.path),
                "from_version": backup.from_version,
                "to_version": backup.to_version,
                "cause": type(exc).__name__,
            },
        )

    async def _record_good_start(self, migrate_as: DatabaseSettings) -> None:
        """Managed mode: remember the version that started successfully (the
        next start backs up before migrating when it differs, D393)."""
        if self.managed_db is None:
            return
        revision = await asyncio.to_thread(current_revision, migrate_as)
        await asyncio.to_thread(
            write_marker,
            self.managed_db.layout.root,
            version=__version__,
            revision=revision,
        )
        # A good start supersedes an earlier failed rollback's record.
        await asyncio.to_thread(clear_rollback_failed, self.managed_db.layout.root)

    async def _restore_after_failed_upgrade(self) -> None:
        """Restore the pre-upgrade backup (D393). If that fails too, leave a
        persistent ``rollback-failed.json`` (``db status`` and the package's
        postinst report it) - the database is then *not* the pre-upgrade one."""
        backup, self._rollback = self._rollback, None
        if backup is None or self.managed_db is None:
            return
        root = self.managed_db.layout.root
        cluster = self.managed_db

        async def restore() -> None:
            if not cluster.running:  # the postmaster died: nothing to restore into
                raise ManagedDbError(
                    "the managed database is not running",
                    reason=ManagedDbReason.NOT_RUNNING,
                )
            await cluster.restore_database(backup.path)

        try:
            await restore()
        except Exception as exc:  # noqa: BLE001 - logged loudly, stop continues
            error = f"{type(exc).__name__}: {str(exc)[:400]}"
            with contextlib.suppress(OSError):
                await asyncio.to_thread(
                    write_rollback_failed,
                    root,
                    backup=backup.path,
                    from_version=backup.from_version,
                    to_version=backup.to_version,
                    error=error,
                )
            log.critical(
                "controller.db.rollback_failed",
                backup=str(backup.path),
                cause=type(exc).__name__,
                error=str(exc)[:500],
                hint="restore by hand: meshforge-controller db restore <backup> --yes",
            )
            return
        await asyncio.to_thread(clear_marker, root)
        await asyncio.to_thread(clear_rollback_failed, root)
        log.warning(
            "controller.db.rolled_back",
            backup=str(backup.path),
            restored_version=backup.from_version,
        )

    async def _watch_database(self, cluster: ManagedCluster) -> None:
        """A postmaster that exits while the controller runs is fatal: the
        controller stops with ``FAILURE`` and systemd restarts both (the
        restart's reconciliation cleans up after it)."""
        code = await cluster.wait_exit()
        if self._stopped:
            return
        log.critical(
            "controller.db.managed.exited",
            exit_code=code,
            data_dir=str(cluster.layout.data_dir),
            hint="see the PostgreSQL logs in " + str(cluster.layout.log_dir),
        )
        if self.failed_task is None:
            self.failed_task = "managed-db"
        self.request_stop()

    async def _stop_database(self) -> None:
        watch, self._db_watch = self._db_watch, None
        if watch is not None and not watch.done():
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        if self.managed_db is not None:
            try:
                await self.managed_db.stop()
            except ManagedDbError as exc:
                log.error(  # noqa: TRY400 - typed, details logged; no traceback
                    "controller.db.managed.stop_failed",
                    error=str(exc),
                    failure_type=exc.failure_type.value,
                    details=exc.details,
                )

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
        if self.calibration_refresher is not None:
            self.calibration_refresher.request_stop()  # V0.14 G2
        if self.ml_retrainer is not None:
            self.ml_retrainer.request_stop()  # V0.15 G3
        eval_stopped = (
            None if self.planner_eval is None else self.planner_eval.request_stop()
        )  # V0.15 G6b (D378): kills its fold child, marks controller_restart
        if eval_stopped is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(eval_stopped, return_exceptions=True), remaining()
                )
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
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()  # forever by design; nothing to drain
            await asyncio.gather(self._watcher_task, return_exceptions=True)
        if self.split_progress is not None:
            # V0.10 G7: ends after its current read (never cancelled mid-query)
            self.split_progress.request_stop()
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
        if self._plain_server is not None:
            self._plain_server.should_exit = True
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=remaining())
            for task in still:  # anything still running after the drain is late
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
        if self._http_sock is not None:
            self._http_sock.close()
        if self._plain_sock is not None:
            self._plain_sock.close()
        if self.scrubber is not None:
            await self.scrubber.close()
        if self.gc is not None:
            await self.gc.close()
        if self._engine is not None:
            changes.detach(self._engine)
            await self._engine.dispose()
        # V0.17 G2: a failed upgrade's rollback (D393), then the managed
        # PostgreSQL last - nothing uses it any more.
        await self._restore_after_failed_upgrade()
        await self._stop_database()
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


def _setup_offered(settings: ControllerSettings) -> bool:
    """V0.17 G2: ``mtls``, no fabric CA and a setup token -> setup mode."""
    from meshforge.controller.setup.token import read_setup_token

    if settings.security.insecure:
        return False
    recover_interrupted(settings.ca_dir)
    if fabric_present(settings.ca_dir):
        return False
    return read_setup_token(settings.setup_token_path) is not None


def _prepare_fabric(
    settings: ControllerSettings, setup_pending: bool = False
) -> tuple[FabricCa | None, ServerCert | None]:
    """Load the fabric CA and ensure the server cert (V0.7 G1, D134).

    No CA yet: a typed ``config_error`` in ``mtls`` mode (V0.7 G3), ``(None,
    None)`` in ``insecure-dev``. An interrupted ``fabric create --force`` is
    repaired first (D149c).
    """
    recover_interrupted(settings.ca_dir)
    if not fabric_present(settings.ca_dir):
        if setup_pending:
            log.info("controller.fabric.absent", setup="pending")
            return None, None
        if not settings.security.insecure:
            raise ConfigError(
                "this controller has no fabric CA, and [security] mode = 'mtls' "
                "needs one: open the setup page (`meshforge-controller setup url` "
                "prints its link) or run `meshforge-controller fabric create` once "
                "on this machine (or, for development on one machine only, set "
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
    while True:
        runtime = ControllerRuntime(settings)
        # Before start-up, not after: start-up awaits migrations and the mDNS
        # registration (~1 s), and a SIGTERM in that window must still be a
        # graceful stop, not the default "killed by signal" exit (G3 finding).
        runtime.install_signal_handlers()
        async with _RuntimeContext(runtime):
            await runtime.wait_for_stop()
        if not runtime.restart_requested:
            break
        # V0.17 G2: the setup page created the fabric - start again, now with
        # the CA (TLS listeners, gRPC, enrolment) - in this same process.
        log.info("controller.restarting", reason="setup_complete")
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
