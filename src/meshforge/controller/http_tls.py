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

from meshforge.common.errors import ConfigError
from meshforge.controller.session.peer_cert import PeerCert, parse_peer

__all__ = [
    "TLS_PEER_STATE_KEY",
    "BlobPortSecurity",
    "NodeCertPolicy",
    "blob_port_security",
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


@dataclass(frozen=True, slots=True)
class BlobPortSecurity:
    """V0.8 G1 (D168 N2): **one** value says whether the REST + blob port
    speaks TLS, and every decision that depends on it reads it here - the
    listener's TLS options, the URL scheme the port is reached by, and the
    D139 node-certificate lock on agent blob credentials. Two flags that could
    disagree were how an ``insecure-dev`` controller with a fabric CA demanded
    client certificates on plain HTTP (V0.7 G7, ``2aa2caa``)."""

    blob_port_is_tls: bool

    @property
    def scheme(self) -> str:
        return "https" if self.blob_port_is_tls else "http"

    def node_cert_policy(
        self, fabric_id: str | None, revoked: Callable[[], Collection[str]]
    ) -> NodeCertPolicy | None:
        """The D139 lock - only on a TLS port (plain HTTP carries no client
        certificate) and only with a fabric to check it against."""
        if not self.blob_port_is_tls or fabric_id is None:
            return None
        return NodeCertPolicy(fabric_id=fabric_id, revoked=revoked)

    def check_url(self, url: str | None, *, setting: str) -> None:
        """A configured URL of this port must use :attr:`scheme` (an agent told
        ``https://`` for a plain port, or the reverse, could never fetch)."""
        if url is None:
            return
        if not url.lower().startswith(f"{self.scheme}://"):
            raise ConfigError(
                f"{setting} must be {self.scheme}:// - the REST + blob port "
                f"{'serves TLS' if self.blob_port_is_tls else 'is plain HTTP'} "
                f"here, got {url}",
                details={"reason": "blob_url_scheme", "scheme": self.scheme},
            )


def blob_port_security(*, insecure: bool, server_tls_ready: bool) -> BlobPortSecurity:
    """TLS exactly when not ``insecure-dev`` - and then a server certificate is
    required (fail closed: ``mtls`` never falls back to plain HTTP)."""
    if not insecure and not server_tls_ready:
        raise ConfigError(
            "[security] mode = 'mtls' needs the fabric CA's server certificate "
            "for the REST + blob port (run `meshforge-controller fabric create`)",
            details={"reason": "no_server_cert"},
        )
    return BlobPortSecurity(blob_port_is_tls=not insecure)
