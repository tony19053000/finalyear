"""Trust on first contact, safely: read a server's certificate chain without
trusting it, and keep only the fabric CA a fingerprint names (D136, V0.7 G4).

Two callers, one rule:

* ``meshforge-agent join`` pins the CA with the 128-bit fingerprint its join
  code carries (:mod:`meshforge.agent.enrollment`);
* ``compute login --fingerprint`` pins it with the full 256-bit fingerprint
  the admin reads from ``meshforge-controller fabric show`` (D141).

:func:`presented_chain` does one TLS handshake with verification **off**, reads
the chain the server sent and closes the socket - nothing is written after the
handshake, so a fake server learns only that someone connected.
:func:`pinned_ca` then accepts a certificate only if it matches the fingerprint
(constant time), is a self-signed CA, is valid, **and issued the server's own
certificate** - a server replaying the real CA certificate beside its own key
is refused.
"""

from __future__ import annotations

import _ssl  # 3.12: get_unverified_chain() yields _ssl.Certificate (D153 item 5)
import contextlib
import socket
import ssl
from collections.abc import Sequence
from typing import Any, Final

from cryptography import x509

from meshforge.common import pki

__all__ = [
    "MAX_CHAIN",
    "chain_der",
    "is_self_signed_ca",
    "pinned_ca",
    "presented_chain",
]

MAX_CHAIN: Final = 8
"""At most this many certificates are read from a presented chain."""


def chain_der(sock: Any) -> list[bytes] | None:
    """The certificate chain the peer sent, unverified, as DER; ``None`` when
    this Python cannot read it.

    Python 3.13+ has ``SSLSocket.get_unverified_chain()``; 3.10-3.12 expose the
    same call on the private ``_sslobj`` (3.12 is what we pin). The bytes are
    only ever compared with a pin, never trusted as they are.
    """
    getter = getattr(sock, "get_unverified_chain", None)
    if getter is None:
        getter = getattr(getattr(sock, "_sslobj", None), "get_unverified_chain", None)
    if getter is None:
        return None
    out: list[bytes] = []
    for item in (getter() or [])[:MAX_CHAIN]:
        if isinstance(item, bytes):
            out.append(item)
        else:  # 3.12: _ssl.Certificate
            out.append(item.public_bytes(_ssl.ENCODING_DER))
    return out


def presented_chain(
    host: str,
    port: int,
    *,
    timeout_s: float,
    alpn: Sequence[str] = (),
    server_hostname: str = pki.SERVER_NAME,
) -> list[x509.Certificate]:
    """The certificates a TLS server presents (verification OFF).

    Raises ``OSError`` (unreachable), ``ssl.SSLError`` (no TLS handshake) or
    ``RuntimeError`` (this Python cannot read a chain).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # untrusted read: checked against the pin
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if alpn:
        context.set_alpn_protocols(list(alpn))
    with (
        socket.create_connection((host, port), timeout=timeout_s) as raw,
        context.wrap_socket(raw, server_hostname=server_hostname) as tls,
    ):
        der = chain_der(tls)
    if der is None:  # pragma: no cover - every supported Python has one
        raise RuntimeError("this Python cannot read a server's certificate chain")
    certs: list[x509.Certificate] = []
    for item in der:
        with contextlib.suppress(ValueError):
            certs.append(x509.load_der_x509_certificate(item))
    return certs


def is_self_signed_ca(cert: x509.Certificate) -> bool:
    """A CA certificate (BasicConstraints ca=True) that signed itself."""
    try:
        bc = pki.cert_extensions(cert).get_extension_for_class(x509.BasicConstraints)
        pki.verify_issued_by(cert, cert)
    except (pki.PkiError, x509.ExtensionNotFound):
        return False
    return bool(bc.value.ca)


def pinned_ca(
    presented: Sequence[x509.Certificate], fingerprint: str, *, require_full: bool
) -> x509.Certificate | None:
    """The presented CA that matches ``fingerprint``, is valid and issued the
    server's certificate (``presented[0]``); else ``None`` (send nothing)."""
    for cert in presented:
        if pki.fingerprint_matches(
            cert, fingerprint, require_full=require_full
        ) and is_self_signed_ca(cert):
            try:
                pki.verify_issued_by(presented[0], cert)
                pki.check_validity(cert)
            except pki.PkiError:
                # The pinned CA is present but did not issue the server's own
                # certificate (or has expired): a server replaying the real
                # CA certificate beside an attacker's server cert. Stop - a
                # second certificate with the same pin cannot exist short of a
                # SHA-256 second preimage, so there is nothing more to look for
                # (D153 item 4).
                return None
            return cert
    return None
