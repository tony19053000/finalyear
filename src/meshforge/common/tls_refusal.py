"""A server refused our client certificate: recognise it and say so (V1.0 G8 F1).

With TLS 1.3 the client finishes its handshake **before** the server has
checked the client certificate. A server that refuses it (another fabric's
CA, an expired certificate) is meant to answer with a TLS alert
(``unknown_ca``, ``certificate_expired``); the gRPC ports do. Python's asyncio
servers (the controller's HTTP port, the agents' peer blob servers) usually
drop that alert and just close the connection, so the client sees a bare EOF,
``BrokenPipeError`` or ``ConnectionResetError`` on its first request - which
reads like a network fault.

This module is the one place that decides "that was a certificate refusal"
and words it; the transports raise or carry the evidence:

* :class:`CertRefused` - a mixin marking an exception as a refusal. The
  agent's HTTPS transport (:mod:`meshforge.agent.http_refusal`) raises one
  when a connection that presented a client certificate is closed or reset
  after a successful handshake **before the server sent a single byte**, or
  when the alert does arrive;
* :func:`find_refusal` - looks through an exception's cause/context chain for
  that marker, or for a raw :class:`ssl.SSLError` carrying one of the alerts
  in :data:`REFUSAL_ALERTS` (e.g. a gRPC-style server that did send it);
* :func:`refusal_message` - the user-facing text with the hint.

Errors before or without a handshake (connection refused, DNS, timeouts) are
never refusals: they carry no marker and no such alert.
"""

from __future__ import annotations

import ssl
from typing import Final

__all__ = [
    "CERT_REFUSED_HINT",
    "CLOSED_AFTER_HANDSHAKE",
    "REFUSAL_ALERTS",
    "CertRefused",
    "find_refusal",
    "refusal_message",
]

CLOSED_AFTER_HANDSHAKE: Final = "closed_after_handshake"
""":attr:`CertRefused.reason` when no alert arrived, only an EOF/EPIPE/reset."""

REFUSAL_ALERTS: Final = frozenset(
    {
        "TLSV1_ALERT_UNKNOWN_CA",
        "SSLV3_ALERT_CERTIFICATE_EXPIRED",
        "SSLV3_ALERT_BAD_CERTIFICATE",
        "SSLV3_ALERT_CERTIFICATE_UNKNOWN",
        "SSLV3_ALERT_CERTIFICATE_REVOKED",
        "SSLV3_ALERT_UNSUPPORTED_CERTIFICATE",
        "TLSV13_ALERT_CERTIFICATE_REQUIRED",
        "TLSV1_ALERT_ACCESS_DENIED",
    }
)
"""OpenSSL reason codes of the alerts a server sends when it will not accept
the client certificate (``ssl.SSLError.reason``)."""

CERT_REFUSED_HINT: Final = (
    " - wrong fabric or expired certificate? Check `meshforge-agent identity "
    "show` (fabric and expiry); re-join with a new code from this fabric's "
    "admin if this laptop belongs to another fabric"
)

_MAX_CHAIN: Final = 16
"""Exception chains are walked at most this deep (cycles are possible)."""


class CertRefused(Exception):  # noqa: N818 - a mixin marker, not a standalone error
    """Mixin: this exception means the server refused our client certificate.

    ``server`` - who refused (``"the controller"``, ``"peer node node_..."``);
    ``reason`` - the OpenSSL alert reason, or :data:`CLOSED_AFTER_HANDSHAKE`.
    """

    server: str = "the server"
    reason: str = CLOSED_AFTER_HANDSHAKE

    def mark_refused(self, *, server: str, reason: str) -> None:
        self.server = server
        self.reason = reason


class _AlertRefusal(CertRefused):
    """A refusal synthesised from a raw alert found in a chain."""


def _alert_reason(exc: BaseException) -> str | None:
    if isinstance(exc, ssl.SSLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and reason in REFUSAL_ALERTS:
            return reason
    return None


def find_refusal(
    exc: BaseException, *, server: str = "the server"
) -> CertRefused | None:
    """The :class:`CertRefused` behind ``exc`` (itself, its causes or
    contexts), or one made from a refusal alert there; else ``None``.

    ``server``: who the caller was talking to (``"the controller"``,
    ``"peer node node_..."``), used to word a refusal made from a raw alert;
    a marker raised by a transport keeps its own label."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(current))
        if isinstance(current, CertRefused):
            return current
        alert = _alert_reason(current)
        if alert is not None:
            refusal = _AlertRefusal(alert)
            refusal.mark_refused(server=server, reason=alert)
            return refusal
        current = current.__cause__ or current.__context__
    return None


def refusal_message(refusal: CertRefused) -> str:
    """One line: who refused, what was seen, and what to do."""
    if refusal.reason == CLOSED_AFTER_HANDSHAKE:
        seen = (
            "closed the connection right after the TLS handshake without "
            "answering: it most likely refused this laptop's certificate"
        )
    else:
        seen = f"refused this laptop's certificate (TLS alert {refusal.reason})"
    return f"{refusal.server} {seen}{CERT_REFUSED_HINT}"
