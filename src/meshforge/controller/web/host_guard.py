"""Host allow-list for the plain-HTTP loopback listener (V0.8 G1, D182 O-1).

``[controller.http] loopback_plain`` serves the API and dashboard over plain
HTTP on ``127.0.0.1`` / ``[::1]`` only. A loopback socket alone does not stop
**DNS rebinding**: a hostile page on ``http://evil.example:47111`` can rebind
its name to ``127.0.0.1`` and then talk to this port as a *same-origin*
page of ``evil.example``. The browser still sends ``Host: evil.example:47111``
- so every request (HTTP and WebSocket) whose ``Host`` is not one of the
loopback names, optionally with this listener's own port, is refused before
it reaches the app. A missing or repeated ``Host`` is refused too (fail
closed). The LAN HTTPS port does not need this: its certificate names the
controller, a rebound name fails TLS.
"""

from __future__ import annotations

import json
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.errors import error_body
from meshforge.controller.web.spa import SECURITY_HEADERS

__all__ = ["LOOPBACK_HOST_NAMES", "LoopbackHostGuard", "allowed_host"]

log = get_logger(__name__)

LOOPBACK_HOST_NAMES: Final = frozenset({"localhost", "127.0.0.1", "[::1]"})
_MAX_HOST_CHARS: Final = 64
_REFUSED: Final = json.dumps(
    error_body(
        FailureType.INVALID_INPUT,
        "this listener only answers Host localhost, 127.0.0.1 or [::1]",
    ),
    separators=(",", ":"),
).encode("ascii")


def allowed_host(host: str, port: int) -> bool:
    """Is ``host`` (a ``Host`` header value) a loopback name, with no port or
    exactly ``port``? Case-insensitive; nothing else (no trailing dot, no
    userinfo, no other port). The port must be the exact decimal digits of
    ``port`` - no leading zeros, sign, spaces or non-ASCII digits (D184)."""
    if not host or len(host) > _MAX_HOST_CHARS:
        return False
    host = host.lower()
    if host in LOOPBACK_HOST_NAMES:
        return True
    name, sep, port_text = host.rpartition(":")
    return bool(sep) and name in LOOPBACK_HOST_NAMES and port_text == str(port)


class LoopbackHostGuard:
    """ASGI wrapper: 400 ``invalid_input`` (HTTP) / close 1008 before accept
    (WebSocket) unless the single ``Host`` header passes :func:`allowed_host`."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.port = port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hosts = [v for k, v in scope.get("headers", ()) if k.lower() == b"host"]
        try:
            ok = len(hosts) == 1 and allowed_host(hosts[0].decode("ascii"), self.port)
        except UnicodeDecodeError:
            ok = False
        if ok:
            await self.app(scope, receive, send)
            return
        # The value itself is attacker-chosen: count it, never log it.
        log.warning("controller.http.loopback_host_refused", kind=scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(_REFUSED)).encode("ascii")),
            *((k.encode("latin-1"), v.encode("latin-1")) for k, v in SECURITY_HEADERS),
        ]
        await send({"type": "http.response.start", "status": 400, "headers": headers})
        await send({"type": "http.response.body", "body": _REFUSED})
