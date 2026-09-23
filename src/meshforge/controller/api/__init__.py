"""Read-only REST surface of the controller (``/v1``, roadmap 0.3.12).

See :mod:`meshforge.controller.api.routes` for the trust boundary (D14) and
:mod:`meshforge.controller.api.models` for the three liveness notions (D16).
"""

from __future__ import annotations

from meshforge.controller.api.context import FABRIC_STATE_KEY, FabricView
from meshforge.controller.api.errors import ApiError, install_error_handlers
from meshforge.controller.api.routes import router

__all__ = [
    "FABRIC_STATE_KEY",
    "ApiError",
    "FabricView",
    "install_error_handlers",
    "router",
]
