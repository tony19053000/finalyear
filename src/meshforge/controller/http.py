"""REST surface of the controller (FastAPI): ``GET /healthz`` plus ``/v1`` (0.3.12).

The app is built by a factory so tests can mount it on an ephemeral port with their
own :class:`~meshforge.controller.api.context.FabricView`.

**Authenticated (V0.7 G4, D141/D148).** Every route but ``/healthz`` and
``POST /v1/auth/login`` needs a user's API token (``compute login``); blob
routes also take an agent's per-attempt credentials. The listener is HTTPS in
``mtls`` mode (D139) and stays LAN-bound by default (``[controller] http_bind``
``127.0.0.1:47110``; a wildcard bind needs ``allow_any_bind``).

``openapi_url`` is served at ``/v1/openapi.json`` (it was ``None`` while ``/healthz``
was the only endpoint): the schema is the contract the V0.8 dashboard and the SDK
generate their client from, and publishing it exposes no capability a read-only API
does not already have. The interactive docs pages stay off - Swagger UI and ReDoc
load their assets from a public CDN, which a lab machine on an isolated LAN cannot
reach and an operator's browser should not be asked to fetch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from starlette.types import ASGIApp

from meshforge import __version__
from meshforge.controller.alerts import AlertLog
from meshforge.controller.api import (
    FABRIC_STATE_KEY,
    FabricView,
    install_error_handlers,
    router,
)
from meshforge.controller.api.alert_routes import ALERTS_STATE_KEY, alert_router
from meshforge.controller.api.auth import AUTH_STATE_KEY, ApiAuth, guard_requests
from meshforge.controller.api.auth_routes import (
    AUTH_API_STATE_KEY,
    AuthApi,
    auth_router,
)
from meshforge.controller.api.bench_control_routes import bench_control_router
from meshforge.controller.api.bench_routes import (
    BENCH_STATE_KEY,
    BenchApi,
    bench_router,
)
from meshforge.controller.api.blob_routes import BLOBS_STATE_KEY, blob_router
from meshforge.controller.api.data_routes import data_router
from meshforge.controller.api.decision_routes import decision_router
from meshforge.controller.api.event_routes import EVENTS_STATE_KEY, event_router
from meshforge.controller.api.fabric_routes import (
    FABRIC_STATE_KEY_INVITES,
    InviteApi,
    fabric_router,
)
from meshforge.controller.api.job_routes import JOBS_STATE_KEY, JobApi, job_router
from meshforge.controller.api.log_routes import log_router

# V0.15 G1 (D333): the ML training dataset
from meshforge.controller.api.ml_model_routes import (
    ML_MODELS_STATE_KEY,
    MlModelsApi,
    ml_models_router,
)
from meshforge.controller.api.ml_prediction_routes import (
    ML_PREDICTIONS_STATE_KEY,
    MlPredictionsApi,
    ml_predictions_router,
)
from meshforge.controller.api.ml_routes import ML_STATE_KEY, MlDatasetApi, ml_router
from meshforge.controller.api.network_routes import (
    NETWORK_STATE_KEY,
    NetworkApi,
    network_router,
)

# V0.14 G4 (D318): the offline evaluator's reports
from meshforge.controller.api.planner_eval_routes import (
    PLANNER_EVAL_STATE_KEY,
    PlannerEvalApi,
    planner_eval_router,
)
from meshforge.controller.api.planner_routes import (
    PLANNER_STATE_KEY,
    PlannerApi,
    planner_router,
)
from meshforge.controller.api.prepare_routes import prepare_router
from meshforge.controller.api.response_models import ERROR_RESPONSES
from meshforge.controller.api.revocation_routes import (
    REVOCATION_STATE_KEY,
    revocation_router,
)
from meshforge.controller.api.secret_routes import SECRETS_STATE_KEY, secret_router
from meshforge.controller.api.session_routes import session_router
from meshforge.controller.api.split_routes import split_router
from meshforge.controller.api.telemetry_routes import (
    TELEMETRY_STATE_KEY,
    TelemetryExport,
    telemetry_router,
)
from meshforge.controller.blobs import BlobStore
from meshforge.controller.cache_index import CacheLocationStore
from meshforge.controller.events.bus import EventBus
from meshforge.controller.registry import Registry
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.revocation import Revocations
from meshforge.controller.secret_store import SecretStore
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.web.spa import (
    DEFAULT_DIST,
    SecurityHeadersMiddleware,
    SpaMiddleware,
)

__all__ = ["Health", "create_http_app", "openapi_document"]


class Health(BaseModel):
    """``GET /healthz``: ``ok`` or ``degraded`` (still 200)."""

    status: str
    sessions: int
    degraded: list[str] | None = None


OPENAPI_URL = "/v1/openapi.json"


class _ControllerApp(FastAPI):
    """FastAPI with :class:`SecurityHeadersMiddleware` OUTSIDE everything.

    ``add_middleware`` always lands inside Starlette's ``ServerErrorMiddleware``
    (which answers an unhandled exception's 500), so the headers are wrapped
    around the built stack instead (V0.8 G1, D182 T-1: every response).
    """

    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersMiddleware(super().build_middleware_stack())


def create_http_app(
    manager: SessionManager,
    *,
    registry: Registry,
    resources: ResourceTracker,
    clock: Callable[[], float],
    auth: ApiAuth | None = None,
    blobs: BlobStore | None = None,
    jobs: JobApi | None = None,
    cache: CacheLocationStore | None = None,
    network: NetworkApi | None = None,
    invites: InviteApi | None = None,
    revocations: Revocations | None = None,
    users: AuthApi | None = None,
    secrets: SecretStore | None = None,
    events: EventBus | None = None,
    alerts: AlertLog | None = None,
    telemetry: TelemetryExport | None = None,
    bench: BenchApi | None = None,
    planner: PlannerApi | None = None,
    planner_eval: PlannerEvalApi | None = None,
    ml_dataset: MlDatasetApi | None = None,
    ml_models: MlModelsApi | None = None,
    ml_predictions: MlPredictionsApi | None = None,
    web_dist: Path | None = DEFAULT_DIST,
    wall_clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the controller's HTTP app. ``clock`` must be the loop's monotonic clock;
    ``wall_clock`` (epoch seconds) is what ``GET /v1/clock`` answers.

    ``auth`` holds what credentials are checked against (V0.7 G4: the user
    directory, the legacy switch, agents' blob credentials). Without it every
    call but ``/healthz`` is refused (fail closed). ``users`` mounts
    ``/v1/auth`` (login, tokens, whoami).
    """
    app = _ControllerApp(
        title="MeshForge controller",
        version=__version__,
        summary=(
            "The compute fabric's REST surface. Every call but /healthz and "
            "POST /v1/auth/login needs a user's API token (`compute login`)."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
        dependencies=[Depends(guard_requests)],
        # V0.8 G1 (D171): every route documents the one error envelope.
        responses=ERROR_RESPONSES,
    )
    setattr(app.state, AUTH_STATE_KEY, auth or ApiAuth(verifier=None))
    setattr(
        app.state,
        FABRIC_STATE_KEY,
        FabricView(
            registry=registry,
            manager=manager,
            resources=resources,
            clock=clock,
            cache=cache,
            wall_clock=wall_clock,
        ),
    )
    install_error_handlers(app)
    # V0.8 G1 (D169): the dashboard SPA outside /v1 and /healthz (None = off).
    # The security headers wrap the whole stack (_ControllerApp), so even a
    # 500 from Starlette's ServerErrorMiddleware carries them (D182 T-1).
    if web_dist is not None:
        app.add_middleware(SpaMiddleware, dist=web_dist)

    @app.get(
        "/healthz",
        tags=["health"],
        response_model=Health,
        response_model_exclude_none=True,
    )
    async def healthz() -> dict[str, Any]:
        """Liveness probe: the process is up; ``sessions`` = live agent streams.
        ``"degraded"`` (still 200) names a supervised background loop that keeps
        failing (V0.7 G5, D159 N4: the stream auth re-check)."""
        live_streams = getattr(app.state, AUTH_STATE_KEY).live_streams
        degraded: list[str] = []
        if live_streams is not None and live_streams.degraded:
            degraded.append("stream-auth-recheck")
        # V0.9 G5 (D246): the campaign runner crashed 5 ticks in a row
        if bench is not None and bench.runner is not None and bench.runner.degraded:
            degraded.append("bench-runner")
        if degraded:
            return {
                "status": "degraded",
                "sessions": len(manager),
                "degraded": degraded,
            }
        return {"status": "ok", "sessions": len(manager)}

    app.include_router(router)
    if blobs is not None:
        setattr(app.state, BLOBS_STATE_KEY, blobs)
        app.include_router(blob_router)
    if jobs is not None:
        setattr(app.state, JOBS_STATE_KEY, jobs)
        app.include_router(job_router)
        app.include_router(decision_router)  # V0.6 G2 plan decisions
        app.include_router(split_router)  # V0.10 G7 split progress + chunks
        app.include_router(log_router)
        if jobs.inputs is not None:
            app.include_router(data_router)  # V0.5 G5 named inputs
        if jobs.prepare is not None:
            app.include_router(prepare_router)  # V0.5 G5 compute node prepare
    if network is not None:
        setattr(app.state, NETWORK_STATE_KEY, network)
        app.include_router(network_router)  # V0.6 G4 network matrix
    if invites is not None:
        setattr(app.state, FABRIC_STATE_KEY_INVITES, invites)
        app.include_router(fabric_router)  # V0.7 G2 join codes (D135, D151)
    if revocations is not None:
        setattr(app.state, REVOCATION_STATE_KEY, revocations)
        app.include_router(revocation_router)  # V0.7 G3 node revoke (D138)
    if users is not None:
        setattr(app.state, AUTH_API_STATE_KEY, users)
        app.include_router(auth_router)  # V0.7 G4 login + tokens (D141)
        app.include_router(session_router)  # V0.8 G1 browser session (D170)
    if secrets is not None:
        setattr(app.state, SECRETS_STATE_KEY, secrets)
        app.include_router(secret_router)  # V0.7 G6 job secrets (D144)
    if events is not None:
        setattr(app.state, EVENTS_STATE_KEY, events)
        app.include_router(event_router)  # V0.8 G1 live events (D172)
    if alerts is not None:
        setattr(app.state, ALERTS_STATE_KEY, alerts)
        app.include_router(alert_router)  # V0.8 G3 alerts list (D180)
    if telemetry is not None:
        setattr(app.state, TELEMETRY_STATE_KEY, telemetry)
        app.include_router(telemetry_router)  # V0.9 G1 dataset export (D211)
    if bench is not None:
        setattr(app.state, BENCH_STATE_KEY, bench)
        app.include_router(bench_router)  # V0.9 G3 benchmark campaigns (D216)
        app.include_router(bench_control_router)  # V0.9 G5 runner controls
    if planner is not None:
        setattr(app.state, PLANNER_STATE_KEY, planner)
        app.include_router(planner_router)  # V0.14 G2 calibration (D293)
    if planner_eval is not None:
        setattr(app.state, PLANNER_EVAL_STATE_KEY, planner_eval)
        app.include_router(planner_eval_router)  # V0.14 G4 eval reports (D318)
    if ml_dataset is not None:
        setattr(app.state, ML_STATE_KEY, ml_dataset)
        app.include_router(ml_router)  # V0.15 G1 ML training dataset (D333)
    if ml_models is not None:
        setattr(app.state, ML_MODELS_STATE_KEY, ml_models)
        app.include_router(ml_models_router)  # V0.15 G3 model registry (D339)
    if ml_predictions is not None:
        setattr(app.state, ML_PREDICTIONS_STATE_KEY, ml_predictions)
        app.include_router(ml_predictions_router)  # V0.15 G5b scatter (D374)
    return app


def openapi_document() -> dict[str, Any]:
    """The controller's complete OpenAPI document - every optional router
    mounted - built without a database, a server or a socket (V0.8 G1, D171:
    ``scripts/export_openapi.py`` and the dashboard's drift gate use it). The
    collaborators are inert placeholders: no route is ever called."""
    from types import SimpleNamespace

    def inert() -> Any:
        return SimpleNamespace()

    app = create_http_app(
        inert(),
        registry=inert(),
        resources=inert(),
        clock=lambda: 0.0,
        auth=ApiAuth(verifier=None),
        blobs=inert(),
        jobs=SimpleNamespace(inputs=inert(), prepare=inert()),  # type: ignore[arg-type]
        network=inert(),
        invites=inert(),
        revocations=inert(),
        users=inert(),
        secrets=inert(),
        events=inert(),
        alerts=inert(),
        telemetry=inert(),
        bench=inert(),
        planner=inert(),
        planner_eval=inert(),
        ml_dataset=inert(),
        ml_models=inert(),
        ml_predictions=inert(),
        web_dist=None,
    )
    return app.openapi()
