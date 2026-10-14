"""The agent's HTTPS clients (V0.7 G3, D139): towards the controller's blob API
and towards other agents' peer blob servers.

:func:`load_http_tls` builds one :func:`~meshforge.common.fabric_tls.client_context`
from this laptop's enrolment: trust only ``ca.crt``, present ``node.crt`` +
``node.key`` (the controller's blob API requires the node certificate next
to the fetch token / upload grant - two locks; a peer server requires it
outright), TLS 1.3 only. Every request then proves who answers before a byte
is sent: the controller by its ``meshforge-controller`` name, a peer by the
node id the controller named (:func:`peer_extensions`).

:meth:`AgentHttpTls.transport` makes a fresh httpx transport per client (an
``httpx.AsyncClient`` closes its transport when it closes); its connections
open through :mod:`meshforge.agent.http_connect`, so a cancelled request
cannot leave a socket open.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from typing import Any

import httpx

from meshforge.agent import enrollment
from meshforge.agent.http_connect import cancel_safe_transport
from meshforge.agent.identity import KEY_FILE_NAME, NODE_CERT_FILE_NAME, NodeIdentity
from meshforge.agent.session.tls import JOIN_HINT, SessionTlsError
from meshforge.common.fabric_tls import client_context, peer_server_name

__all__ = ["AgentHttpTls", "load_http_tls", "peer_extensions"]


@dataclass(frozen=True, slots=True)
class AgentHttpTls:
    """The agent's client TLS for the HTTP planes (no key in ``repr``)."""

    context: ssl.SSLContext = field(repr=False)

    def transport(self) -> httpx.AsyncHTTPTransport:
        return cancel_safe_transport(self.context)

    def reload(self, identity: NodeIdentity) -> None:
        """V0.7 G4 (D140): present the renewed certificate (or rotated key)
        on every new connection; clients made before keep theirs."""
        self.context.load_cert_chain(
            identity.path / NODE_CERT_FILE_NAME, identity.path / KEY_FILE_NAME
        )

    @property
    def scheme(self) -> str:
        return "https"


def peer_extensions(node_id: str) -> dict[str, Any]:
    """httpx request extensions demanding peer ``node_id`` (D139)."""
    return {"sni_hostname": peer_server_name(node_id)}


def load_http_tls(identity: NodeIdentity) -> AgentHttpTls:
    """From the stored enrolment; :class:`SessionTlsError` when there is none."""
    try:
        stored = enrollment.load(identity)
    except enrollment.JoinError as exc:
        raise SessionTlsError(exc.message, details=exc.details) from None
    if stored is None:
        raise SessionTlsError(
            f"this laptop is not enrolled in a fabric: {JOIN_HINT}",
            details={"reason": "not_enrolled"},
        )
    context = client_context(
        stored.ca_cert,
        cert_file=identity.path / NODE_CERT_FILE_NAME,
        key_file=identity.path / KEY_FILE_NAME,
    )
    return AgentHttpTls(context=context)
