"""HTTPS on the controller's REST + blob port (V0.7 G3, D139).

uvicorn serves the port with :meth:`~meshforge.controller.server_tls.ServerTls.ssl_context`
(TLS 1.3 only, the renewable server certificate, client certificates
**optional** - the CLI has none, agents present their node certificate). A
presented certificate is verified against the fabric CA during the handshake;
uvicorn does not hand it to the application, so :func:`peer_cert_protocol`
returns uvicorn's own HTTP protocol class with one addition: when a connection
is made, the client certificate (DER) is copied into that connection's
``app_state``, which uvicorn puts into every request's ``scope["state"]``
(:data:`TLS_PEER_STATE_KEY`). Nothing is shared between connections.

:func:`request_node_cert` turns it into a checked
:class:`~meshforge.controller.session.peer_cert.PeerCert` (same rules as the
session port: our fabric, ``clientAuth``, valid now, not revoked).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ssl
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Final

from starlette.requests import HTTPConnection
from uvicorn.protocols.http.auto import AutoHTTPProtocol

from meshforge.controller.session.peer_cert import PeerCert, parse_peer

__all__ = [
    "TLS_PEER_STATE_KEY",
    "NodeCertPolicy",
    "peer_cert_protocol",
    "request_node_cert",
]

TLS_PEER_STATE_KEY: Final = "meshforge_tls_peer_der"


def peer_cert_protocol() -> type[asyncio.Protocol]:
    """uvicorn's HTTP protocol class that records the TLS client certificate."""
    base: Any = AutoHTTPProtocol

    class _PeerCertProtocol(base):  # type: ignore[misc]
        def connection_made(self, transport: asyncio.Transport) -> None:
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
            if der:
                # Per connection: a copy, so no other connection ever sees it.
                state: dict[str, Any] = getattr(self, "app_state", {})
                self.app_state = {**state, TLS_PEER_STATE_KEY: der}

    return _PeerCertProtocol


@dataclass(frozen=True, slots=True)
class NodeCertPolicy:
    """How the API checks a request's node certificate (mtls mode only)."""

    fabric_id: str
    revoked: Callable[[], Collection[str]] = frozenset
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)


def request_node_cert(conn: HTTPConnection, policy: NodeCertPolicy) -> PeerCert:
    """The request's verified node certificate, or :class:`PeerCertError`."""
    state = conn.scope.get("state") or {}
    der = state.get(TLS_PEER_STATE_KEY)
    pem = ssl.DER_cert_to_PEM_cert(der).encode("ascii") if der else None
    return parse_peer(
        pem, fabric_id=policy.fabric_id, now=policy.clock(), revoked=policy.revoked()
    )
