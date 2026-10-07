"""``meshforge.common.pki`` (V0.7 G1, D134/D136): CA, server/node certs, CSRs.

Hostile CSRs (wrong curve, RSA, Ed25519, bad signature, SAN count/shape, oversized,
garbage), validity windows with an injected clock, certificates from another CA,
fingerprints, and the private-file helpers. THREAT_MODEL rows PK-S1, PK-T1, PK-E1,
PK-D1, CG-S5, CG-S6 point here.
"""

from __future__ import annotations

import ipaddress
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from meshforge.common import pki
from meshforge.common.errors import ExitCode, FailureType
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.pki import PkiError, PkiFileError, PkiReason

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
WILDCARD_V4 = "0.0.0.0"  # noqa: S104 - refused as a SAN host, never bound
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX mode bits; Windows uses ACLs"
)


@pytest.fixture(scope="module")
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id(), now=NOW)


@pytest.fixture(scope="module")
def node_key() -> ec.EllipticCurvePrivateKey:
    return pki.generate_key()


def _csr_with(
    key: object,
    sans: list[x509.GeneralName] | None,
    *,
    algorithm: hashes.HashAlgorithm | None = None,
    extra: list[x509.ExtensionType] | None = None,
) -> bytes:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    )
    if sans is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans), critical=False
        )
    for ext in extra or []:
        builder = builder.add_extension(ext, critical=True)
    csr = builder.sign(key, algorithm)  # type: ignore[arg-type]
    return csr.public_bytes(serialization.Encoding.PEM)


def _uri(fabric_id: str, node_id: str) -> x509.GeneralName:
    return x509.UniformResourceIdentifier(pki.node_uri(fabric_id, node_id))


def _reason(exc: pytest.ExceptionInfo[PkiError]) -> PkiReason:
    return exc.value.reason


# --------------------------------------------------------------------------- CA


def test_ca_is_self_signed_p256_with_fabric_id(ca: pki.CertificateAuthority) -> None:
    cert = ca.cert
    pki.verify_issued_by(cert, cert)
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    assert isinstance(cert.public_key().curve, ec.SECP256R1)  # type: ignore[union-attr]
    assert pki.parse_fabric_id(cert) == ca.fabric_id
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    assert cert.not_valid_after_utc - NOW == pki.CA_LIFETIME
    assert NOW - cert.not_valid_before_utc == pki.CLOCK_SKEW


def test_ca_path_length_zero(ca: pki.CertificateAuthority) -> None:
    bc = ca.cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca and bc.value.path_length == 0
    ku = ca.cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign and ku.value.crl_sign


def test_ca_repr_hides_the_key(ca: pki.CertificateAuthority) -> None:
    assert "key=" not in repr(ca)
    assert "PRIVATE" not in repr(ca)


def test_create_ca_refuses_bad_fabric_id_and_lifetime() -> None:
    with pytest.raises(ValueError, match="fab"):
        pki.create_ca(new_node_id(), now=NOW)
    with pytest.raises(PkiError) as exc:
        pki.create_ca(new_fabric_id(), now=NOW, lifetime=pki.CA_LIFETIME + timedelta(1))
    assert _reason(exc) is PkiReason.BAD_LIFETIME


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        pki.create_ca(new_fabric_id(), now=datetime(2026, 1, 1))  # noqa: DTZ001


def test_create_ca_refuses_non_p256_key() -> None:
    with pytest.raises(PkiError) as exc:
        pki.create_ca(
            new_fabric_id(), now=NOW, key=ec.generate_private_key(ec.SECP384R1())
        )
    assert _reason(exc) is PkiReason.WRONG_CURVE


# --------------------------------------------------------------------- server cert


def test_server_cert_sans_and_usage(ca: pki.CertificateAuthority) -> None:
    issued = pki.issue_server_cert(
        ca,
        hosts=["192.168.1.10", "Laptop-1.local.", "[fe80::1]", "192.168.1.10"],
        now=NOW,
    )
    cert = issued.cert
    pki.verify_issued_by(cert, ca.cert)
    assert pki.cert_sans(cert) == [
        pki.SERVER_NAME,
        "192.168.1.10",
        "laptop-1.local",
        "fe80::1",
    ]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert pki.public_keys_equal(issued.key.public_key(), cert.public_key())
    assert cert.not_valid_after_utc - NOW == pki.SERVER_CERT_LIFETIME


@pytest.mark.parametrize(
    "host",
    [WILDCARD_V4, "::", "*.lan", "-bad.lan", "a..b", "", "under_score", "x" * 64],
)
def test_server_cert_refuses_bad_hosts(ca: pki.CertificateAuthority, host: str) -> None:
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(ca, hosts=[host], now=NOW)
    assert _reason(exc) is PkiReason.BAD_HOST


def test_server_cert_san_count_bound(ca: pki.CertificateAuthority) -> None:
    hosts = [
        str(ipaddress.ip_address("10.0.0.1") + i) for i in range(pki.MAX_SERVER_SANS)
    ]
    issued = pki.issue_server_cert(ca, hosts=hosts[:-1], now=NOW)  # + SERVER_NAME = max
    assert len(pki.cert_sans(issued.cert)) == pki.MAX_SERVER_SANS
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(ca, hosts=hosts, now=NOW)
    assert _reason(exc) is PkiReason.BAD_HOST


def test_dns_name_length_bound() -> None:
    label = "a" * 63
    ok = ".".join([label, label, label, "a" * 61])  # 253 characters
    assert len(ok) == 253
    assert isinstance(pki.validate_san_host(ok), x509.DNSName)
    with pytest.raises(PkiError):
        pki.validate_san_host(ok + "a")


def test_leaf_lifetime_is_clamped_to_the_ca() -> None:
    short_ca = pki.create_ca(new_fabric_id(), now=NOW, lifetime=timedelta(days=10))
    issued = pki.issue_server_cert(short_ca, hosts=[], now=NOW)
    assert issued.cert.not_valid_after_utc == short_ca.cert.not_valid_after_utc


def test_expired_ca_issues_nothing() -> None:
    old = pki.create_ca(new_fabric_id(), now=NOW, lifetime=timedelta(days=1))
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(old, hosts=[], now=NOW + timedelta(days=2))
    assert _reason(exc) is PkiReason.EXPIRED


@pytest.mark.parametrize("lifetime", [timedelta(seconds=59), timedelta(days=826)])
def test_leaf_lifetime_bounds(
    ca: pki.CertificateAuthority, lifetime: timedelta
) -> None:
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=lifetime)
    assert _reason(exc) is PkiReason.BAD_LIFETIME


# ----------------------------------------------------------------------- node cert


def test_node_cert_round_trip(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    csr = pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id)
    cert = pki.issue_node_cert(
        ca, csr, node_id=node_id, fabric_id=ca.fabric_id, now=NOW
    )
    pki.verify_issued_by(cert, ca.cert)
    assert pki.parse_node_id(cert) == node_id
    assert pki.parse_node_id(cert, fabric_id=ca.fabric_id) == node_id
    assert pki.public_keys_equal(node_key.public_key(), cert.public_key())
    assert pki.cert_sans(cert) == [pki.node_uri(ca.fabric_id, node_id)]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert set(eku) == {
        ExtendedKeyUsageOID.CLIENT_AUTH,
        ExtendedKeyUsageOID.SERVER_AUTH,
    }
    assert cert.not_valid_after_utc - NOW == pki.NODE_CERT_LIFETIME


def test_leaf_certs_are_not_cas(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    # The CSR *asks* to be a CA; the issued cert must ignore every CSR extension.
    pem = _csr_with(
        node_key,
        [_uri(ca.fabric_id, node_id)],
        algorithm=hashes.SHA256(),
        extra=[x509.BasicConstraints(ca=True, path_length=None)],
    )
    node = pki.issue_node_cert(
        ca, pem, node_id=node_id, fabric_id=ca.fabric_id, now=NOW
    )
    server = pki.issue_server_cert(ca, hosts=[], now=NOW).cert
    for cert in (node, server):
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.critical and not bc.value.ca
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        assert not ku.key_cert_sign and not ku.crl_sign
    # A node cert never carries a DNS/IP name: it cannot pass a controller hostname check.
    san = node.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert not san.get_values_for_type(x509.DNSName)
    assert not san.get_values_for_type(x509.IPAddress)


def test_issue_node_cert_refuses_other_fabric(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    other = new_fabric_id()
    node_id = new_node_id()
    csr = pki.build_csr(node_key, fabric_id=other, node_id=node_id)
    with pytest.raises(PkiError) as exc:
        pki.issue_node_cert(ca, csr, node_id=node_id, fabric_id=other, now=NOW)
    assert _reason(exc) is PkiReason.FABRIC_MISMATCH


def test_parse_node_id_refuses_server_cert_and_other_fabric(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    server = pki.issue_server_cert(ca, hosts=[], now=NOW).cert
    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(server)
    assert _reason(exc) is PkiReason.SAN_MALFORMED
    node_id = new_node_id()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id),
        node_id=node_id,
        fabric_id=ca.fabric_id,
        now=NOW,
    )
    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(cert, fabric_id=new_fabric_id())
    assert _reason(exc) is PkiReason.FABRIC_MISMATCH
    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(ca.cert)  # no SAN at all
    assert _reason(exc) is PkiReason.SAN_MISSING


# ------------------------------------------------------------------- hostile CSRs


def _verify(pem: bytes, fabric_id: str, node_id: str | None = None) -> PkiReason:
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(pem, fabric_id=fabric_id, node_id=node_id)
    return _reason(exc)


def test_verify_csr_accepts_pem_and_der(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    csr = pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id)
    for data in (
        csr.public_bytes(serialization.Encoding.PEM),
        csr.public_bytes(serialization.Encoding.DER),
    ):
        verified = pki.verify_csr(data, fabric_id=ca.fabric_id, node_id=node_id)
        assert verified.node_id == node_id
        assert verified.fabric_id == ca.fabric_id
        assert pki.public_keys_equal(verified.public_key, node_key.public_key())


def test_verify_csr_refuses_wrong_curve(ca: pki.CertificateAuthority) -> None:
    node_id = new_node_id()
    key = ec.generate_private_key(ec.SECP384R1())
    pem = _csr_with(key, [_uri(ca.fabric_id, node_id)], algorithm=hashes.SHA256())
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.WRONG_CURVE


def test_verify_csr_refuses_rsa(ca: pki.CertificateAuthority) -> None:
    node_id = new_node_id()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = _csr_with(key, [_uri(ca.fabric_id, node_id)], algorithm=hashes.SHA256())
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.WRONG_KEY_TYPE


def test_verify_csr_refuses_ed25519(ca: pki.CertificateAuthority) -> None:
    node_id = new_node_id()
    key = ed25519.Ed25519PrivateKey.generate()
    pem = _csr_with(key, [_uri(ca.fabric_id, node_id)], algorithm=None)
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.WRONG_KEY_TYPE


def test_verify_csr_refuses_sha384(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    pem = _csr_with(node_key, [_uri(ca.fabric_id, node_id)], algorithm=hashes.SHA384())
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.WRONG_HASH


def test_verify_csr_refuses_bad_signature(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    der = bytearray(
        pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id).public_bytes(
            serialization.Encoding.DER
        )
    )
    der[-1] ^= (
        0x01  # last byte of the ECDSA ``s`` integer: still parses, no longer verifies
    )
    assert _verify(bytes(der), ca.fabric_id, node_id) is PkiReason.BAD_SIGNATURE


def test_verify_csr_refuses_two_sans(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    two_uris = [_uri(ca.fabric_id, node_id), _uri(ca.fabric_id, new_node_id())]
    pem = _csr_with(node_key, two_uris, algorithm=hashes.SHA256())
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.SAN_COUNT
    uri_and_dns = [_uri(ca.fabric_id, node_id), x509.DNSName(pki.SERVER_NAME)]
    pem = _csr_with(node_key, uri_and_dns, algorithm=hashes.SHA256())
    assert _verify(pem, ca.fabric_id, node_id) is PkiReason.SAN_COUNT


def test_verify_csr_refuses_missing_or_wrong_san(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    assert (
        _verify(_csr_with(node_key, None, algorithm=hashes.SHA256()), ca.fabric_id)
        is PkiReason.SAN_MISSING
    )
    dns_only = _csr_with(
        node_key, [x509.DNSName(pki.SERVER_NAME)], algorithm=hashes.SHA256()
    )
    assert _verify(dns_only, ca.fabric_id) is PkiReason.SAN_MALFORMED
    for bad in (
        f"meshforge://fabric/{ca.fabric_id}/node/{node_id}/extra",
        f"https://fabric/{ca.fabric_id}/node/{node_id}",
        f"meshforge://fabric/{ca.fabric_id}/node/{node_id.lower()}",
        f"meshforge://fabric/{ca.fabric_id}/node/job_{node_id[5:]}",
    ):
        pem = _csr_with(
            node_key, [x509.UniformResourceIdentifier(bad)], algorithm=hashes.SHA256()
        )
        assert _verify(pem, ca.fabric_id) is PkiReason.SAN_MALFORMED, bad


def test_verify_csr_refuses_other_node_and_fabric(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    pem = pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id).public_bytes(
        serialization.Encoding.PEM
    )
    assert _verify(pem, ca.fabric_id, new_node_id()) is PkiReason.NODE_MISMATCH
    assert _verify(pem, new_fabric_id(), node_id) is PkiReason.FABRIC_MISMATCH


def test_verify_csr_size_bound(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    node_id = new_node_id()
    pem = pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id).public_bytes(
        serialization.Encoding.PEM
    )
    at_max = pem + b"\n" * (pki.MAX_CSR_BYTES - len(pem))
    assert len(at_max) == pki.MAX_CSR_BYTES
    assert pki.verify_csr(at_max, fabric_id=ca.fabric_id).node_id == node_id
    assert _verify(at_max + b"\n", ca.fabric_id) is PkiReason.OVERSIZED


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"garbage",
        b"-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n",
        b"\x30\x03\x02\x01\x00",
    ],
)
def test_verify_csr_refuses_garbage(ca: pki.CertificateAuthority, data: bytes) -> None:
    assert _verify(data, ca.fabric_id) is PkiReason.MALFORMED


def test_pki_error_is_typed_and_carries_no_input(
    ca: pki.CertificateAuthority,
) -> None:
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(b"secret-looking-bytes", fabric_id=ca.fabric_id)
    assert exc.value.failure_type is FailureType.INVALID_INPUT
    assert exc.value.details == {"reason": "malformed"}
    assert "secret-looking" not in str(exc.value)


# -------------------------------------------------------------- issuer / validity


def test_cert_from_another_ca_is_refused(
    ca: pki.CertificateAuthority, node_key: ec.EllipticCurvePrivateKey
) -> None:
    # Same fabric_id, same subject text - only the key differs.
    twin = pki.create_ca(ca.fabric_id, now=NOW)
    assert twin.cert.subject == ca.cert.subject
    node_id = new_node_id()
    cert = pki.issue_node_cert(
        twin,
        pki.build_csr(node_key, fabric_id=ca.fabric_id, node_id=node_id),
        node_id=node_id,
        fabric_id=ca.fabric_id,
        now=NOW,
    )
    with pytest.raises(PkiError) as exc:
        pki.verify_issued_by(cert, ca.cert)
    assert _reason(exc) is PkiReason.NOT_ISSUED_BY_CA
    other = pki.create_ca(new_fabric_id(), now=NOW)
    with pytest.raises(PkiError):
        pki.verify_issued_by(
            pki.issue_server_cert(other, hosts=[], now=NOW).cert, ca.cert
        )


def test_is_expired_boundaries(ca: pki.CertificateAuthority) -> None:
    cert = pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=timedelta(days=3)).cert
    end = cert.not_valid_after_utc
    assert not pki.is_expired(cert, end - timedelta(microseconds=1))
    assert pki.is_expired(cert, end)
    pki.check_validity(cert, NOW)
    with pytest.raises(PkiError) as exc:
        pki.check_validity(cert, end)
    assert _reason(exc) is PkiReason.EXPIRED


def test_is_expired_not_yet_valid(ca: pki.CertificateAuthority) -> None:
    cert = pki.issue_server_cert(ca, hosts=[], now=NOW).cert
    start = cert.not_valid_before_utc
    pki.check_validity(cert, start)  # the back-dated start itself is valid
    with pytest.raises(PkiError) as exc:
        pki.check_validity(cert, start - timedelta(microseconds=1))
    assert _reason(exc) is PkiReason.NOT_YET_VALID


def test_renewal_due_at_two_thirds(ca: pki.CertificateAuthority) -> None:
    cert = pki.issue_server_cert(
        ca, hosts=[], now=NOW, lifetime=timedelta(days=30)
    ).cert
    start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
    due = start + (end - start) * 2 / 3
    assert not pki.renewal_due(cert, due - timedelta(seconds=1))
    assert pki.renewal_due(cert, due)
    with pytest.raises(ValueError, match="fraction"):
        pki.renewal_due(cert, NOW, fraction=0)


# -------------------------------------------------------------------- fingerprints


def test_short_fingerprint_is_128_bits_of_sha256(ca: pki.CertificateAuthority) -> None:
    full = pki.cert_fingerprint(ca.cert)
    assert len(full) == 64 and full == full.lower()
    assert pki.short_fingerprint(ca.cert) == full[:32]
    assert pki.format_fingerprint(full[:8]) == f"{full[:4]} {full[4:8]}"


def test_short_fingerprint_matches_forms(ca: pki.CertificateAuthority) -> None:
    full = pki.cert_fingerprint(ca.cert)
    short = full[:32]
    assert pki.fingerprint_matches(ca.cert, short)
    assert pki.fingerprint_matches(ca.cert, full.upper())
    assert pki.fingerprint_matches(ca.cert, pki.format_fingerprint(short))
    assert pki.fingerprint_matches(
        ca.cert, ":".join(short[i : i + 2] for i in range(0, 32, 2))
    )
    other = pki.create_ca(ca.fabric_id, now=NOW)
    assert not pki.fingerprint_matches(other.cert, short)
    flipped = short[:-1] + ("0" if short[-1] != "0" else "1")
    assert not pki.fingerprint_matches(ca.cert, flipped)
    for bad in ("", short[:31], short + "0", "g" * 32, full[:48]):
        assert not pki.fingerprint_matches(ca.cert, bad), bad


# ---------------------------------------------------------------------- key/cert IO


def test_key_and_cert_pem_round_trip(ca: pki.CertificateAuthority) -> None:
    key = pki.key_from_pem(pki.key_to_pem(ca.key))
    assert pki.public_keys_equal(key.public_key(), ca.key.public_key())
    assert pki.cert_from_pem(pki.cert_to_pem(ca.cert)) == ca.cert


def test_key_from_pem_refuses_other_keys() -> None:
    rsa_pem = rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(rsa_pem)
    assert _reason(exc) is PkiReason.WRONG_KEY_TYPE
    p384 = pki.key_to_pem(ec.generate_private_key(ec.SECP384R1()))  # type: ignore[arg-type]
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(p384)
    assert _reason(exc) is PkiReason.WRONG_CURVE
    encrypted = pki.generate_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pw"),
    )
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(encrypted)
    assert _reason(exc) is PkiReason.MALFORMED
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(b"x" * (pki.MAX_KEY_BYTES + 1))
    assert _reason(exc) is PkiReason.OVERSIZED


def test_cert_from_pem_bounds(ca: pki.CertificateAuthority) -> None:
    pem = pki.cert_to_pem(ca.cert)
    at_max = pem + b"\n" * (pki.MAX_CERT_BYTES - len(pem))
    assert pki.cert_from_pem(at_max) == ca.cert
    with pytest.raises(PkiError) as exc:
        pki.cert_from_pem(at_max + b"\n")
    assert _reason(exc) is PkiReason.OVERSIZED
    with pytest.raises(PkiError) as exc:
        pki.cert_from_pem(b"not a cert")
    assert _reason(exc) is PkiReason.MALFORMED


def test_write_and_read_files(tmp_path: Path, ca: pki.CertificateAuthority) -> None:
    key_path, cert_path = tmp_path / "x.key", tmp_path / "x.crt"
    pki.write_private_key(key_path, ca.key, exclusive=True)
    pki.write_cert(cert_path, ca.cert)
    assert pki.public_keys_equal(
        pki.read_private_key(key_path).public_key(), ca.key.public_key()
    )
    assert pki.read_cert(cert_path) == ca.cert
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    with pytest.raises(PkiFileError):
        pki.write_private_key(key_path, ca.key, exclusive=True)  # refuses to overwrite


@posix_only
def test_read_private_key_refuses_loose_mode(
    tmp_path: Path, ca: pki.CertificateAuthority
) -> None:
    path = tmp_path / "x.key"
    pki.write_private_key(path, ca.key)
    os.chmod(path, 0o640)
    with pytest.raises(PkiFileError) as exc:
        pki.read_private_key(path)
    assert exc.value.exit_code == ExitCode.CONFIG_ERROR
    assert "chmod 600" in str(exc.value)
    assert "PRIVATE KEY" not in str(exc.value)


def test_read_files_refuse_missing_oversized_garbage(tmp_path: Path) -> None:
    with pytest.raises(PkiFileError) as exc:
        pki.read_cert(tmp_path / "absent.crt")
    assert exc.value.details["path"].endswith("absent.crt")
    big = tmp_path / "big.key"
    big.write_bytes(b"x" * (pki.MAX_KEY_BYTES + 1))
    big.chmod(0o600)
    with pytest.raises(PkiFileError) as exc:
        pki.read_private_key(big)
    assert exc.value.reason is PkiReason.OVERSIZED
    junk = tmp_path / "junk.crt"
    junk.write_bytes(b"junk")
    with pytest.raises(PkiFileError) as exc:
        pki.read_cert(junk)
    assert exc.value.reason is PkiReason.MALFORMED
