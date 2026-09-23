"""REST surface of the controller (FastAPI): ``GET /healthz`` plus ``/v1`` (0.3.12).

The app is built by a factory so tests can mount it on an ephemeral port with their
own :class:`~meshforge.controller.api.context.FabricView`.

**Read-only and unauthenticated in V0.3 (brief §5e D14).** No endpoint mutates
anything - no node delete, no drain, no command - and there is no authentication
yet, so the listener must stay LAN-bound: ``[controller] http_bind`` defaults to
``127.0.0.1:47110`` and a wildcard bind is refused without ``allow_any_bind``.
Anyone who can reach the port can read the fabric inventory (hostnames, hardware,
addresses, utilisation). V0.7 adds identity, mTLS and authorization (ADR-0008);
``docs/OPERATIONS.md`` states the same boundary for operators.

``openapi_url`` is served at ``/v1/openapi.json`` (it was ``None`` while ``/healthz``
was the only endpoint): the schema is the contract the V0.8 dashboard and the SDK
generate their client from, and publishing it exposes no capability a read-only API
does not already have. The interactive docs pages stay off - Swagger UI and ReDoc
load their assets from a public CDN, which a lab machine on an isolated LAN cannot
reach and an operator's browser should not be asked to fetch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from meshforge import __version__
from meshforge.controller.api import (
    FABRIC_STATE_KEY,
    FabricView,
    install_error_handlers,
    router,
)
from meshforge.controller.registry import Registry
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager

__all__ = ["create_http_app"]

OPENAPI_URL = "/v1/openapi.json"


def create_http_app(
    manager: SessionManager,
    *,
    registry: Registry,
    resources: ResourceTracker,
    clock: Callable[[], float],
) -> FastAPI:
    """Build the controller's HTTP app. ``clock`` must be the loop's monotonic clock."""
    app = FastAPI(
        title="MeshForge controller",
        version=__version__,
        summary="Read-only view of the compute fabric (V0.3; unauthenticated, LAN only).",
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
    )
    setattr(
        app.state,
        FABRIC_STATE_KEY,
        FabricView(
            registry=registry, manager=manager, resources=resources, clock=clock
        ),
    )
    install_error_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, Any]:
        """Liveness probe: the process is up; ``sessions`` = live agent streams."""
        return {"status": "ok", "sessions": len(manager)}

    app.include_router(router)
    return app
