"""Client-side TLS for the HTTP planes (V0.7 G3, D139): who may answer.

Every HTTPS client in MeshForge (the agent towards the controller's blob API
and towards peer blob servers, the CLI towards the REST API) uses a context
from :func:`client_context`:

* it trusts **only** the fabric CA (``ca.crt``), TLS 1.3 only;
* it may present a node certificate (agents: ``node.crt`` + ``node.key``);
* it does **not** use OpenSSL's host-name check, because the right identity
  is not the dialled address. Instead the handshake itself is extended
  (``SSLContext.sslobject_class`` / ``sslsocket_class``): right after
  ``do_handshake()`` succeeds - before a single request byte is written - the
  peer certificate must be either

  - **the controller**: a DNS SAN :data:`~meshforge.common.pki.SERVER_NAME`
    (``meshforge-controller``) and ``serverAuth`` - the default; or
  - **one named node**, when the caller dials with the server name
    :func:`peer_server_name` (``<node_id>.node.meshforge``, via httpx's
    ``sni_hostname`` request extension): the certificate's single
    ``meshforge://fabric/<fabric>/node/<node_id>`` URI must name exactly that
    node of this fabric.

  Node certificates carry no DNS SAN, so an enrolled laptop can never pass the
  controller check, and a peer can only be the node the controller named.

A failed check raises :class:`ssl.SSLCertVerificationError` from the
handshake: httpx reports ``ConnectError`` and nothing was sent (no token).
"""

from __future__ import annotations

import re
import ssl
from pathlib import Path
from typing import Any, Final

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from meshforge.common import pki

__all__ = [
    "PEER_NAME_SUFFIX",
    "client_context",
    "expected_identity",
    "peer_server_name",
]

PEER_NAME_SUFFIX: Final = ".node.meshforge"
_PEER_NAME: Final = re.compile(r"node-(?P<ulid>[0-9a-z]{26})\.node\.meshforge", re.I)


def peer_server_name(node_id: str) -> str:
    """The TLS server name a client uses to demand that peer ``node_id``
    (``node_01ABC...`` -> ``node-01abc....node.meshforge``: a valid DNS name)."""
    prefix, _, body = node_id.partition("_")
    return f"{prefix}-{body}".lower() + PEER_NAME_SUFFIX


def expected_identity(server_hostname: str | bytes | None) -> str | None:
    """``node_id`` when ``server_hostname`` names a peer, else ``None`` (the
    controller)."""
    if isinstance(server_hostname, bytes):
        server_hostname = server_hostname.decode("ascii", "replace")
    if not server_hostname:
        return None
    match = _PEER_NAME.fullmatch(server_hostname)
    if match is None:
        return None
    return f"node_{match['ulid'].upper()}"


def _check(der: bytes | None, server_hostname: str | bytes | None, fabric: str) -> None:
    if not der:
        raise ssl.SSLCertVerificationError("the server presented no certificate")
    cert = x509.load_der_x509_certificate(der)
    node = expected_identity(server_hostname)
    try:
        usages = pki.cert_extensions(cert).get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
    except (pki.PkiError, x509.ExtensionNotFound):
        usages = None
    if usages is None or ExtendedKeyUsageOID.SERVER_AUTH not in usages:
        raise ssl.SSLCertVerificationError("the server certificate is not for servers")
    if node is None:
        try:
            names = pki.cert_extensions(cert).get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        except (pki.PkiError, x509.ExtensionNotFound):
            names = []
        if pki.SERVER_NAME not in names:
            raise ssl.SSLCertVerificationError(
                f"the server is not the fabric controller (no {pki.SERVER_NAME} name)"
            )
        return
    try:
        found = pki.parse_node_id(cert, fabric_id=fabric)
    except pki.PkiError:
        found = None
    if found != node:
        raise ssl.SSLCertVerificationError(
            f"the peer is not the node the controller named ({node})"
        )


class _FabricSSLObject(ssl.SSLObject):
    """Async clients (anyio ``wrap_bio``): identity check right after the handshake."""

    def do_handshake(self) -> None:
        super().do_handshake()
        _check(
            self.getpeercert(binary_form=True),
            self.server_hostname,
            self.context.meshforge_fabric_id,  # type: ignore[attr-defined]
        )


class _FabricSSLSocket(ssl.SSLSocket):
    """Sync clients (``wrap_socket``): the same check."""

    def do_handshake(self, block: bool = False) -> None:
        super().do_handshake(block)
        _check(
            self.getpeercert(binary_form=True),
            self.server_hostname,
            self.context.meshforge_fabric_id,  # type: ignore[attr-defined]
        )


def client_context(
    ca_cert: x509.Certificate,
    *,
    cert_file: Path | None = None,
    key_file: Path | None = None,
) -> ssl.SSLContext:
    """A client context for this fabric (module docstring). ``cert_file`` /
    ``key_file``: the node certificate to present (agents)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_verify_locations(cadata=pki.cert_to_pem(ca_cert).decode("ascii"))
    context.check_hostname = False  # replaced by _check (module docstring)
    context.verify_mode = ssl.CERT_REQUIRED
    if cert_file is not None and key_file is not None:
        context.load_cert_chain(cert_file, key_file)
    context.set_alpn_protocols(["http/1.1"])
    fabric: Any = pki.parse_fabric_id(ca_cert)
    context.meshforge_fabric_id = fabric  # type: ignore[attr-defined]
    context.sslobject_class = _FabricSSLObject
    context.sslsocket_class = _FabricSSLSocket
    return context
