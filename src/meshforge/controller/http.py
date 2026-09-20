"""REST surface of the controller (FastAPI). V0.3 G2: ``GET /healthz`` only.

``/v1/nodes`` and friends arrive with roadmap 0.3.12 (G5). The app is built by a
factory so tests can mount it on an ephemeral port with their own manager.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from meshforge import __version__
from meshforge.controller.session.manager import SessionManager

__all__ = ["create_http_app"]


def create_http_app(manager: SessionManager) -> FastAPI:
    app = FastAPI(
        title="MeshForge controller",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Liveness probe: the process is up; ``sessions`` = live agent streams."""
        return {"status": "ok", "sessions": len(manager)}

    return app
