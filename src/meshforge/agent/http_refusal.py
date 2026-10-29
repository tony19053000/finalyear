"""Tell "the server refused my certificate" apart from a network fault (V1.0 G8 F1).

Every agent HTTPS connection presents this laptop's node certificate
(:class:`~meshforge.agent.http_tls.AgentHttpTls`). Under TLS 1.3 a server
that refuses it (another fabric, expired) does so only after our handshake
has completed; the asyncio servers then close or reset the connection
instead of sending the alert (:mod:`meshforge.common.tls_refusal`). httpx
would report ``RemoteProtocolError`` / ``ReadError`` / ``WriteError`` - a
network fault to the reader.

:class:`RefusalWatchStream` wraps the TLS stream right after the handshake
(:meth:`~meshforge.agent.http_connect._CancelSafeStream.start_tls`). The
evidence is always a **read**: until the server has sent its first byte on
that connection,

* an EOF on read, or an httpcore ``ReadError`` (reset), becomes the same
  httpcore error type with the
  :class:`~meshforge.common.tls_refusal.CertRefused` marker - so httpx still
  raises the type it always did, and :func:`transport_failure` finds the
  marker in its ``__cause__``;
* a raw :class:`ssl.SSLError` on read (the alert did arrive; anyio lets it
  through httpcore unmapped, where it would look like a *local* ``OSError``)
  becomes an httpcore ``ReadError`` - marked if it is a refusal alert.

A **write** error is never marked by itself: a server may answer early (a
``413`` while a large upload is still being sent) and close, so our write
fails with EPIPE/reset although the certificate was accepted. httpcore
swallows a request write error and reads the response; that read decides -
the early answer's bytes mean "not a refusal", a zero-byte EOF/reset means
"refusal". If httpcore never reads, the error stays unmarked. A raw
``ssl.SSLError`` on write becomes an unmarked httpcore ``WriteError``.

**Large requests are never marked.** With an asyncio client a zero-byte read
does not always mean the server sent nothing: when the server answers early
and resets the connection while a large request body is still queued in our
own transport, the transport fails on its background write and drops the
connection *without reading* the answer the kernel already holds (measured:
an early ``413`` + abort during a 64 MiB PUT reads as a bare
``ConnectionResetError``, 10 of 10). So a zero-byte close counts as a refusal
only when everything we wrote on the connection is at most
:data:`SMALL_REQUEST_BYTES` (asyncio's default write high-water mark: such a
request has left our transport before we read). Every GET (bundle, manifest,
chunk fetch, probe) and small upload qualifies; a refused large upload is
reported as the plain network error it was before F1. In practice an agent
whose certificate is refused fails its fetches first, which do say why.

Intended behaviour change: a non-refusal raw ``ssl.SSLError`` on read/write
now surfaces as httpx ``ReadError`` / ``WriteError`` (a retryable network
error) instead of escaping httpx as an ``OSError``.

Once a byte has arrived the server accepted us (a later close is an ordinary
keep-alive or network event), so nothing is reinterpreted. Timeouts and
errors before the handshake (connection refused, DNS, handshake failures) are
never touched: they are not raised through this stream.

**Known heuristic limit (accepted, V1.0 G8 F1 D2).** A server that *accepted*
the certificate but closes or resets the connection before sending a single
byte (it crashed, was killed, or dropped the request) is indistinguishable on
the wire from an asyncio refusal, so it is reported as a refusal too. The
wording is hedged for that reason ("... without answering: it most likely
refused this laptop's certificate"), the original exception stays in the
chain, and the failure type and retry behaviour of the caller are unchanged.
"""

from __future__ import annotations

import ssl
from typing import Any, Final, NoReturn

import httpcore

from meshforge.common.fabric_tls import expected_identity
from meshforge.common.logging import get_logger
from meshforge.common.tls_refusal import (
    CLOSED_AFTER_HANDSHAKE,
    REFUSAL_ALERTS,
    CertRefused,
    find_refusal,
    refusal_message,
)

__all__ = [
    "CONTROLLER_LABEL",
    "SMALL_REQUEST_BYTES",
    "RefusalWatchStream",
    "RefusedProtocolError",
    "RefusedReadError",
    "server_label",
    "transport_failure",
]

log = get_logger(__name__)

CONTROLLER_LABEL: Final = "the controller"
"""Message label of the controller (:func:`server_label`)."""

SMALL_REQUEST_BYTES: Final = 64 * 1024
"""Requests up to this size (asyncio's default write high-water mark) are
fully handed to the kernel before we read; a larger one may still sit in our
transport when the server closes, and then an early answer can be lost
(module docstring)."""


class RefusedReadError(CertRefused, httpcore.ReadError):
    """httpx maps it to ``httpx.ReadError`` (cause: this)."""


class RefusedProtocolError(CertRefused, httpcore.RemoteProtocolError):
    """An EOF before any byte: httpx's usual ``RemoteProtocolError``, marked."""


def server_label(server_hostname: str | None) -> str:
    """``"peer node node_..."`` when the connection demanded a peer, else
    ``"the controller"`` (the TLS name rule of :mod:`~meshforge.common.fabric_tls`)."""
    node = expected_identity(server_hostname)
    return f"peer node {node}" if node is not None else CONTROLLER_LABEL


def transport_failure(exc: BaseException, *, server: str = "the server") -> str:
    """What an httpx transport failure was, for an error message: the
    certificate-refusal text when :func:`find_refusal` recognises one, else
    the exception's type name (the existing, terse wording). ``server``
    (:func:`server_label` / ``Source.label``) words a raw-alert refusal."""
    refusal = find_refusal(exc, server=server)
    if refusal is not None:
        return refusal_message(refusal)
    return type(exc).__name__


class RefusalWatchStream(httpcore.AsyncNetworkStream):
    """A TLS stream that marks a refusal (module docstring)."""

    def __init__(
        self, inner: httpcore.AsyncNetworkStream, server_hostname: str | None
    ) -> None:
        self._inner = inner
        self._server = server_label(server_hostname)
        self._answered = False
        # Cumulative per connection (every request on it, not just the
        # first): harmless, because the first response byte sets _answered
        # and after that nothing on this connection is reinterpreted.
        self._written = 0
        self._logged = False

    def _refused[E: CertRefused](
        self, error: type[E], reason: str, cause: BaseException
    ) -> NoReturn:
        marked = error(_message_for(self._server, reason))
        marked.mark_refused(server=self._server, reason=reason)
        if not self._logged:  # one warning per connection
            self._logged = True
            log.warning("agent.http.cert_refused", server=self._server, reason=reason)
        raise marked from cause

    def _read_ssl_error(self, exc: ssl.SSLError) -> NoReturn:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and reason in REFUSAL_ALERTS:
            self._refused(RefusedReadError, reason, exc)
        # Not a refusal: still a network error to httpx, not a local OSError.
        raise httpcore.ReadError(_tls_error_text(exc)) from exc

    def _closed_unanswered(self) -> bool:
        """A zero-byte close/reset may be read as a refusal: nothing was
        answered yet, and our request was small enough to have left our own
        transport (module docstring, "large requests")."""
        return not self._answered and self._written <= SMALL_REQUEST_BYTES

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        try:
            data = await self._inner.read(max_bytes, timeout)
        except ssl.SSLError as exc:
            self._read_ssl_error(exc)
        except httpcore.ReadError as exc:
            if not self._closed_unanswered() or isinstance(exc, CertRefused):
                raise
            self._refused(RefusedReadError, CLOSED_AFTER_HANDSHAKE, exc)
        if data:
            self._answered = True
        elif self._closed_unanswered():
            self._refused(
                RefusedProtocolError,
                CLOSED_AFTER_HANDSHAKE,
                ConnectionAbortedError("EOF before any response byte"),
            )
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self._written += len(buffer)
        try:
            await self._inner.write(buffer, timeout)
        except ssl.SSLError as exc:
            # Never marked here (module docstring): httpcore reads next, and
            # that read decides whether this was a refusal.
            raise httpcore.WriteError(_tls_error_text(exc)) from exc

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        # TLS inside TLS (an HTTPS proxy's CONNECT tunnel): the inner TLS
        # stream is the one that carries our requests, so it is watched too.
        inner = await self._inner.start_tls(ssl_context, server_hostname, timeout)
        return RefusalWatchStream(inner, server_hostname)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


def _tls_error_text(exc: ssl.SSLError) -> str:
    reason = getattr(exc, "reason", None)
    return f"TLS error: {reason or type(exc).__name__}"


def _message_for(server: str, reason: str) -> str:
    """:func:`refusal_message` for a refusal not yet raised."""
    probe = CertRefused()
    probe.mark_refused(server=server, reason=reason)
    return refusal_message(probe)
