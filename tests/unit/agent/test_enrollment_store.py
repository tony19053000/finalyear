"""V0.7 G2 agent side (D136, D149a): pinning, response checks and the stored
enrolment - no network, no controller.

* ``pin_ca``: only a self-signed CA whose SHA-256 starts with the pin AND that
  issued the presented server cert; a matching leaf, another CA, an empty chain
  are ``pin_mismatch``;
* ``check_issued``: the returned cert must come from the pinned CA, carry our
  key, name our node and fabric, be valid; the returned CA must be the pinned one;
* ``store``/``load``: owner-only files beside ``node.key``; refuse to overwrite
  without ``--force``; a hostile ``node.crt`` (symlink, FIFO, loose mode, another
  key/node/CA, missing ``ca.crt``) is ``invalid_enrollment``;
* ``identity reset`` removes the enrolment too.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509

from meshforge.agent import enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create, reset
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX FIFOs / modes / symlinks"
)


@pytest.fixture
def identity(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "identity")


@pytest.fixture
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id())


def _issue(
    ca: pki.CertificateAuthority, identity: NodeIdentity, **kw: object
) -> x509.Certificate:
    csr = pki.build_csr(
        identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
    )
    return pki.issue_node_cert(
        ca,
        csr,
        node_id=identity.node_id,
        fabric_id=ca.fabric_id,
        **kw,  # type: ignore[arg-type]
    )


def _response(
    ca: pki.CertificateAuthority, cert: x509.Certificate, node_id: str
) -> enroll_pb2.EnrollResponse:
    return enroll_pb2.EnrollResponse(
        node_cert_pem=pki.cert_to_pem(cert),
        ca_cert_pem=pki.cert_to_pem(ca.cert),
        fabric_id=ca.fabric_id,
        node_id=node_id,
    )


# ---------------------------------------------------------------------- pin_ca


@pytest.mark.security
def test_pin_ca_accepts_only_the_pinned_issuing_ca(
    ca: pki.CertificateAuthority,
) -> None:
    server = pki.issue_server_cert(ca, hosts=["127.0.0.1"]).cert
    pin = pki.short_fingerprint(ca.cert)
    assert enrollment.pin_ca([server, ca.cert], pin) == ca.cert
    other = pki.create_ca(new_fabric_id())
    for chain, wanted in (
        ([server, ca.cert], pki.short_fingerprint(other.cert)),  # another fabric
        ([server, other.cert], pin),  # pinned CA not presented
        ([server], pki.short_fingerprint(server)),  # a leaf matching its own pin
        ([], pin),
        # The right CA, but the server cert was issued by another one.
        ([pki.issue_server_cert(other, hosts=["h"]).cert, ca.cert], pin),
    ):
        with pytest.raises(JoinError) as info:
            enrollment.pin_ca(chain, wanted)
        assert info.value.reason is JoinReason.PIN_MISMATCH


# ---------------------------------------------------------------- check_issued


def test_check_issued_accepts_our_cert(
    ca: pki.CertificateAuthority, identity: NodeIdentity
) -> None:
    cert = _issue(ca, identity)
    got, fabric_id = enrollment.check_issued(
        _response(ca, cert, identity.node_id), pinned_ca=ca.cert, identity=identity
    )
    assert got == cert and fabric_id == ca.fabric_id


@pytest.mark.security
def test_check_issued_refusals(
    ca: pki.CertificateAuthority, identity: NodeIdentity, tmp_path: Path
) -> None:
    other_ca = pki.create_ca(ca.fabric_id)  # same fabric text, other key
    stranger = load_or_create(tmp_path / "stranger")
    good = _issue(ca, identity)
    cases = {
        "other CA signed": _response(
            other_ca, _issue(other_ca, identity), identity.node_id
        ),
        "another node's cert": _response(ca, _issue(ca, stranger), identity.node_id),
        "response names another node": _response(ca, good, str(new_node_id())),
        "garbage": enroll_pb2.EnrollResponse(
            node_cert_pem=b"nope", ca_cert_pem=pki.cert_to_pem(ca.cert)
        ),
    }
    wrong_fabric = _response(ca, good, identity.node_id)
    wrong_fabric.fabric_id = str(new_fabric_id())
    cases["response names another fabric"] = wrong_fabric
    for name, response in cases.items():
        with pytest.raises(JoinError) as info:
            enrollment.check_issued(response, pinned_ca=ca.cert, identity=identity)
        assert info.value.reason is JoinReason.BAD_RESPONSE, name
    old = pki.create_ca(ca.fabric_id, now=datetime.now(UTC) - timedelta(days=90))
    expired = _issue(old, identity, now=datetime.now(UTC) - timedelta(days=60))
    with pytest.raises(JoinError, match="expired"):
        enrollment.check_issued(
            _response(old, expired, identity.node_id),
            pinned_ca=old.cert,
            identity=identity,
        )
    # A cert carrying another key but our node_id (built by hand).
    foreign = pki.generate_key()
    csr = pki.build_csr(foreign, fabric_id=ca.fabric_id, node_id=identity.node_id)
    their = pki.issue_node_cert(
        ca, csr, node_id=identity.node_id, fabric_id=ca.fabric_id
    )
    with pytest.raises(JoinError, match="key_mismatch"):
        enrollment.check_issued(
            _response(ca, their, identity.node_id), pinned_ca=ca.cert, identity=identity
        )


# --------------------------------------------------------------- store / load


@pytest.mark.security
def test_store_load_and_force(
    ca: pki.CertificateAuthority, identity: NodeIdentity
) -> None:
    assert enrollment.load(identity) is None
    cert = _issue(ca, identity)
    enrollment.store(identity, cert, ca.cert, force=False)
    loaded = enrollment.load(identity)
    assert loaded is not None
    assert loaded.fabric_id == ca.fabric_id and loaded.node_cert == cert
    if os.name == "posix":
        for name in ("node.crt", "ca.crt"):
            assert (identity.path / name).stat().st_mode & 0o777 == 0o600
    with pytest.raises(JoinError) as info:
        enrollment.store(identity, cert, ca.cert, force=False)
    assert info.value.reason is JoinReason.ALREADY_ENROLLED
    again = _issue(ca, identity)
    enrollment.store(identity, again, ca.cert, force=True)
    reloaded = enrollment.load(identity)
    assert reloaded is not None and reloaded.node_cert == again


def _assert_invalid(identity: NodeIdentity) -> None:
    with pytest.raises(JoinError) as info:
        enrollment.load(identity)
    assert info.value.reason is JoinReason.INVALID_ENROLLMENT
    assert "join --force" in info.value.message


@pytest.mark.security
def test_load_refuses_a_cert_for_another_key_or_ca(
    ca: pki.CertificateAuthority, identity: NodeIdentity, tmp_path: Path
) -> None:
    stranger = load_or_create(tmp_path / "stranger")
    enrollment.store(identity, _issue(ca, stranger), ca.cert, force=False)
    _assert_invalid(identity)
    other = pki.create_ca(new_fabric_id())
    enrollment.store(identity, _issue(ca, identity), other.cert, force=True)
    _assert_invalid(identity)
    (identity.path / "ca.crt").unlink()
    _assert_invalid(identity)


@pytest.mark.security
@posix_only
def test_load_refuses_hostile_files(
    ca: pki.CertificateAuthority, identity: NodeIdentity, tmp_path: Path
) -> None:
    cert = _issue(ca, identity)
    enrollment.store(identity, cert, ca.cert, force=False)
    node_crt = identity.path / "node.crt"
    node_crt.chmod(0o644)
    _assert_invalid(identity)
    node_crt.chmod(0o600)
    real = tmp_path / "elsewhere.crt"
    node_crt.rename(real)
    node_crt.symlink_to(real)
    _assert_invalid(identity)
    node_crt.unlink()
    os.mkfifo(node_crt, 0o600)
    _assert_invalid(identity)  # O_NONBLOCK: returns, never hangs


@pytest.mark.security
def test_reset_removes_the_enrolment(
    ca: pki.CertificateAuthority, identity: NodeIdentity
) -> None:
    enrollment.store(identity, _issue(ca, identity), ca.cert, force=False)
    reset(identity.path)
    assert not (identity.path / "node.crt").exists()
    assert not (identity.path / "ca.crt").exists()
    fresh = load_or_create(identity.path)
    assert enrollment.load(fresh) is None


def test_join_refuses_a_bad_code_and_an_existing_enrolment_locally(
    ca: pki.CertificateAuthority, identity: NodeIdentity
) -> None:
    with pytest.raises(JoinError) as info:
        enrollment.join(identity, "mf1-not-a-code")
    assert info.value.reason is JoinReason.BAD_CODE
    assert info.value.exit_code_for == 2
    enrollment.store(identity, _issue(ca, identity), ca.cert, force=False)
    from meshforge.common import join_code

    code = join_code.encode(
        join_code.JoinCode(
            host="127.0.0.1", port=9, token=join_code.new_token(), ca_pin="a" * 32
        )
    )
    with pytest.raises(JoinError) as info:  # before any connection is tried
        enrollment.join(identity, code)
    assert info.value.reason is JoinReason.ALREADY_ENROLLED
