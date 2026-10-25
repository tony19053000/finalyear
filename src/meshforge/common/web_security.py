"""The browser security headers every MeshForge web UI sends (ADR-0017, D181).

One definition for the controller's dashboard (``controller/web/spa.py``) and
the agent's local console (``agent/console/app.py``, V0.17 G3): a strict
Content-Security-Policy (``default-src 'self'``, no inline script or style,
``frame-ancestors 'none'``), ``nosniff``, no referrer, no framing. The frontend
build checks its output against the CSP (``frontend/scripts/check-dist.mjs``).
Pure constants: nothing here imports a web framework.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CSP", "SECURITY_HEADERS"]

CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS: Final = (
    ("content-security-policy", CSP),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
)
