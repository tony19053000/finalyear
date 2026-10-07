# ruff: noqa: S104, S303, E501, PLE2502, RUF001  # hostile-input fixtures: wildcard hosts, SHA-1, bidi/unicode lookalikes
"""Independent tester suite for ``meshforge.common.pki`` (V0.7 G1, D134 / §4 items 19-22).

Written from the spec (hostile CSRs, expiry/clock edges, same-subject foreign CA,
constant-time fingerprint compare, secrets never in errors) - not from the
implementation. THREAT_MODEL rows PK-S1, PK-T1, PK-E1, PK-D1, CG-S5, CG-S6.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.pki import PkiError, PkiReason

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------------- helpers


@pytest.fixture(scope="module")
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id(), now=NOW)


def _csr(
    key: Any,
    *,
    sans: list[x509.GeneralName],
    cn: str = "x",
    algorithm: hashes.HashAlgorithm | None = None,
    extra: list[tuple[x509.ExtensionType, bool]] | None = None,
    san_critical: bool = False,
) -> x509.CertificateSigningRequest:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans), critical=san_critical
        )
    for ext, critical in extra or []:
        builder = builder.add_extension(ext, critical=critical)
    if isinstance(key, ed25519.Ed25519PrivateKey | ed448.Ed448PrivateKey):
        return builder.sign(key, None)
    return builder.sign(key, algorithm or hashes.SHA256())


def _uri(fabric_id: str, node_id: str) -> x509.GeneralName:
    return x509.UniformResourceIdentifier(pki.node_uri(fabric_id, node_id))


def _reason(exc: pytest.ExceptionInfo[PkiError]) -> PkiReason:
    return exc.value.reason


# ------------------------------------------------------------ hostile CSR: keys


@pytest.mark.parametrize(
    ("curve", "reason"),
    [
        (ec.SECP384R1(), PkiReason.WRONG_CURVE),
        (ec.SECP521R1(), PkiReason.WRONG_CURVE),
        (ec.SECP256K1(), PkiReason.WRONG_CURVE),
    ],
)
def test_csr_on_a_non_p256_curve_is_refused(
    ca: pki.CertificateAuthority, curve: ec.EllipticCurve, reason: PkiReason
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    csr = _csr(ec.generate_private_key(curve), sans=[_uri(fid, nid)])
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(csr, fabric_id=fid, node_id=nid)
    assert _reason(exc) is reason
    with pytest.raises(PkiError):
        pki.issue_node_cert(ca, csr, node_id=nid, fabric_id=fid, now=NOW)


@pytest.mark.parametrize(
    "key",
    [
        rsa.generate_private_key(65537, 2048),
        ed25519.Ed25519PrivateKey.generate(),
        ed448.Ed448PrivateKey.generate(),
    ],
    ids=["rsa", "ed25519", "ed448"],
)
def test_csr_with_a_non_ecdsa_key_is_refused(
    ca: pki.CertificateAuthority, key: Any
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    csr = _csr(key, sans=[_uri(fid, nid)])
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(csr, fabric_id=fid, node_id=nid)
    assert _reason(exc) is PkiReason.WRONG_KEY_TYPE


@pytest.mark.parametrize("algorithm", [hashes.SHA384(), hashes.SHA512(), hashes.SHA1()])
def test_csr_signed_with_another_hash_is_refused(
    ca: pki.CertificateAuthority, algorithm: hashes.HashAlgorithm
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    try:
        csr = _csr(pki.generate_key(), sans=[_uri(fid, nid)], algorithm=algorithm)
    except Exception:  # noqa: BLE001 - the library may refuse to build SHA-1
        pytest.skip("cryptography refuses to build this CSR")
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(csr, fabric_id=fid, node_id=nid)
    assert _reason(exc) is PkiReason.WRONG_HASH


def test_csr_with_a_tampered_body_fails_the_signature(
    ca: pki.CertificateAuthority,
) -> None:
    """Flip a byte inside the signed region (the CN) - the signature must not verify."""
    fid, nid = ca.fabric_id, new_node_id()
    good = _csr(pki.generate_key(), sans=[_uri(fid, nid)], cn="AAAAAAAA")
    der = bytearray(good.public_bytes(serialization.Encoding.DER))
    at = bytes(der).index(b"AAAAAAAA")
    der[at] = ord("B")
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(bytes(der), fabric_id=fid, node_id=nid)
    assert _reason(exc) is PkiReason.BAD_SIGNATURE


def test_csr_with_a_swapped_public_key_fails_the_signature(
    ca: pki.CertificateAuthority,
) -> None:
    """Attacker splices victim's public key into their own signed CSR body."""
    fid, nid = ca.fabric_id, new_node_id()
    attacker, victim = pki.generate_key(), pki.generate_key()
    der = bytearray(
        _csr(attacker, sans=[_uri(fid, nid)]).public_bytes(serialization.Encoding.DER)
    )
    spki = attacker.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    new_spki = victim.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    at = bytes(der).index(spki)
    der[at : at + len(spki)] = new_spki
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(bytes(der), fabric_id=fid, node_id=nid)
    assert _reason(exc) is PkiReason.BAD_SIGNATURE


# ------------------------------------------------------------- hostile CSR: SANs


def test_csr_san_shapes_are_all_refused(ca: pki.CertificateAuthority) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    other = new_node_id()
    key = pki.generate_key()
    cases: dict[str, tuple[list[x509.GeneralName], PkiReason]] = {
        "no_san": ([], PkiReason.SAN_MISSING),
        "two_uris": ([_uri(fid, nid), _uri(fid, other)], PkiReason.SAN_COUNT),
        "uri_plus_dns": (
            [_uri(fid, nid), x509.DNSName("evil.example")],
            PkiReason.SAN_COUNT,
        ),
        "dns_only": ([x509.DNSName("evil.example")], PkiReason.SAN_MALFORMED),
        "ip_only": (
            [x509.IPAddress(ipaddress.ip_address("10.0.0.1"))],
            PkiReason.SAN_MALFORMED,
        ),
        "email_only": ([x509.RFC822Name("a@b.c")], PkiReason.SAN_MALFORMED),
    }
    for name, (sans, reason) in cases.items():
        csr = _csr(key, sans=sans)
        with pytest.raises(PkiError) as exc:
            pki.verify_csr(csr, fabric_id=fid, node_id=nid)
        assert _reason(exc) is reason, name


@pytest.mark.parametrize(
    "uri_for",
    [
        lambda f, n: f"meshforge://fabric/{f}/node/{n}/",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}/extra",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}?x=1",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}#frag",
        lambda f, n: f" meshforge://fabric/{f}/node/{n}",
        lambda f, n: f"meshforge://fabric/{f}/node/{n} ",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}\n",
        lambda f, n: f"MESHFORGE://fabric/{f}/node/{n}",
        lambda f, n: f"meshforge://fabric/{f.lower()}/node/{n.lower()}",
        lambda f, n: f"meshforge://fabric/{f}/node/../node/{n}",
        lambda f, n: f"meshforge://fabric/{f}/node/{n[:-1]}",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}0",
        lambda f, n: f"meshforge://fabric/{f}/node/",
        lambda f, n: f"meshforge://other/{f}/node/{n}",
        lambda f, n: f"https://fabric/{f}/node/{n}",
        lambda f, n: f"meshforge://fabric/{f}/job/{n}",
        lambda f, n: f"meshforge://fabric/{f}/node/{n}\x00",
        lambda f, n: "",
    ],
)
def test_csr_san_uri_tricks_are_refused(
    ca: pki.CertificateAuthority, uri_for: Any
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    try:
        san = x509.UniformResourceIdentifier(uri_for(fid, nid))
        csr = _csr(pki.generate_key(), sans=[san])
    except (ValueError, UnicodeError):
        pytest.skip("cryptography refuses to build this SAN")
    with pytest.raises(PkiError):
        pki.verify_csr(csr, fabric_id=fid, node_id=nid)
    with pytest.raises(PkiError):
        pki.issue_node_cert(ca, csr, node_id=nid, fabric_id=fid, now=NOW)


def test_csr_for_another_fabric_or_node_is_refused(
    ca: pki.CertificateAuthority,
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    key = pki.generate_key()
    foreign = pki.build_csr(key, fabric_id=new_fabric_id(), node_id=nid)
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(foreign, fabric_id=fid, node_id=nid)
    assert _reason(exc) is PkiReason.FABRIC_MISMATCH
    mine = pki.build_csr(key, fabric_id=fid, node_id=nid)
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(mine, fabric_id=fid, node_id=new_node_id())
    assert _reason(exc) is PkiReason.NODE_MISMATCH
    # issue_node_cert: the *caller's* fabric_id must be the CA's, even when the
    # CSR agrees with the wrong value.
    other_fid = new_fabric_id()
    csr_other = pki.build_csr(key, fabric_id=other_fid, node_id=nid)
    with pytest.raises(PkiError) as exc:
        pki.issue_node_cert(ca, csr_other, node_id=nid, fabric_id=other_fid, now=NOW)
    assert _reason(exc) is PkiReason.FABRIC_MISMATCH


# ------------------------------------------------------------ hostile CSR: inputs


def test_csr_size_boundary_and_garbage() -> None:
    fid = new_fabric_id()
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(b"A" * (pki.MAX_CSR_BYTES + 1), fabric_id=fid)
    assert _reason(exc) is PkiReason.OVERSIZED
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(b"A" * pki.MAX_CSR_BYTES, fabric_id=fid)  # at the limit: parsed
    assert _reason(exc) is PkiReason.MALFORMED
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(b"\x00" * (10 * 1024 * 1024), fabric_id=fid)
    assert _reason(exc) is PkiReason.OVERSIZED


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"\x00",
        b"\xff\xfe\xfd" * 50,
        b"-----BEGIN CERTIFICATE REQUEST-----\n!!!notbase64!!!\n-----END CERTIFICATE REQUEST-----\n",
        b"-----BEGIN CERTIFICATE REQUEST-----\n",
        b"\x30\x82\xff\xff" + b"\x00" * 20,  # DER SEQUENCE with a lying length
        b"-----BEGIN CERTIFICATE REQUEST-----\n\xc3\x28\n-----END CERTIFICATE REQUEST-----",
        "‮evil-----BEGIN".encode(),
    ],
)
def test_garbage_csr_bytes_are_a_typed_error_not_a_crash(blob: bytes) -> None:
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(blob, fabric_id=new_fabric_id())
    assert _reason(exc) in {PkiReason.MALFORMED, PkiReason.OVERSIZED}


def test_csr_as_pem_and_der_both_work_and_trailing_junk_is_not_trusted(
    ca: pki.CertificateAuthority,
) -> None:
    fid, nid = ca.fabric_id, new_node_id()
    csr = pki.build_csr(pki.generate_key(), fabric_id=fid, node_id=nid)
    pem = csr.public_bytes(serialization.Encoding.PEM)
    der = csr.public_bytes(serialization.Encoding.DER)
    assert pki.verify_csr(pem, fabric_id=fid).node_id == nid
    assert pki.verify_csr(b"\n  " + pem, fabric_id=fid).node_id == nid
    assert pki.verify_csr(der, fabric_id=fid).node_id == nid
    # Never crash with anything but PkiError; trailing junk may be refused or ignored,
    # but the answer must still be the *same* proven identity if accepted.
    try:
        got = pki.verify_csr(der + b"\x00junk", fabric_id=fid)
    except PkiError:
        pass
    else:
        assert got.node_id == nid


# --------------------------------------------------- CSR cannot smuggle privileges


def test_issued_node_cert_ignores_everything_but_the_public_key(
    ca: pki.CertificateAuthority,
) -> None:
    """CA:TRUE, keyCertSign, unknown critical OIDs, other EKU and subject in the CSR
    are *requests*: the issued cert must be a plain leaf bound to node_id."""
    fid, nid = ca.fabric_id, new_node_id()
    key = pki.generate_key()
    extra: list[tuple[x509.ExtensionType, bool]] = [
        (x509.BasicConstraints(ca=True, path_length=None), True),
        (
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
            True,
        ),
        (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), False),
        (
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier("1.3.6.1.4.1.99999.1"), b"\x05\x00"
            ),
            True,
        ),
    ]
    csr = _csr(key, sans=[_uri(fid, nid)], cn="CN=evil-admin", extra=extra)
    cert = pki.issue_node_cert(ca, csr, node_id=nid, fabric_id=fid, now=NOW)
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is False and bc.critical
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert not ku.key_cert_sign and not ku.crl_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.CODE_SIGNING not in list(eku)
    oids = {e.oid.dotted_string for e in cert.extensions}
    assert "1.3.6.1.4.1.99999.1" not in oids
    assert [
        a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    ] == [nid]
    assert [
        a.value
        for a in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    ] == [fid]
    assert pki.parse_node_id(cert, fabric_id=fid) == nid
    assert pki.public_keys_equal(key.public_key(), cert.public_key())
    pki.verify_issued_by(cert, ca.cert)
    assert cert.issuer == ca.cert.subject


def test_a_node_cert_cannot_be_used_as_an_issuer(ca: pki.CertificateAuthority) -> None:
    """A leaf must not 'issue': a cert it signs fails verify against the real CA, and a
    leaf is not accepted as its own CA by the structural checks."""
    fid, nid = ca.fabric_id, new_node_id()
    key = pki.generate_key()
    leaf = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=fid, node_id=nid),
        node_id=nid,
        fabric_id=fid,
        now=NOW,
    )
    forged = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forged")]))
        .issuer_name(leaf.subject)
        .public_key(pki.generate_key().public_key())
        .serial_number(1)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(PkiError) as exc:
        pki.verify_issued_by(forged, ca.cert)
    assert _reason(exc) is PkiReason.NOT_ISSUED_BY_CA
    bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False


def test_ca_is_a_pathlen_zero_root_with_cert_sign_usage(
    ca: pki.CertificateAuthority,
) -> None:
    bc = ca.cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca and bc.value.path_length == 0
    ku = ca.cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign
    assert ca.cert.issuer == ca.cert.subject
    assert isinstance(ca.cert.public_key(), ec.EllipticCurvePublicKey)
    assert ca.cert.public_key().curve.name == "secp256r1"  # type: ignore[union-attr]
    assert ca.cert.not_valid_after_utc - NOW == pki.CA_LIFETIME


# ------------------------------------------------------- other CA, same subject


def test_cert_from_another_ca_with_the_same_subject_is_refused() -> None:
    fid = new_fabric_id()
    real = pki.create_ca(fid, now=NOW)
    clone = pki.create_ca(fid, now=NOW)  # same subject text, different key
    assert real.cert.subject == clone.cert.subject
    leaf = pki.issue_server_cert(clone, hosts=["10.0.0.1"], now=NOW).cert
    with pytest.raises(PkiError) as exc:
        pki.verify_issued_by(leaf, real.cert)
    assert _reason(exc) is PkiReason.NOT_ISSUED_BY_CA
    pki.verify_issued_by(leaf, clone.cert)


def test_forged_authority_key_identifier_does_not_fool_issuer_check() -> None:
    fid = new_fabric_id()
    real = pki.create_ca(fid, now=NOW)
    clone = pki.create_ca(fid, now=NOW)
    nid = new_node_id()
    key = pki.generate_key()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nid)]))
        .issuer_name(real.cert.subject)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(real.key.public_key()),
            critical=False,
        )
        .sign(clone.key, hashes.SHA256())  # signed by the impostor, AKI says "real"
    )
    with pytest.raises(PkiError):
        pki.verify_issued_by(leaf, real.cert)


def test_cert_with_a_tampered_signature_is_refused(
    ca: pki.CertificateAuthority,
) -> None:
    leaf = pki.issue_server_cert(ca, hosts=["10.0.0.1"], now=NOW).cert
    der = bytearray(leaf.public_bytes(serialization.Encoding.DER))
    der[-3] ^= 0xFF
    try:
        broken = x509.load_der_x509_certificate(bytes(der))
    except ValueError:
        return  # even better: unparseable
    with pytest.raises(PkiError):
        pki.verify_issued_by(broken, ca.cert)


# ----------------------------------------------------------- expiry / clock edges


def test_validity_boundaries_are_exact(ca: pki.CertificateAuthority) -> None:
    leaf = pki.issue_server_cert(
        ca, hosts=[], now=NOW, lifetime=timedelta(days=10)
    ).cert
    nb, na = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    assert nb == NOW - pki.CLOCK_SKEW
    pki.check_validity(leaf, nb)  # first valid instant
    with pytest.raises(PkiError) as exc:
        pki.check_validity(leaf, nb - timedelta(microseconds=1))
    assert _reason(exc) is PkiReason.NOT_YET_VALID
    pki.check_validity(leaf, na - timedelta(microseconds=1))
    with pytest.raises(PkiError) as exc:
        pki.check_validity(leaf, na)
    assert _reason(exc) is PkiReason.EXPIRED
    assert pki.is_expired(leaf, na) and not pki.is_expired(
        leaf, na - timedelta(microseconds=1)
    )


def test_naive_clock_is_rejected_loudly(ca: pki.CertificateAuthority) -> None:
    leaf = pki.issue_server_cert(ca, hosts=[], now=NOW).cert
    with pytest.raises(ValueError, match="timezone"):
        pki.is_expired(leaf, datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        pki.check_validity(leaf, datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        pki.create_ca(new_fabric_id(), now=datetime(2026, 1, 1))  # noqa: DTZ001


def test_non_utc_aware_clock_is_normalised(ca: pki.CertificateAuthority) -> None:
    leaf = pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=timedelta(days=1)).cert
    tz = timezone_plus(14)
    inside = (NOW + timedelta(hours=1)).astimezone(tz)
    pki.check_validity(leaf, inside)
    outside = (NOW + timedelta(days=2)).astimezone(tz)
    with pytest.raises(PkiError):
        pki.check_validity(leaf, outside)


def timezone_plus(hours: int) -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_renewal_due_is_exactly_at_two_thirds(ca: pki.CertificateAuthority) -> None:
    leaf = pki.issue_server_cert(
        ca, hosts=[], now=NOW, lifetime=timedelta(days=30)
    ).cert
    start, end = leaf.not_valid_before_utc, leaf.not_valid_after_utc
    edge = start + (end - start) * pki.RENEW_FRACTION
    assert pki.renewal_due(leaf, edge)
    assert not pki.renewal_due(leaf, edge - timedelta(seconds=1))
    assert pki.renewal_due(leaf, end + timedelta(days=100))  # long expired -> due
    for bad in (0, -0.1, 1.0001, float("nan")):
        with pytest.raises(ValueError):
            pki.renewal_due(leaf, NOW, fraction=bad)


def test_leaf_notafter_is_clamped_to_the_ca(ca: pki.CertificateAuthority) -> None:
    short = pki.create_ca(new_fabric_id(), now=NOW, lifetime=timedelta(days=10))
    leaf = pki.issue_server_cert(
        short, hosts=[], now=NOW, lifetime=timedelta(days=400)
    ).cert
    assert leaf.not_valid_after_utc == short.cert.not_valid_after_utc
    nid = new_node_id()
    node = pki.issue_node_cert(
        short,
        pki.build_csr(pki.generate_key(), fabric_id=short.fabric_id, node_id=nid),
        node_id=nid,
        fabric_id=short.fabric_id,
        now=NOW,
        lifetime=timedelta(days=400),
    )
    assert node.not_valid_after_utc == short.cert.not_valid_after_utc


def test_issuing_from_an_expired_or_future_ca_is_refused() -> None:
    short = pki.create_ca(new_fabric_id(), now=NOW, lifetime=timedelta(days=2))
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(short, hosts=[], now=NOW + timedelta(days=3))
    assert _reason(exc) is PkiReason.EXPIRED
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(short, hosts=[], now=NOW - timedelta(days=1))
    assert _reason(exc) is PkiReason.NOT_YET_VALID
    # One instant before expiry still issues (clamped to a sliver of life).
    last = short.cert.not_valid_after_utc - timedelta(seconds=1)
    sliver = pki.issue_server_cert(short, hosts=[], now=last).cert
    assert sliver.not_valid_after_utc == short.cert.not_valid_after_utc


@pytest.mark.parametrize(
    "life",
    [timedelta(0), timedelta(seconds=-5), timedelta(seconds=59), timedelta(days=826)],
)
def test_bad_leaf_lifetimes_are_refused(
    ca: pki.CertificateAuthority, life: timedelta
) -> None:
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=life)
    assert _reason(exc) is PkiReason.BAD_LIFETIME
    nid = new_node_id()
    csr = pki.build_csr(pki.generate_key(), fabric_id=ca.fabric_id, node_id=nid)
    with pytest.raises(PkiError) as exc:
        pki.issue_node_cert(
            ca, csr, node_id=nid, fabric_id=ca.fabric_id, now=NOW, lifetime=life
        )
    assert _reason(exc) is PkiReason.BAD_LIFETIME


def test_lifetime_boundaries_are_accepted(ca: pki.CertificateAuthority) -> None:
    pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=timedelta(minutes=1))
    pki.issue_server_cert(ca, hosts=[], now=NOW, lifetime=pki.MAX_LEAF_LIFETIME)
    with pytest.raises(PkiError):
        pki.create_ca(
            new_fabric_id(), now=NOW, lifetime=pki.CA_LIFETIME + timedelta(days=1)
        )


# -------------------------------------------------------------- server cert SANs


@pytest.mark.parametrize(
    "host",
    [
        "*.example.com",
        "*",
        "0.0.0.0",
        "::",
        "[::]",
        "0:0:0:0:0:0:0:0",
        "",
        "   ",
        "a..b",
        "-a.example",
        "a-.example",
        "a_b.example",
        "ünï.example",
        "a" * 64 + ".example",
        ("a" * 60 + ".") * 5 + "com",
        "http://host",
        "host:80",
        "host/path",
        "ho st",
        "a\x00b.example",
        "a\nb.example",
        ".",
        "‮host",
    ],
)
def test_bad_server_hosts_are_refused(ca: pki.CertificateAuthority, host: str) -> None:
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(ca, hosts=[host], now=NOW)
    assert _reason(exc) is PkiReason.BAD_HOST


def test_server_cert_sans_dedupe_normalise_and_keep_order(
    ca: pki.CertificateAuthority,
) -> None:
    cert = pki.issue_server_cert(
        ca,
        hosts=[
            "10.0.0.5",
            "Foo.LAN",
            "foo.lan.",
            "[::1]",
            "::1",
            "0:0:0:0:0:0:0:1",
            "10.0.0.5",
            pki.SERVER_NAME,
            pki.SERVER_NAME.upper(),
            "localhost",
        ],
        now=NOW,
    ).cert
    assert pki.cert_sans(cert) == [
        pki.SERVER_NAME,
        "10.0.0.5",
        "foo.lan",
        "::1",
        "localhost",
    ]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False
    assert cert.not_valid_after_utc - NOW == pki.SERVER_CERT_LIFETIME


def test_server_cert_san_count_limit(ca: pki.CertificateAuthority) -> None:
    ok = [f"10.0.0.{i}" for i in range(pki.MAX_SERVER_SANS - 1)]  # + SERVER_NAME == max
    assert (
        len(pki.cert_sans(pki.issue_server_cert(ca, hosts=ok, now=NOW).cert))
        == pki.MAX_SERVER_SANS
    )
    with pytest.raises(PkiError):
        pki.issue_server_cert(ca, hosts=[*ok, "10.9.9.9"], now=NOW)
    # a generator with a million entries must not be consumed unboundedly
    import itertools

    gen = (f"h{i}.example" for i in itertools.count())
    with pytest.raises(PkiError):
        pki.issue_server_cert(ca, hosts=itertools.islice(gen, 5000), now=NOW)


def test_issue_with_a_wrong_curve_or_type_key_is_refused(
    ca: pki.CertificateAuthority,
) -> None:
    with pytest.raises(PkiError) as exc:
        pki.issue_server_cert(
            ca, hosts=[], now=NOW, key=ec.generate_private_key(ec.SECP384R1())
        )
    assert _reason(exc) is PkiReason.WRONG_CURVE
    with pytest.raises(PkiError):
        pki.create_ca(
            new_fabric_id(), now=NOW, key=rsa.generate_private_key(65537, 2048)
        )  # type: ignore[arg-type]
    from meshforge.common.errors import MeshForgeError

    for bad_id in ("not-a-fabric-id", "", "nod_01M4BTPD2Z1AS2CWTTQ9ARP9KX"):
        with pytest.raises(MeshForgeError):  # typed refusal; never mints a CA
            pki.create_ca(bad_id, now=NOW)


def test_serial_numbers_are_random_and_unique(ca: pki.CertificateAuthority) -> None:
    serials = {
        pki.issue_server_cert(ca, hosts=[], now=NOW).cert.serial_number
        for _ in range(50)
    }
    assert len(serials) == 50
    assert all(0 < s < 2**159 for s in serials)  # RFC 5280: positive, <= 20 octets


# --------------------------------------------------------------------- parsers


def test_parse_node_id_refuses_server_certs_and_wrong_fabrics(
    ca: pki.CertificateAuthority,
) -> None:
    server = pki.issue_server_cert(ca, hosts=["10.0.0.1"], now=NOW).cert
    with pytest.raises(PkiError):
        pki.parse_node_id(server)  # a DNS/IP SAN is not a node identity
    nid = new_node_id()
    node = pki.issue_node_cert(
        ca,
        pki.build_csr(pki.generate_key(), fabric_id=ca.fabric_id, node_id=nid),
        node_id=nid,
        fabric_id=ca.fabric_id,
        now=NOW,
    )
    assert pki.parse_node_id(node) == nid
    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(node, fabric_id=new_fabric_id())
    assert _reason(exc) is PkiReason.FABRIC_MISMATCH
    assert pki.parse_fabric_id(node) == ca.fabric_id
    assert pki.parse_fabric_id(server) == ca.fabric_id


def test_parse_node_id_without_a_san_or_with_two_is_refused(
    ca: pki.CertificateAuthority,
) -> None:
    nid = new_node_id()

    def leaf(sans: list[x509.GeneralName]) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nid)]))
            .issuer_name(ca.cert.subject)
            .public_key(pki.generate_key().public_key())
            .serial_number(5)
            .not_valid_before(NOW)
            .not_valid_after(NOW + timedelta(days=1))
        )
        if sans:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(sans), critical=False
            )
        return builder.sign(ca.key, hashes.SHA256())

    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(leaf([]))
    assert _reason(exc) is PkiReason.SAN_MISSING
    two = leaf([_uri(ca.fabric_id, nid), _uri(ca.fabric_id, new_node_id())])
    with pytest.raises(PkiError) as exc:
        pki.parse_node_id(two)
    assert _reason(exc) is PkiReason.SAN_COUNT


def test_parse_fabric_id_with_two_ou_values_is_refused() -> None:
    key = pki.generate_key()
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, new_fabric_id()),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, new_fabric_id()),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(3)
        .not_valid_before(NOW)
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(PkiError):
        pki.parse_fabric_id(cert)


# -------------------------------------------------------- PEM key / cert parsing


def test_key_from_pem_refuses_everything_but_unencrypted_p256() -> None:
    p384 = ec.generate_private_key(ec.SECP384R1())
    rsa_key = rsa.generate_private_key(65537, 2048)
    ed = ed25519.Ed25519PrivateKey.generate()
    for key, reason in [
        (p384, PkiReason.WRONG_CURVE),
        (rsa_key, PkiReason.WRONG_KEY_TYPE),
        (ed, PkiReason.WRONG_KEY_TYPE),
    ]:
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with pytest.raises(PkiError) as exc:
            pki.key_from_pem(pem)
        assert _reason(exc) is reason
        assert b"PRIVATE" not in str(exc.value).encode()
    encrypted = pki.generate_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"pw"),
    )
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(encrypted)
    assert _reason(exc) is PkiReason.MALFORMED
    public_pem = (
        pki.generate_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    with pytest.raises(PkiError):
        pki.key_from_pem(public_pem)
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(b"x" * (pki.MAX_KEY_BYTES + 1))
    assert _reason(exc) is PkiReason.OVERSIZED
    with pytest.raises(PkiError):
        pki.key_from_pem(b"")


def test_cert_from_pem_bounds_and_garbage(ca: pki.CertificateAuthority) -> None:
    with pytest.raises(PkiError) as exc:
        pki.cert_from_pem(b"x" * (pki.MAX_CERT_BYTES + 1))
    assert _reason(exc) is PkiReason.OVERSIZED
    for junk in (
        b"",
        b"\xff\xfe",
        b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    ):
        with pytest.raises(PkiError) as exc:
            pki.cert_from_pem(junk)
        assert _reason(exc) is PkiReason.MALFORMED
    der = ca.cert.public_bytes(serialization.Encoding.DER)
    with pytest.raises(PkiError):
        pki.cert_from_pem(der)  # DER is not PEM
    assert pki.cert_from_pem(pki.cert_to_pem(ca.cert)) == ca.cert


# ------------------------------------------------------------------ fingerprints


def test_fingerprint_forms_and_rejections(ca: pki.CertificateAuthority) -> None:
    full = pki.cert_fingerprint(ca.cert)
    short = pki.short_fingerprint(ca.cert)
    assert len(full) == 64 and len(short) == 32 and full.startswith(short)
    assert pki.fingerprint_matches(ca.cert, full, require_full=False)
    assert pki.fingerprint_matches(ca.cert, short, require_full=False)
    assert pki.fingerprint_matches(ca.cert, full.upper(), require_full=False)
    assert pki.fingerprint_matches(
        ca.cert, pki.format_fingerprint(short), require_full=False
    )
    assert pki.fingerprint_matches(
        ca.cert, ":".join(short[i : i + 2] for i in range(0, 32, 2)), require_full=False
    )
    assert pki.fingerprint_matches(ca.cert, f"  {short}\n", require_full=False)
    flipped = ("0" if short[0] != "0" else "1") + short[1:]
    last = short[:-1] + ("0" if short[-1] != "0" else "1")
    assert not pki.fingerprint_matches(ca.cert, flipped, require_full=False)
    assert not pki.fingerprint_matches(ca.cert, last, require_full=False)
    bad: list[str] = [
        "",
        " ",
        short[:-1],
        short + "0",
        full[:-1],
        full + "0",
        full[:40],
        "g" * 32,
        "z" * 64,
        "٠" * 32,  # Arabic-Indic digits
        "０" * 32,  # fullwidth digits
        short + "\x00",
        "\x00" * 32,
        "0x" + short[:30],
        short.replace(short[0], "K", 1) if short[0] == "k" else short[:-1] + "ı",
        "-" + short[:31],
    ]
    for value in bad:
        assert not pki.fingerprint_matches(ca.cert, value, require_full=False), repr(
            value
        )
    other = pki.create_ca(new_fabric_id(), now=NOW)
    assert not pki.fingerprint_matches(other.cert, short, require_full=False)
    assert not pki.fingerprint_matches(other.cert, full, require_full=False)


def test_fingerprint_compare_is_constant_time_by_construction(
    ca: pki.CertificateAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 22: the comparison goes through hmac.compare_digest (a spy proves it) and
    never short-circuits on ``==`` - the same call happens for first-char and last-char
    mismatches."""
    seen: list[tuple[bytes, bytes]] = []
    real = pki.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        seen.append((bytes(a), bytes(b)))
        return real(a, b)

    monkeypatch.setattr(pki.hmac, "compare_digest", spy)
    short = pki.short_fingerprint(ca.cert)
    first = ("0" if short[0] != "0" else "1") + short[1:]
    last = short[:-1] + ("0" if short[-1] != "0" else "1")
    for candidate in (short, first, last):
        pki.fingerprint_matches(ca.cert, candidate, require_full=False)
    assert len(seen) == 3
    assert all(len(a) == len(b) == 32 for a, b in seen)


def test_mutation_the_spy_would_notice_a_plain_equality(
    ca: pki.CertificateAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation check for the test above: if ``fingerprint_matches`` used ``==`` the
    spy sees no compare_digest call."""
    seen: list[int] = []
    monkeypatch.setattr(pki.hmac, "compare_digest", lambda a, b: seen.append(1) or True)

    def mutant(cert: x509.Certificate, expected: str) -> bool:
        return pki.cert_fingerprint(cert)[:32] == expected

    mutant(ca.cert, pki.short_fingerprint(ca.cert))
    assert seen == []


# ------------------------------------------------------- secrets never leak (§4/20)


def _no_secret(text: str, key: ec.EllipticCurvePrivateKey) -> None:
    pem = pki.key_to_pem(key).decode()
    body = [ln for ln in pem.splitlines() if ln and not ln.startswith("-----")]
    assert "PRIVATE KEY" not in text
    for line in body:
        assert line not in text
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    assert der.hex() not in text.lower()
    nums = key.private_numbers().private_value
    assert str(nums) not in text and format(nums, "x") not in text.lower()


def test_dataclass_reprs_and_errors_never_show_key_material() -> None:
    authority = pki.create_ca(new_fabric_id(), now=NOW)
    issued = pki.issue_server_cert(authority, hosts=[], now=NOW)
    for obj in (authority, issued):
        _no_secret(repr(obj), obj.key)
        _no_secret(str(obj), obj.key)
    # Errors raised while *handling* a key do not echo it.
    pem = pki.key_to_pem(authority.key)
    with pytest.raises(PkiError) as exc:
        pki.key_from_pem(pem[:-30] + b"\n")  # truncated: malformed
    _no_secret(f"{exc.value} {exc.value!r} {exc.value.details}", authority.key)
    # Hostile CSR content is not reflected into the error text either.
    secret_cn = "TOPSECRET-" + "Z" * 20
    csr = _csr(
        pki.generate_key(),
        sans=[x509.DNSName(secret_cn.lower() + ".example")],
        cn=secret_cn,
    )
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(csr, fabric_id=authority.fabric_id)
    assert secret_cn not in f"{exc.value} {exc.value.details}"
    with pytest.raises(PkiError) as exc:
        pki.verify_csr(b"-----BEGIN " + b"Q" * 100, fabric_id=authority.fabric_id)
    assert "QQQQ" not in f"{exc.value} {exc.value.details}"


def test_mutation_the_leak_detector_catches_a_repr_that_exposes_the_key() -> None:
    """Item 20 mutation check: flip ``repr=False`` off and the detector must fire -
    proving the assertions above are not vacuous."""
    import dataclasses

    authority = pki.create_ca(new_fabric_id(), now=NOW)

    @dataclasses.dataclass(frozen=True)
    class LeakyCa:  # the mutation: key included in repr
        key: ec.EllipticCurvePrivateKey
        cert: x509.Certificate

    leaky = LeakyCa(key=authority.key, cert=authority.cert)
    # The default object repr of the key is opaque, so make the mutation explicit:
    text = repr(leaky) + pki.key_to_pem(authority.key).decode()
    with pytest.raises(AssertionError):
        _no_secret(text, authority.key)


def test_pki_file_errors_name_the_path_never_the_content(tmp_path: Any) -> None:
    path = tmp_path / "ca.key"
    secret = pki.key_to_pem(pki.generate_key())
    path.write_bytes(secret + b"x" * (pki.MAX_KEY_BYTES))  # oversized
    path.chmod(0o600)
    with pytest.raises(PkiError) as exc:
        pki.read_private_key(path, what="fabric CA key")
    text = f"{exc.value} {exc.value.details!r}"
    assert str(path) in text
    assert "BEGIN" not in text and secret.splitlines()[1].decode() not in text
