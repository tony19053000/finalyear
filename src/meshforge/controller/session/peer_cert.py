"""Who is on the other end of a session stream: the client certificate (V0.7 G3,
0.7.5, D137).

The session port is mutual TLS: gRPC (BoringSSL) only completes a handshake
whose client certificate chains to the fabric CA and is inside its validity
window. :func:`peer_cert` then reads that certificate from the call's
``auth_context`` and derives the node it names - **the transport, not
``Hello.node_id``, is the identity** - and :class:`NodeCertInterceptor` refuses
every call on the port that does not carry one (defence in depth: the TLS
configuration already demands it).

Checks, each a typed :class:`PeerCertReason` (fail closed - anything missing,
unparseable or unexpected is a refusal):

* a certificate is present (``no_cert``) and parses (``malformed``);
* exactly one SAN URI ``meshforge://fabric/<our fabric>/node/<node_id>``
  (``wrong_fabric`` / ``malformed``);
* EKU ``clientAuth`` (``not_client``) - the controller's own server
  certificate is never a client identity;
* valid now (``expired``), with the controller's clock;
* its serial is not revoked (``revoked``; the set is the revocation store's).

Refusals end the call ``UNAUTHENTICATED`` (``PERMISSION_DENIED`` for
``revoked``) with trailing metadata ``meshforge-reason``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import grpc
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from meshforge.common import pki
from meshforge.common.logging import get_logger
from meshforge.controller.enrollment.service import public_key_fingerprint

__all__ = [
    "REASON_METADATA_KEY",
    "NodeCertInterceptor",
    "PeerCert",
    "PeerCertError",
    "PeerCertReason",
    "abort_with_reason",
    "peer_cert",
    "status_for",
]

log = get_logger(__name__)

REASON_METADATA_KEY: Final = "meshforge-reason"
_PEM_KEY: Final = "x509_pem_cert"


class PeerCertReason(StrEnum):
    NO_CERT = "no_cert"
    MALFORMED = "malformed_cert"
    WRONG_FABRIC = "wrong_fabric"
    NOT_CLIENT = "not_client_cert"
    EXPIRED = "cert_expired"
    REVOKED = "cert_revoked"
    NODE_ID_MISMATCH = "node_id_mismatch"
    """``Hello.node_id`` differs from the certificate's (checked by the servicer)."""
    CERT_SUPERSEDED = "cert_superseded"
    """The node was re-keyed (``invite --replace``) and this is its old
    certificate (checked at admission against the recorded serial)."""


class PeerCertError(Exception):
    """A call whose client certificate is not acceptable."""

    def __init__(self, reason: PeerCertReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def status_for(reason: PeerCertReason) -> grpc.StatusCode:
    if reason is PeerCertReason.REVOKED:
        return grpc.StatusCode.PERMISSION_DENIED
    return grpc.StatusCode.UNAUTHENTICATED


@dataclass(frozen=True, slots=True)
class PeerCert:
    """The verified client certificate of one call (no key material)."""

    node_id: str
    serial: str
    """Lower-case hex, as stored in ``node.cert_serial``."""
    fingerprint: str
    not_after: dt.datetime
    public_key_fingerprint: str


def _pem_of(context: Any) -> bytes | None:
    auth = context.auth_context() or {}
    values = auth.get(_PEM_KEY) or []
    return bytes(values[0]) if values else None


def parse_peer(
    pem: bytes | None,
    *,
    fabric_id: str,
    now: dt.datetime,
    revoked: Collection[str] = (),
) -> PeerCert:
    """The checks of the module docstring on a PEM leaf; raises
    :class:`PeerCertError`."""
    if not pem:
        raise PeerCertError(
            PeerCertReason.NO_CERT,
            "this port needs a fabric node certificate (meshforge-agent join)",
        )
    try:
        cert = pki.cert_from_pem(pem)
        node_id = pki.parse_node_id(cert, fabric_id=fabric_id)
    except pki.PkiError as exc:
        reason = (
            PeerCertReason.WRONG_FABRIC
            if exc.reason is pki.PkiReason.FABRIC_MISMATCH
            else PeerCertReason.MALFORMED
        )
        raise PeerCertError(
            reason, f"client certificate refused ({exc.reason.value})"
        ) from None
    try:
        usages = (
            pki.cert_extensions(cert)
            .get_extension_for_class(x509.ExtendedKeyUsage)
            .value
        )
    except (pki.PkiError, x509.ExtensionNotFound):
        usages = None
    if usages is None or ExtendedKeyUsageOID.CLIENT_AUTH not in usages:
        raise PeerCertError(
            PeerCertReason.NOT_CLIENT, "the certificate is not a client certificate"
        )
    try:
        pki.check_validity(cert, now)
    except pki.PkiError:
        raise PeerCertError(
            PeerCertReason.EXPIRED,
            "the node certificate is expired or not yet valid; re-join "
            "(meshforge-agent join --force <code>)",
        ) from None
    serial = format(cert.serial_number, "x")
    if serial in revoked:
        raise PeerCertError(PeerCertReason.REVOKED, "this node certificate was revoked")
    public = cert.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey):  # pragma: no cover
        raise PeerCertError(PeerCertReason.MALFORMED, "not an EC key")
    return PeerCert(
        node_id=str(node_id),
        serial=serial,
        fingerprint=pki.cert_fingerprint(cert),
        not_after=cert.not_valid_after_utc,
        public_key_fingerprint=public_key_fingerprint(public),
    )


def peer_cert(
    context: Any,
    *,
    fabric_id: str,
    now: dt.datetime | None = None,
    revoked: Collection[str] = (),
) -> PeerCert:
    """The verified identity of ``context``'s caller (see :func:`parse_peer`)."""
    return parse_peer(
        _pem_of(context),
        fabric_id=fabric_id,
        now=now or dt.datetime.now(dt.UTC),
        revoked=revoked,
    )


async def abort_with_reason(context: Any, error: PeerCertError) -> None:
    """End the call with the error's status and ``meshforge-reason``."""
    context.set_trailing_metadata(((REASON_METADATA_KEY, error.reason.value),))
    await context.abort(status_for(error.reason), error.message)


class NodeCertInterceptor(grpc.aio.ServerInterceptor):
    """Refuses every call without an acceptable node certificate, except the
    methods in ``allow_without_cert`` (full gRPC method paths).

    gRPC's interceptor API sees only the method name and metadata, so the
    check runs inside a wrapper around the real handler, where the call's
    ``context`` (and its ``auth_context``) exists.
    """

    def __init__(
        self,
        *,
        fabric_id: str,
        revoked: Callable[[], Collection[str]] = frozenset,
        allow_without_cert: Collection[str] = (),
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._fabric_id = fabric_id
        self._revoked = revoked
        self._allow = frozenset(allow_without_cert)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.refused = 0

    async def _check(self, context: Any, method: str) -> bool:
        try:
            peer_cert(
                context,
                fabric_id=self._fabric_id,
                now=self._clock(),
                revoked=self._revoked(),
            )
        except PeerCertError as error:
            self.refused += 1
            log.warning(
                "session.cert_refused",
                method=method,
                reason=error.reason.value,
                peer=context.peer(),
            )
            await abort_with_reason(context, error)
            return False
        return True

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        handler = await continuation(handler_call_details)
        method = handler_call_details.method
        if handler is None or method in self._allow:
            return handler
        return _guarded(handler, lambda ctx: self._check(ctx, method))


def _guarded(handler: Any, check: Callable[[Any], Any]) -> Any:
    """``handler`` with ``check(context)`` awaited before its behaviour."""
    kwargs = {
        "request_deserializer": handler.request_deserializer,
        "response_serializer": handler.response_serializer,
    }
    if handler.stream_stream is not None:
        inner_ss = handler.stream_stream

        async def stream_stream(request_iterator: Any, context: Any) -> Any:
            if not await check(context):
                return
            responses = inner_ss(request_iterator, context)
            try:
                async for response in responses:
                    yield response
            finally:
                await responses.aclose()

        return grpc.stream_stream_rpc_method_handler(stream_stream, **kwargs)
    if handler.unary_unary is not None:
        inner_uu = handler.unary_unary

        async def unary_unary(request: Any, context: Any) -> Any:
            if not await check(context):
                return None
            return await inner_uu(request, context)

        return grpc.unary_unary_rpc_method_handler(unary_unary, **kwargs)
    if handler.unary_stream is not None:
        inner_us = handler.unary_stream

        async def unary_stream(request: Any, context: Any) -> Any:
            if not await check(context):
                return
            responses = inner_us(request, context)
            try:
                async for response in responses:
                    yield response
            finally:
                await responses.aclose()

        return grpc.unary_stream_rpc_method_handler(unary_stream, **kwargs)
    inner_su = handler.stream_unary

    async def stream_unary(request_iterator: Any, context: Any) -> Any:
        if not await check(context):
            return None
        return await inner_su(request_iterator, context)

    return grpc.stream_unary_rpc_method_handler(stream_unary, **kwargs)
