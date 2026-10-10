"""Tester support (V0.7 G3): certificates signed by the **real** fixture CA but
shaped wrongly - what a bug in issuance, a stolen server key or a second
fabric sharing a CA would produce. TLS accepts the chain; every plane's
identity rules must still refuse them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import grpc
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from mf_fabric import ca_pem, fabric_ca

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id

__all__ = ["CLIENT", "SERVER", "Forged", "forge", "grpc_credentials"]

CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH
SERVER = ExtendedKeyUsageOID.SERVER_AUTH


@dataclass(frozen=True)
class Forged:
    node_id: str
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def key_pem(self) -> bytes:
        return pki.key_to_pem(self.key)

    @property
    def cert_pem(self) -> bytes:
        return pki.cert_to_pem(self.cert)

    @property
    def serial(self) -> str:
        return format(self.cert.serial_number, "x")


def forge(
    *,
    usages: list[x509.ObjectIdentifier] | None = None,
    node_id: str | None = None,
    fabric_id: str | None = None,
    sans: list[x509.GeneralName] | None = None,
    now: dt.datetime | None = None,
    lifetime: dt.timedelta = dt.timedelta(days=1),
    ca: pki.CertificateAuthority | None = None,
) -> Forged:
    """A leaf signed by ``ca`` (default: the fixture fabric CA).

    ``fabric_id`` other than the CA's puts a foreign fabric in the node URI
    (a second fabric that shares the CA key)."""
    authority = ca or fabric_ca()
    node = node_id or str(new_node_id())
    key = pki.generate_key()
    uri = pki.node_uri(fabric_id or authority.fabric_id, node)
    cert = pki._sign_leaf(
        authority,
        subject=pki._name(node, authority.fabric_id),
        public_key=key.public_key(),
        sans=sans if sans is not None else [x509.UniformResourceIdentifier(uri)],
        usages=usages if usages is not None else [CLIENT, SERVER],
        now=now,
        lifetime=lifetime,
    )
    return Forged(node, key, cert)


def other_fabric_id() -> str:
    return new_fabric_id()


def grpc_credentials(forged: Forged) -> grpc.ChannelCredentials:
    return grpc.ssl_channel_credentials(
        root_certificates=ca_pem(),
        private_key=forged.key_pem,
        certificate_chain=forged.cert_pem,
    )
