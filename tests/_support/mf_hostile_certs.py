"""Certificates that verify but carry extensions ``cryptography`` refuses to parse.

``cryptography``'s builder will not produce them, so they are made by patching the
DER of a normally built certificate's *TBS* part and re-signing it with the real
issuer key - the signature is valid, so every check that runs before the
extensions are parsed passes (V0.7 D149b, reviewer N-1):

* :func:`duplicate_extension` - two extensions with the same OID
  (``x509.DuplicateExtension`` on ``cert.extensions``);
* :func:`unsupported_general_name` - a SAN entry of a type cryptography does not
  support (an ``x400Address``: ``x509.UnsupportedGeneralNameType``).
"""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

__all__ = ["duplicate_extension", "resign", "unsupported_general_name"]

_ECDSA_SHA256_ALG = bytes.fromhex("300a06082a8648ce3d040302")
_SKI_OID = b"\x06\x03\x55\x1d\x0e"  # 2.5.29.14 subjectKeyIdentifier
_SAN_OID = b"\x06\x03\x55\x1d\x11"  # 2.5.29.17 subjectAltName
_BC_OID = b"\x06\x03\x55\x1d\x13"  # 2.5.29.19 basicConstraints


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def resign(tbs: bytes, key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    """A certificate from raw TBS bytes, signed ECDSA-SHA256 by ``key``."""
    signature = key.sign(tbs, ec.ECDSA(hashes.SHA256()))
    bits = b"\x03" + _der_len(len(signature) + 1) + b"\x00" + signature
    body = tbs + _ECDSA_SHA256_ALG + bits
    return x509.load_der_x509_certificate(b"\x30" + _der_len(len(body)) + body)


def duplicate_extension(
    cert: x509.Certificate,
    key: ec.EllipticCurvePrivateKey,
    *,
    target: str = "san",
) -> x509.Certificate:
    """``cert`` with its subjectKeyIdentifier renamed to a second SAN (``target
    ="san"``) or a second basicConstraints (``"bc"``), re-signed by ``key``."""
    tbs = cert.tbs_certificate_bytes
    assert tbs.count(_SKI_OID) == 1, "needs exactly one subjectKeyIdentifier"
    oid = {"san": _SAN_OID, "bc": _BC_OID}[target]
    assert tbs.count(oid) == 1
    return resign(tbs.replace(_SKI_OID, oid), key)


def unsupported_general_name(
    cert: x509.Certificate, key: ec.EllipticCurvePrivateKey, dns_name: str
) -> x509.Certificate:
    """``cert`` with its SAN DNS entry ``dns_name`` re-tagged as an x400Address."""
    tbs = cert.tbs_certificate_bytes
    encoded = dns_name.encode("ascii")
    entry = b"\x82" + _der_len(len(encoded)) + encoded
    at = tbs.find(entry)
    assert at >= 0, "the DNS SAN entry is not in the certificate"
    return resign(tbs[:at] + b"\xa3" + tbs[at + 1 :], key)


def as_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)
