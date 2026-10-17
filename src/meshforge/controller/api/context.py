"""What every ``/v1`` handler is allowed to touch, and how it gets there.

:class:`FabricView` is a **read-only** view of the running controller: the registry
(one statement per call), the in-memory session table, the live resource rings and
the controller's monotonic clock - the same ``loop.time`` the session manager and
the leases use, which is what makes ``sample_age_s`` comparable with them (D16).

It is put on ``app.state`` by :func:`meshforge.controller.http.create_http_app` and
read back through a FastAPI dependency, so tests can mount the routers on a
hand-built view with no gRPC server and no database.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from meshforge.controller.registry import Registry
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager

if TYPE_CHECKING:
    from meshforge.controller.cache_index import CacheLocationStore

__all__ = ["FABRIC_STATE_KEY", "FabricView", "get_fabric"]

FABRIC_STATE_KEY = "meshforge_fabric"


@dataclass(frozen=True, slots=True)
class FabricView:
    """Read-only handles the API layer may use. No mutating method is exposed."""

    registry: Registry
    manager: SessionManager
    resources: ResourceTracker
    clock: Callable[[], float]
    """The controller's **monotonic** clock (``loop.time``), never a wall clock."""
    cache: CacheLocationStore | None = None
    """V0.5 D101: the cache location index (node detail counts); None = absent."""
    wall_clock: Callable[[], float] = time.time
    """The controller's **wall** clock in epoch seconds - only ``GET /v1/clock``
    answers it (V0.9 G1, D226 item 0.4: injected here, not a module variable,
    so a test or a second app in one process can give it its own source)."""


def get_fabric(request: Request) -> FabricView:
    view: FabricView = getattr(request.app.state, FABRIC_STATE_KEY)
    return view
