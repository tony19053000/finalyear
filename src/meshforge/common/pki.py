"""Fabric PKI: the CA, certificates and CSRs (V0.7 D134/D136, ADR-0016, ADR-0008).

Pure functions on :mod:`cryptography` objects - no file or network I/O except the
explicit ``read_*`` / ``write_*`` helpers at the bottom, which follow the
private-file rules of ADR-0013 (:mod:`meshforge.common.private_files`). The
controller (CA owner) and, from G2, the agent (CSR author) both use it; it imports
nothing outside ``common``.

What the certificates look like (every key is **ECDSA P-256**, every signature
**ECDSA-SHA256** - BoringSSL in grpcio, Python ``ssl`` and browsers all accept it,
ADR-0013):

* **Fabric CA** (:func:`create_ca`, 10 years): self-signed; subject
  ``CN=MeshForge fabric CA, O=MeshForge, OU=<fabric_id>``;
  ``BasicConstraints(ca, path_length=0)`` and ``KeyUsage(keyCertSign, cRLSign,
  digitalSignature)``, both critical. No intermediate CA (D134).
* **Controller server cert** (:func:`issue_server_cert`, 1 year): SAN = the fixed
  name :data:`SERVER_NAME` plus the controller's configured hosts (DNS names / IP
  addresses); EKU ``serverAuth``; not a CA.
* **Node cert** (:func:`issue_node_cert`, 30 days): SAN = exactly one URI
  ``meshforge://fabric/<fabric_id>/node/<node_id>``, **no** DNS/IP names (a node
  certificate can never pass a hostname check for the controller); EKU
  ``clientAuth`` + ``serverAuth`` (the peer server uses it, D139); not a CA.

Rules that matter for security:

* A CSR is **hostile input** (:func:`verify_csr`): size-bounded before parsing; key
  must be P-256 (RSA, Ed25519, other curves refused), hash SHA-256, signature valid
  (the author holds the key), and exactly one SAN naming the expected node and
  fabric. The issued certificate is built **from scratch**: nothing in the CSR
  except its public key is copied (a CSR asking for ``CA:TRUE`` or extra names gets
  neither).
* Every refusal is a :class:`PkiError` carrying a :class:`PkiReason` code; messages
  never contain key material or the CSR bytes.
* Validity: ``not_valid_before`` is back-dated by :data:`CLOCK_SKEW` so a laptop
  whose clock is a few minutes behind accepts a fresh certificate; a leaf never
  outlives its CA (clamped). Times are wall-clock UTC (certificates are timestamps
  by nature), injected as ``now`` everywhere so tests control them.
* Fingerprints are SHA-256 over the DER encoding. :func:`short_fingerprint` is its
  first 128 bits (32 hex) - what a join code carries (D135: 2^128 second-preimage
  work); :func:`fingerprint_matches` compares in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import (
    ExitCode,
    FailureType,
    InvalidIdError,
    MeshForgeError,
)
from meshforge.common.ids import FabricId, NodeId, validate_fabric_id, validate_node_id
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    protect_private,
    read_bounded,
    read_private,
)

__all__ = [
    "CA_LIFETIME",
    "CLOCK_SKEW",
    "MAX_CERT_BYTES",
    "MAX_CSR_BYTES",
    "MAX_KEY_BYTES",
    "MAX_SERVER_SANS",
    "NODE_CERT_LIFETIME",
    "RENEW_FRACTION",
    "SERVER_CERT_LIFETIME",
    "SERVER_NAME",
    "CertificateAuthority",
    "IssuedCert",
    "PkiError",
    "PkiFileError",
    "PkiReason",
    "VerifiedCsr",
    "build_csr",
    "cert_extensions",
    "cert_fingerprint",
    "cert_from_pem",
    "cert_sans",
    "cert_to_pem",
    "check_validity",
    "create_ca",
    "csr_from_bytes",
    "fingerprint_matches",
    "format_fingerprint",
    "generate_key",
    "is_expired",
    "issue_node_cert",
    "issue_server_cert",
    "key_from_pem",
    "key_to_pem",
    "node_uri",
    "parse_fabric_id",
    "parse_node_id",
    "public_keys_equal",
    "read_cert",
    "read_private_key",
    "renewal_due",
    "short_fingerprint",
    "validate_san_host",
    "verify_csr",
    "verify_issued_by",
    "write_cert",
    "write_private_key",
]

CA_LIFETIME: Final = timedelta(days=3652)
"""10 years (D134)."""
SERVER_CERT_LIFETIME: Final = timedelta(days=365)
NODE_CERT_LIFETIME: Final = timedelta(days=30)
"""D136: short, so revocation is cheap and renewal (D140) routine."""
MAX_LEAF_LIFETIME: Final = timedelta(days=825)
CLOCK_SKEW: Final = timedelta(minutes=5)
"""``not_valid_before`` back-dating for laptops whose clocks lag."""
RENEW_FRACTION: Final = 2 / 3
"""Renew at two thirds of a certificate's life (D134 server, D140 node)."""
SERVER_NAME: Final = "meshforge-controller"
"""Fixed DNS SAN of every controller server cert: agents may verify against it
whatever address they dialled (mDNS, D137)."""
MAX_CSR_BYTES: Final = 4096
"""A P-256 CSR with one URI SAN is ~500 bytes PEM; 8x headroom."""
MAX_CERT_BYTES: Final = 16 * 1024
MAX_KEY_BYTES: Final = 4096
MAX_SERVER_SANS: Final = 64
_ORG: Final = "MeshForge"
_CA_COMMON_NAME: Final = "MeshForge fabric CA"
_URI_PREFIX: Final = "meshforge://fabric/"
_NODE_URI_RE: Final = re.compile(
    r"meshforge://fabric/(fab_[0-9A-Z]{26})/node/(node_[0-9A-Z]{26})"
)
_DNS_LABEL_RE: Final = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_FILE_MODE: Final = 0o600


class PkiReason(StrEnum):
    """Why a certificate, CSR or key was refused (stable codes for logs/tests)."""

    OVERSIZED = "oversized"
    MALFORMED = "malformed"
    WRONG_KEY_TYPE = "wrong_key_type"
    WRONG_CURVE = "wrong_curve"
    WRONG_HASH = "wrong_hash"
    BAD_SIGNATURE = "bad_signature"
    SAN_MISSING = "san_missing"
    SAN_COUNT = "san_count"
    SAN_MALFORMED = "san_malformed"
    NODE_MISMATCH = "node_mismatch"
    FABRIC_MISMATCH = "fabric_mismatch"
    NOT_ISSUED_BY_CA = "not_issued_by_ca"
    NOT_A_CA = "not_a_ca"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    KEY_MISMATCH = "key_mismatch"
    BAD_LIFETIME = "bad_lifetime"
    BAD_HOST = "bad_host"


class PkiError(MeshForgeError):
    """A certificate, CSR or key was refused. ``reason`` says why."""

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(self, reason: PkiReason, message: str) -> None:
        super().__init__(message, details={"reason": reason.value})
        self.reason = reason


class PkiFileError(PkiError):
    """A PKI file is unreadable, malformed or not private (operator must act)."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR

    def __init__(self, reason: PkiReason, message: str, *, path: Path) -> None:
        super().__init__(reason, message)
        self.details["path"] = str(path)


@dataclass(frozen=True, slots=True)
class CertificateAuthority:
    """The fabric CA: its key (never in ``repr``), certificate and ``fabric_id``."""

    key: ec.EllipticCurvePrivateKey = field(repr=False)
    cert: x509.Certificate
    fabric_id: FabricId


@dataclass(frozen=True, slots=True)
class IssuedCert:
    """A certificate with the private key it was issued for (server certs)."""

    key: ec.EllipticCurvePrivateKey = field(repr=False)
    cert: x509.Certificate


@dataclass(frozen=True, slots=True)
class VerifiedCsr:
    """What :func:`verify_csr` proved: the author holds ``public_key`` and asks
    for ``node_id`` in ``fabric_id``."""

    node_id: NodeId
    fabric_id: FabricId
    public_key: ec.EllipticCurvePublicKey


# ------------------------------------------------------------------ keys / encoding


def generate_key() -> ec.EllipticCurvePrivateKey:
    """A fresh ECDSA P-256 private key."""
    return ec.generate_private_key(ec.SECP256R1())


def key_to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    """Unencrypted PKCS#8 PEM (same format as ``node.key``, ADR-0013)."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def key_from_pem(data: bytes) -> ec.EllipticCurvePrivateKey:
    """Parse a P-256 private key; anything else is a :class:`PkiError`."""
    if len(data) > MAX_KEY_BYTES:
        raise PkiError(PkiReason.OVERSIZED, f"key larger than {MAX_KEY_BYTES} bytes")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise PkiError(
            PkiReason.MALFORMED, "not an unencrypted PEM private key"
        ) from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise PkiError(PkiReason.WRONG_KEY_TYPE, "private key is not ECDSA")
    if not isinstance(key.curve, ec.SECP256R1):
        raise PkiError(PkiReason.WRONG_CURVE, "private key is not on P-256")
    return key


def cert_to_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def cert_from_pem(data: bytes) -> x509.Certificate:
    """Parse one PEM certificate (size-bounded first)."""
    if len(data) > MAX_CERT_BYTES:
        raise PkiError(
            PkiReason.OVERSIZED, f"certificate larger than {MAX_CERT_BYTES} bytes"
        )
    try:
        return x509.load_pem_x509_certificate(data)
    except ValueError:
        raise PkiError(PkiReason.MALFORMED, "not a PEM certificate") from None


def public_keys_equal(a: ec.EllipticCurvePublicKey, b: object) -> bool:
    """Same public key (compared on the DER SubjectPublicKeyInfo)."""
    if not isinstance(b, ec.EllipticCurvePublicKey):
        return False
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    return hmac.compare_digest(
        a.public_bytes(serialization.Encoding.DER, fmt),
        b.public_bytes(serialization.Encoding.DER, fmt),
    )


# ------------------------------------------------------------------- fingerprints


def cert_fingerprint(cert: x509.Certificate) -> str:
    """Lower-case hex SHA-256 of the certificate's DER (64 characters)."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def short_fingerprint(cert: x509.Certificate) -> str:
    """The first 128 bits of :func:`cert_fingerprint` (32 hex) - join codes."""
    return cert_fingerprint(cert)[:32]


def format_fingerprint(hex_digest: str) -> str:
    """Group a hex fingerprint in fours for reading aloud: ``abcd ef01 ...``."""
    return " ".join(hex_digest[i : i + 4] for i in range(0, len(hex_digest), 4))


def fingerprint_matches(
    cert: x509.Certificate, expected: str, *, require_full: bool
) -> bool:
    """Constant-time check of ``cert`` against a typed/pasted fingerprint.

    ``expected`` is the 64-hex full form or - only when ``require_full`` is
    ``False`` - the 32-hex short form a join code carries (D135, D149d: a place
    that pins the CA for good never accepts the 128-bit form). Spaces, colons and
    case are ignored. Any other length or a non-hex character is ``False``.
    The keyword is required so every caller decides which form it accepts.
    """
    cleaned = re.sub(r"[\s:]", "", expected).lower()
    allowed = (64,) if require_full else (32, 64)
    if len(cleaned) not in allowed or not re.fullmatch(r"[0-9a-f]+", cleaned):
        return False
    actual = cert_fingerprint(cert)[: len(cleaned)]
    return hmac.compare_digest(actual.encode("ascii"), cleaned.encode("ascii"))


# ------------------------------------------------------------------- names / SANs


def node_uri(fabric_id: str, node_id: str) -> str:
    """The SAN URI that binds a node certificate to ``node_id`` (D136)."""
    validate_fabric_id(fabric_id)
    validate_node_id(node_id)
    return f"{_URI_PREFIX}{fabric_id}/node/{node_id}"


def _parse_node_uri(value: str) -> tuple[FabricId, NodeId]:
    match = _NODE_URI_RE.fullmatch(value)
    if match is None:
        raise PkiError(PkiReason.SAN_MALFORMED, "SAN is not a MeshForge node URI")
    try:
        return validate_fabric_id(match[1]), validate_node_id(match[2])
    except InvalidIdError:
        raise PkiError(
            PkiReason.SAN_MALFORMED, "SAN names an invalid fabric or node id"
        ) from None


def validate_san_host(host: str) -> x509.GeneralName:
    """A server-cert host as a SAN entry: an IP address or a DNS name.

    Wildcards, the unspecified addresses (``0.0.0.0``, ``::``) and anything that is
    not an RFC 1123 host name are refused (``bad_host``).
    """
    text = host.strip().removeprefix("[").removesuffix("]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        name = text.lower().rstrip(".")
        labels = name.split(".")
        if (
            not name
            or len(name) > 253
            or not all(_DNS_LABEL_RE.fullmatch(label) for label in labels)
        ):
            raise PkiError(
                PkiReason.BAD_HOST, f"{host!r} is not an IP address or host name"
            ) from None
        return x509.DNSName(name)
    if ip.is_unspecified:
        raise PkiError(
            PkiReason.BAD_HOST, f"{host!r} is a wildcard address, not a host"
        )
    if isinstance(ip, ipaddress.IPv6Address) and ip.scope_id:
        # A SAN cannot carry a zone id (``%eth0`` names an interface of *this*
        # host only); drop it so the issued SAN and the compared one are equal.
        ip = ipaddress.IPv6Address(int(ip))
    return x509.IPAddress(ip)


def cert_extensions(cert: x509.Certificate) -> x509.Extensions:
    """``cert.extensions``, or ``PkiError(malformed)`` when they cannot be parsed.

    :mod:`cryptography` parses extensions lazily and raises exceptions that are
    *not* ``ValueError`` for a duplicated extension (``DuplicateExtension``) or a
    SAN entry type it does not support (``UnsupportedGeneralNameType``, e.g. an
    x400Address) - a certificate that verifies can still carry them (D149b).
    """
    try:
        return cert.extensions
    except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
        raise PkiError(
            PkiReason.MALFORMED, "certificate extensions are malformed"
        ) from None


def cert_sans(cert: x509.Certificate) -> list[str]:
    """The DNS names, IP addresses and URIs in ``cert``'s SAN (empty if none).

    Raises ``PkiError(malformed)`` when the extensions cannot be parsed.
    """
    try:
        ext = cert_extensions(cert).get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    out: list[str] = []
    for name in ext.value:
        if isinstance(name, x509.IPAddress):
            out.append(str(name.value))
        elif isinstance(name, x509.DNSName | x509.UniformResourceIdentifier):
            out.append(name.value)
        else:
            out.append(f"<{type(name).__name__}>")
    return out


def _single_attr(name: x509.Name, oid: x509.ObjectIdentifier) -> str | None:
    attrs = name.get_attributes_for_oid(oid)
    if len(attrs) != 1 or not isinstance(attrs[0].value, str):
        return None
    return attrs[0].value


def parse_fabric_id(cert: x509.Certificate) -> FabricId:
    """The ``fabric_id`` in a fabric certificate's subject (``OU``)."""
    value = _single_attr(cert.subject, NameOID.ORGANIZATIONAL_UNIT_NAME)
    if value is None:
        raise PkiError(PkiReason.MALFORMED, "certificate names no single fabric id")
    try:
        return validate_fabric_id(value)
    except InvalidIdError:
        raise PkiError(
            PkiReason.MALFORMED, "certificate names an invalid fabric id"
        ) from None


def _only_san_uri(names: Iterable[x509.GeneralName]) -> str:
    entries = list(names)
    if len(entries) != 1:
        raise PkiError(
            PkiReason.SAN_COUNT, f"expected exactly one SAN entry, got {len(entries)}"
        )
    entry = entries[0]
    if not isinstance(entry, x509.UniformResourceIdentifier):
        raise PkiError(PkiReason.SAN_MALFORMED, "the SAN entry is not a URI")
    return entry.value


def _san_extension(
    extensions: x509.Extensions,
) -> x509.SubjectAlternativeName:
    try:
        ext = extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        raise PkiError(PkiReason.SAN_MISSING, "no subjectAltName") from None
    return ext.value


def parse_node_id(cert: x509.Certificate, *, fabric_id: str | None = None) -> NodeId:
    """The ``node_id`` a node certificate is bound to (its one SAN URI).

    With ``fabric_id``, the URI must name that fabric (``fabric_mismatch``).
    """
    san = _san_extension(cert_extensions(cert))
    cert_fabric, node_id = _parse_node_uri(_only_san_uri(san))
    if fabric_id is not None and not hmac.compare_digest(cert_fabric, fabric_id):
        raise PkiError(PkiReason.FABRIC_MISMATCH, "certificate is for another fabric")
    return node_id


# ------------------------------------------------------------------------ validity


def _as_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def is_expired(cert: x509.Certificate, now: datetime | None = None) -> bool:
    """``True`` from ``not_valid_after`` on."""
    return _as_utc(now) >= cert.not_valid_after_utc


def check_validity(cert: x509.Certificate, now: datetime | None = None) -> None:
    """Raise ``expired`` / ``not_yet_valid`` unless ``now`` is inside the window."""
    current = _as_utc(now)
    if current < cert.not_valid_before_utc:
        raise PkiError(PkiReason.NOT_YET_VALID, "certificate is not valid yet")
    if current >= cert.not_valid_after_utc:
        raise PkiError(PkiReason.EXPIRED, "certificate has expired")


def renewal_due(
    cert: x509.Certificate,
    now: datetime | None = None,
    *,
    fraction: float = RENEW_FRACTION,
) -> bool:
    """``True`` once ``fraction`` of the certificate's life has passed."""
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
    return _as_utc(now) >= start + (end - start) * fraction


def verify_issued_by(cert: x509.Certificate, ca_cert: x509.Certificate) -> None:
    """Raise ``not_issued_by_ca`` unless ``ca_cert`` signed ``cert``.

    Checks the issuer name and the ECDSA signature against the CA's public key -
    a certificate from another fabric (same subject text, other key) fails.
    """
    try:
        cert.verify_directly_issued_by(ca_cert)
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
        raise PkiError(
            PkiReason.NOT_ISSUED_BY_CA, "certificate was not issued by this fabric CA"
        ) from None


# ---------------------------------------------------------------------------- CSRs


def build_csr(
    key: ec.EllipticCurvePrivateKey, *, fabric_id: str, node_id: str
) -> x509.CertificateSigningRequest:
    """The CSR an agent sends at enrollment (G2): signed by its ADR-0013 key."""
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(node_uri(fabric_id, node_id))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )


def csr_from_bytes(data: bytes) -> x509.CertificateSigningRequest:
    """Parse a PEM or DER CSR, refusing anything over :data:`MAX_CSR_BYTES` unread."""
    if len(data) > MAX_CSR_BYTES:
        raise PkiError(PkiReason.OVERSIZED, f"CSR larger than {MAX_CSR_BYTES} bytes")
    try:
        if data.lstrip().startswith(b"-----BEGIN"):
            return x509.load_pem_x509_csr(data)
        return x509.load_der_x509_csr(data)
    except ValueError:
        raise PkiError(PkiReason.MALFORMED, "not a certificate request") from None


def _p256_public_key(key: object, what: str) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise PkiError(PkiReason.WRONG_KEY_TYPE, f"{what} key is not ECDSA")
    if not isinstance(key.curve, ec.SECP256R1):
        raise PkiError(PkiReason.WRONG_CURVE, f"{what} key is not on P-256")
    return key


def verify_csr(
    csr: x509.CertificateSigningRequest | bytes,
    *,
    fabric_id: str,
    node_id: str | None = None,
) -> VerifiedCsr:
    """Check a CSR as hostile input; return what it proves.

    Order: size/parse -> key type and curve -> hash -> signature -> SAN. The SAN
    must be exactly one URI naming ``fabric_id`` and, when given, ``node_id``.
    """
    request = csr_from_bytes(csr) if isinstance(csr, bytes) else csr
    try:
        public_key = _p256_public_key(request.public_key(), "CSR")
    except (ValueError, UnsupportedAlgorithm):
        raise PkiError(PkiReason.MALFORMED, "CSR public key is unreadable") from None
    try:
        algorithm = request.signature_hash_algorithm
    except UnsupportedAlgorithm:
        algorithm = None
    if not isinstance(algorithm, hashes.SHA256):
        raise PkiError(PkiReason.WRONG_HASH, "CSR is not signed with SHA-256")
    if not request.is_signature_valid:
        raise PkiError(PkiReason.BAD_SIGNATURE, "CSR signature does not verify")
    try:
        san = _san_extension(request.extensions)
    except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
        raise PkiError(PkiReason.MALFORMED, "CSR extensions are malformed") from None
    csr_fabric, csr_node = _parse_node_uri(_only_san_uri(san))
    if not hmac.compare_digest(csr_fabric, fabric_id):
        raise PkiError(PkiReason.FABRIC_MISMATCH, "CSR names another fabric")
    if node_id is not None and not hmac.compare_digest(csr_node, node_id):
        raise PkiError(PkiReason.NODE_MISMATCH, "CSR names another node")
    return VerifiedCsr(node_id=csr_node, fabric_id=csr_fabric, public_key=public_key)


# ----------------------------------------------------------------------- issuance


def _name(common_name: str, fabric_id: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _ORG),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, fabric_id),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _check_lifetime(lifetime: timedelta, limit: timedelta) -> None:
    if not timedelta(minutes=1) <= lifetime <= limit:
        raise PkiError(
            PkiReason.BAD_LIFETIME,
            f"lifetime must be between 1 minute and {limit.days} days",
        )


def create_ca(
    fabric_id: str,
    *,
    now: datetime | None = None,
    lifetime: timedelta = CA_LIFETIME,
    key: ec.EllipticCurvePrivateKey | None = None,
) -> CertificateAuthority:
    """A new self-signed fabric CA (D134)."""
    fid = validate_fabric_id(fabric_id)
    _check_lifetime(lifetime, CA_LIFETIME)
    current = _as_utc(now)
    ca_key = key or generate_key()
    _p256_public_key(ca_key.public_key(), "CA")
    name = _name(_CA_COMMON_NAME, fid)
    public = ca_key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - CLOCK_SKEW)
        .not_valid_after(current + lifetime)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return CertificateAuthority(key=ca_key, cert=cert, fabric_id=fid)


def _leaf_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _sign_leaf(
    ca: CertificateAuthority,
    *,
    subject: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    sans: Sequence[x509.GeneralName],
    usages: Sequence[x509.ObjectIdentifier],
    now: datetime | None,
    lifetime: timedelta,
) -> x509.Certificate:
    _check_lifetime(lifetime, MAX_LEAF_LIFETIME)
    current = _as_utc(now)
    check_validity(ca.cert, current)
    not_after = min(current + lifetime, ca.cert.not_valid_after_utc)
    ca_public = ca.key.public_key()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - CLOCK_SKEW)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_leaf_key_usage(), critical=True)
        .add_extension(x509.ExtendedKeyUsage(list(usages)), critical=False)
        .add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_public),
            critical=False,
        )
        .sign(ca.key, hashes.SHA256())
    )


def issue_server_cert(
    ca: CertificateAuthority,
    *,
    hosts: Iterable[str],
    now: datetime | None = None,
    lifetime: timedelta = SERVER_CERT_LIFETIME,
    key: ec.EllipticCurvePrivateKey | None = None,
) -> IssuedCert:
    """The controller's TLS server certificate (D134): fresh key unless given.

    SANs: :data:`SERVER_NAME` first, then each host (deduplicated, order kept).
    """
    sans: list[x509.GeneralName] = [x509.DNSName(SERVER_NAME)]
    for host in hosts:
        entry = validate_san_host(host)
        if entry not in sans:
            sans.append(entry)
    if len(sans) > MAX_SERVER_SANS:
        raise PkiError(PkiReason.BAD_HOST, f"more than {MAX_SERVER_SANS} SAN entries")
    server_key = key or generate_key()
    public = _p256_public_key(server_key.public_key(), "server")
    cert = _sign_leaf(
        ca,
        subject=_name(SERVER_NAME, ca.fabric_id),
        public_key=public,
        sans=sans,
        usages=[ExtendedKeyUsageOID.SERVER_AUTH],
        now=now,
        lifetime=lifetime,
    )
    return IssuedCert(key=server_key, cert=cert)


def issue_node_cert(
    ca: CertificateAuthority,
    csr: x509.CertificateSigningRequest | bytes,
    *,
    node_id: str,
    fabric_id: str,
    now: datetime | None = None,
    lifetime: timedelta = NODE_CERT_LIFETIME,
) -> x509.Certificate:
    """A node certificate for a verified CSR (D136); only its key is copied."""
    if not hmac.compare_digest(fabric_id, ca.fabric_id):
        raise PkiError(PkiReason.FABRIC_MISMATCH, "fabric_id is not this CA's fabric")
    verified = verify_csr(csr, fabric_id=fabric_id, node_id=node_id)
    return _sign_leaf(
        ca,
        subject=_name(verified.node_id, ca.fabric_id),
        public_key=verified.public_key,
        sans=[x509.UniformResourceIdentifier(node_uri(ca.fabric_id, verified.node_id))],
        usages=[ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH],
        now=now,
        lifetime=lifetime,
    )


# ------------------------------------------------------------------- file helpers


def _file_error(exc: PrivateFileError, path: Path) -> PkiFileError:
    reason = (
        PkiReason.OVERSIZED
        if exc.reason is PrivateFileReason.OVERSIZED
        else PkiReason.MALFORMED
    )
    return PkiFileError(reason, exc.message, path=path)


def read_private_key(
    path: Path, *, what: str = "private key"
) -> ec.EllipticCurvePrivateKey:
    """Load a P-256 key file: a regular file (no symlink/FIFO/device), owned by
    this user and not readable by others (POSIX)."""
    try:
        data = read_private(path, limit=MAX_KEY_BYTES, what=what)
    except PrivateFileError as exc:
        raise _file_error(exc, path) from None
    try:
        return key_from_pem(data)
    except PkiError as exc:
        raise PkiFileError(
            exc.reason, f"the {what} {path}: {exc.message}", path=path
        ) from None


def read_cert(
    path: Path, *, what: str = "certificate", private: bool = False
) -> x509.Certificate:
    """Load one PEM certificate file: a regular file (no symlink/FIFO/device),
    size-bounded; with ``private`` also owner-only (the agent's ``node.crt``)."""
    try:
        if private:
            data = read_private(path, limit=MAX_CERT_BYTES, what=what)
        else:
            data, _ = read_bounded(path, limit=MAX_CERT_BYTES, what=what)
    except PrivateFileError as exc:
        raise _file_error(exc, path) from None
    try:
        return cert_from_pem(data)
    except PkiError as exc:
        raise PkiFileError(
            exc.reason, f"the {what} {path}: {exc.message}", path=path
        ) from None


def _write(path: Path, data: bytes, *, exclusive: bool, what: str) -> None:
    try:
        atomic_write_bytes(path, data, mode=_FILE_MODE, exclusive=exclusive)
        protect_private(path)
    except OSError as exc:
        raise PkiFileError(
            PkiReason.MALFORMED,
            f"cannot write the {what} {path}: {exc.strerror or exc}",
            path=path,
        ) from None
    except PrivateFileError as exc:
        raise PkiFileError(PkiReason.MALFORMED, exc.message, path=path) from None


def write_private_key(
    path: Path, key: ec.EllipticCurvePrivateKey, *, exclusive: bool = False
) -> None:
    """Atomic, ``0600`` from creation (owner-only ACL on Windows)."""
    _write(path, key_to_pem(key), exclusive=exclusive, what="private key")


def write_cert(path: Path, cert: x509.Certificate, *, exclusive: bool = False) -> None:
    """Atomic; written owner-only like the keys (it sits in a private folder)."""
    _write(path, cert_to_pem(cert), exclusive=exclusive, what="certificate")
