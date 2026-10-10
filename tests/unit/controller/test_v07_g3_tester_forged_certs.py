"""V0.7 G3 tester: certificates a hostile or buggy CA-holder path could produce,
fed to the session port's identity check (:func:`parse_peer`).

Derived from criterion 1 ("only a certificate of this fabric names a node") and
D137, not from the implementation: every shape below is CA-signed (so TLS would
let it through) and must still be refused, or - for the benign ones - give the
node the *SAN* names, never the CN.
"""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.controller.session.peer_cert import (
    PeerCertError,
    PeerCertReason,
    parse_peer,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime.now(dt.UTC)
CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH
SERVER = ExtendedKeyUsageOID.SERVER_AUTH


@pytest.fixture(scope="module")
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id(), now=NOW - dt.timedelta(days=2))


def forge(
    ca: pki.CertificateAuthority,
    *,
    cn: str | None = None,
    sans: list[x509.GeneralName] | None = None,
    usages: list[x509.ObjectIdentifier] | None = None,
    now: dt.datetime = NOW,
    lifetime: dt.timedelta = dt.timedelta(days=30),
    node: str | None = None,
) -> tuple[str, bytes]:
    node_id = node or str(new_node_id())
    key = pki.generate_key()
    cert = pki._sign_leaf(
        ca,
        subject=pki._name(cn or node_id, ca.fabric_id),
        public_key=key.public_key(),
        sans=sans
        if sans is not None
        else [x509.UniformResourceIdentifier(pki.node_uri(ca.fabric_id, node_id))],
        usages=usages if usages is not None else [CLIENT],
        now=now,
        lifetime=lifetime,
    )
    return node_id, pki.cert_to_pem(cert)


def refused(
    pem: bytes | None, ca: pki.CertificateAuthority, **kw: object
) -> PeerCertReason:
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW, **kw)  # type: ignore[arg-type]
    return info.value.reason


def test_server_auth_only_cert_is_not_a_client_identity(
    ca: pki.CertificateAuthority,
) -> None:
    _, pem = forge(ca, usages=[SERVER])
    assert refused(pem, ca) is PeerCertReason.NOT_CLIENT


def test_cert_without_any_eku_is_not_a_client_identity(
    ca: pki.CertificateAuthority,
) -> None:
    # An EKU-less certificate means "any purpose" to some stacks; here it must fail closed.
    node = str(new_node_id())
    key = pki.generate_key()
    builder_cert = pki._sign_leaf(
        ca,
        subject=pki._name(node, ca.fabric_id),
        public_key=key.public_key(),
        sans=[x509.UniformResourceIdentifier(pki.node_uri(ca.fabric_id, node))],
        usages=[],
        now=NOW,
        lifetime=dt.timedelta(days=1),
    )
    assert refused(pki.cert_to_pem(builder_cert), ca) in {
        PeerCertReason.NOT_CLIENT,
        PeerCertReason.MALFORMED,
    }


def test_client_plus_server_auth_node_cert_is_what_a_node_has(
    ca: pki.CertificateAuthority,
) -> None:
    node, pem = forge(ca, usages=[CLIENT, SERVER])
    assert parse_peer(pem, fabric_id=ca.fabric_id, now=NOW).node_id == node


def test_identity_is_the_san_never_the_common_name(
    ca: pki.CertificateAuthority,
) -> None:
    victim = str(new_node_id())
    mine, pem = forge(ca, cn=victim)
    assert parse_peer(pem, fabric_id=ca.fabric_id, now=NOW).node_id == mine != victim


def test_two_node_uris_do_not_name_two_nodes(ca: pki.CertificateAuthority) -> None:
    a, b = new_node_id(), new_node_id()
    _, pem = forge(
        ca,
        sans=[
            x509.UniformResourceIdentifier(pki.node_uri(ca.fabric_id, str(a))),
            x509.UniformResourceIdentifier(pki.node_uri(ca.fabric_id, str(b))),
        ],
    )
    assert refused(pem, ca) is PeerCertReason.MALFORMED


def test_node_uri_plus_extra_dns_name_is_refused(ca: pki.CertificateAuthority) -> None:
    # a node cert that also claims to be the controller must never be accepted
    node = str(new_node_id())
    _, pem = forge(
        ca,
        node=node,
        sans=[
            x509.UniformResourceIdentifier(pki.node_uri(ca.fabric_id, node)),
            x509.DNSName(pki.SERVER_NAME),
        ],
    )
    assert refused(pem, ca) is PeerCertReason.MALFORMED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda uri: uri.upper(),
        lambda uri: uri + "/",
        lambda uri: uri + "?x=1",
        lambda uri: uri + "#frag",
        lambda uri: uri + " ",
        lambda uri: " " + uri,
        lambda uri: uri.replace("meshforge://", "https://"),
        lambda uri: uri[:-1],
        lambda uri: uri + "\u0430",
        lambda uri: uri.replace("/node/", "/node//"),
    ],
)
def test_malformed_node_uris_never_yield_a_node(
    ca: pki.CertificateAuthority, mutate: object
) -> None:
    node = str(new_node_id())
    uri = mutate(pki.node_uri(ca.fabric_id, node))  # type: ignore[operator]
    try:
        _, pem = forge(ca, node=node, sans=[x509.UniformResourceIdentifier(uri)])
    except (ValueError, TypeError):  # the builder may refuse a non-ASCII URI; fine
        return
    reason = refused(pem, ca)
    assert reason in {PeerCertReason.MALFORMED, PeerCertReason.WRONG_FABRIC}


def test_other_fabric_uri_signed_by_our_ca_is_wrong_fabric(
    ca: pki.CertificateAuthority,
) -> None:
    other = new_fabric_id()
    node = str(new_node_id())
    _, pem = forge(
        ca, node=node, sans=[x509.UniformResourceIdentifier(pki.node_uri(other, node))]
    )
    assert refused(pem, ca) is PeerCertReason.WRONG_FABRIC


def test_not_yet_valid_beyond_skew_is_refused(ca: pki.CertificateAuthority) -> None:
    _, pem = forge(ca, now=NOW + dt.timedelta(hours=2))
    assert refused(pem, ca) is PeerCertReason.EXPIRED


def test_expiry_is_exclusive_at_the_second(ca: pki.CertificateAuthority) -> None:
    _, pem = forge(
        ca, now=NOW - dt.timedelta(minutes=30), lifetime=dt.timedelta(minutes=30)
    )
    cert = pki.cert_from_pem(pem)
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=cert.not_valid_after_utc)
    assert info.value.reason is PeerCertReason.EXPIRED
    just_before = cert.not_valid_after_utc - dt.timedelta(seconds=1)
    assert parse_peer(pem, fabric_id=ca.fabric_id, now=just_before).node_id


def test_revoked_serial_match_is_exact_lowercase_hex(
    ca: pki.CertificateAuthority,
) -> None:
    _, pem = forge(ca)
    serial = format(pki.cert_from_pem(pem).serial_number, "x")
    assert refused(pem, ca, revoked={serial}) is PeerCertReason.REVOKED
    assert refused(pem, ca, revoked=frozenset([serial])) is PeerCertReason.REVOKED
    # a different set does not refuse; revoked check precedes nothing else it should not
    assert (
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW, revoked={"deadbeef"}).serial
        == serial
    )


def test_revoked_beats_a_valid_everything_else(ca: pki.CertificateAuthority) -> None:
    _, pem = forge(ca, usages=[CLIENT, SERVER])
    serial = format(pki.cert_from_pem(pem).serial_number, "x")
    assert refused(pem, ca, revoked=[serial]) is PeerCertReason.REVOKED


@pytest.mark.parametrize(
    "blob",
    [
        b"\x00" * 10,
        b"-----BEGIN CERTIFICATE-----\n"
        + b"A" * 100_000
        + b"\n-----END CERTIFICATE-----\n",
        b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        "é".encode() * 1000,
        b"-----BEGIN CERTIFICATE-----\n",
    ],
)
def test_garbage_pem_is_a_typed_refusal_not_a_crash(
    ca: pki.CertificateAuthority, blob: bytes
) -> None:
    assert refused(blob, ca) is PeerCertReason.MALFORMED


def test_pem_with_trailing_second_cert_names_the_first_only(
    ca: pki.CertificateAuthority,
) -> None:
    # A client could send leaf+extra; the identity must never become the second cert's.
    first, a = forge(ca)
    _second, b = forge(ca)
    peer = parse_peer(a + b, fabric_id=ca.fabric_id, now=NOW)
    assert peer.node_id == first
