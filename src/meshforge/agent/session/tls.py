"""The agent's side of the mutual-TLS session (V0.7 G3, 0.7.5, D137).

:func:`load_session_tls` reads the enrolment ``meshforge-agent join`` stored
beside ``node.key`` (:func:`meshforge.agent.enrollment.load`: strict, owner-only
files, the pair re-verified against each other and against the identity key)
and turns it into gRPC channel credentials:

* **trust**: only the fabric CA (``ca.crt``, pinned by the join code);
* **our identity**: ``node.crt`` + the ADR-0013 node key;
* **the controller's name**: every channel verifies the server certificate
  against the fixed SAN :data:`~meshforge.common.pki.SERVER_NAME`
  (``meshforge-controller``), whatever address was dialled - an mDNS-found or
  typed IP is fine because the certificate, not the address, is the trust.
  Node certificates carry only a ``meshforge://`` URI SAN (never a DNS name),
  so another enrolled laptop cannot pose as the controller even though its
  certificate is from the same CA.

Not enrolled, an unusable enrolment, or an expired certificate is a typed,
fatal :class:`SessionTlsError` (``config_error``) whose message is the exact
command to run - never a silent plaintext fallback.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

import grpc

from meshforge.agent import enrollment
from meshforge.agent.identity import NodeIdentity
from meshforge.common import pki
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "JOIN_HINT",
    "SessionTls",
    "SessionTlsError",
    "channel_options",
    "load_session_tls",
]

JOIN_HINT: Final = (
    "ask the fabric admin for a join code (`meshforge-controller fabric invite` "
    "on the controller laptop), then run `meshforge-agent join`"
)


class SessionTlsError(MeshForgeError):
    """This agent cannot open a mutual-TLS session (not enrolled, expired...)."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


@dataclass(frozen=True, slots=True)
class SessionTls:
    """Channel credentials for one connection attempt (no key in ``repr``)."""

    credentials: grpc.ChannelCredentials
    fabric_id: str
    serial: str
    not_after: dt.datetime


def channel_options() -> list[tuple[str, str]]:
    """Verify the controller as ``meshforge-controller`` (module docstring)."""
    return [("grpc.ssl_target_name_override", pki.SERVER_NAME)]


def load_session_tls(
    identity: NodeIdentity, *, now: dt.datetime | None = None
) -> SessionTls:
    """The credentials of the stored enrolment, or :class:`SessionTlsError`."""
    try:
        stored = enrollment.load(identity)
    except enrollment.JoinError as exc:  # invalid_enrollment: says how to fix
        raise SessionTlsError(exc.message, details=exc.details) from None
    if stored is None:
        raise SessionTlsError(
            f"this laptop is not enrolled in a fabric, and [security] mode = "
            f"'mtls' needs that: {JOIN_HINT}",
            details={"reason": "not_enrolled"},
        )
    current = now or dt.datetime.now(dt.UTC)
    try:
        pki.check_validity(stored.node_cert, current)
    except pki.PkiError:
        raise SessionTlsError(
            f"this laptop's node certificate is valid "
            f"{stored.node_cert.not_valid_before_utc.isoformat()} - "
            f"{stored.not_after.isoformat()}, not now; re-join: "
            "`meshforge-agent join --force` with a new code (check this "
            "laptop's clock first if the dates look wrong)",
            details={"reason": "cert_not_valid_now"},
        ) from None
    credentials = grpc.ssl_channel_credentials(
        root_certificates=pki.cert_to_pem(stored.ca_cert),
        private_key=pki.key_to_pem(identity.private_key),
        certificate_chain=pki.cert_to_pem(stored.node_cert),
    )
    return SessionTls(
        credentials=credentials,
        fabric_id=stored.fabric_id,
        serial=format(stored.node_cert.serial_number, "x"),
        not_after=stored.not_after,
    )
