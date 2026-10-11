"""V0.7 G4 deliverable 1 (D140) on the agent, without a controller.

* ``replace_node_cert``: one atomic swap of ``node.crt`` under the lock; a
  failure keeps the old certificate byte-identical; refuses without an
  enrolment;
* ``check_renewed``: a renewed certificate must come from the pinned CA, for
  the key the CSR was signed with, name this node, be valid now - anything
  else is ``bad_response`` and nothing is stored;
* key rotation: ``stage_rotation_key`` is idempotent and private;
  ``commit_rotation`` swaps key + node.json + certificate together, keeps
  node_id and epoch, refuses a key that was not staged; the journal is rolled
  forward after a crash past ``COMMIT`` and rolled back before it, so the trio
  is always consistent; ``next_epoch`` of a process still holding the old key
  continues with the rotated one (same node), and ``identity reset`` drops a
  staged key;
* ``renewal_due_at`` is 2/3 of the validity window.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest
from cryptography import x509

from meshforge.agent import enrollment, identity
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import (
    ROTATION_DIR_NAME,
    ROTATION_KEY_FILE_NAME,
    NodeIdentity,
    commit_rotation,
    load_or_create,
    next_epoch,
    stage_rotation_key,
    staged_rotation_key,
)
from meshforge.agent.session.renewal import renewal_due_at
from meshforge.common import pki
from meshforge.common.errors import IdentityError
from meshforge.common.ids import new_fabric_id, new_node_id

pytestmark = pytest.mark.unit


@pytest.fixture
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id())


@pytest.fixture
def ident(tmp_path: Path, ca: pki.CertificateAuthority) -> NodeIdentity:
    me = load_or_create(tmp_path / "identity")
    enrollment.store(me, _cert(ca, me.private_key, me.node_id), ca.cert, force=False)
    return me


def _cert(
    ca: pki.CertificateAuthority,
    key: object,
    node_id: str,
    *,
    lifetime: dt.timedelta = pki.NODE_CERT_LIFETIME,
) -> x509.Certificate:
    csr = pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node_id)  # type: ignore[arg-type]
    return pki.issue_node_cert(
        ca, csr, node_id=node_id, fabric_id=ca.fabric_id, lifetime=lifetime
    )


# ------------------------------------------------------------ replace + check


def test_replace_node_cert_swaps_atomically(
    ident: NodeIdentity, ca: pki.CertificateAuthority
) -> None:
    new = _cert(ca, ident.private_key, ident.node_id)
    enrollment.replace_node_cert(ident, new)
    stored = enrollment.load(ident)
    assert stored is not None and stored.node_cert.serial_number == new.serial_number
    assert not [p for p in ident.path.iterdir() if p.name.endswith(".tmp")]
    if os.name == "posix":
        assert (ident.path / "node.crt").stat().st_mode & 0o077 == 0


def test_a_failed_replace_keeps_the_old_certificate(
    ident: NodeIdentity, ca: pki.CertificateAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (ident.path / "node.crt").read_bytes()

    def boom(*_a: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(enrollment.os, "replace", boom)
    with pytest.raises(JoinError) as info:
        enrollment.replace_node_cert(ident, _cert(ca, ident.private_key, ident.node_id))
    assert info.value.reason is JoinReason.STORAGE
    assert (ident.path / "node.crt").read_bytes() == before
    assert not [p for p in ident.path.iterdir() if p.name.endswith(".tmp")]


def test_replace_refuses_without_an_enrolment(
    tmp_path: Path, ca: pki.CertificateAuthority
) -> None:
    bare = load_or_create(tmp_path / "bare")
    with pytest.raises(JoinError) as info:
        enrollment.replace_node_cert(bare, _cert(ca, bare.private_key, bare.node_id))
    assert info.value.reason is JoinReason.NOT_ENROLLED


def test_check_renewed_accepts_only_our_pinned_ca_key_and_node(
    ident: NodeIdentity, ca: pki.CertificateAuthority
) -> None:
    current = enrollment.load(ident)
    assert current is not None
    good = _cert(ca, ident.private_key, ident.node_id)
    ca_pem = pki.cert_to_pem(ca.cert)
    assert (
        enrollment.check_renewed(
            pki.cert_to_pem(good), ca_pem, current=current, key=ident.private_key
        ).serial_number
        == good.serial_number
    )
    other_ca = pki.create_ca(ca.fabric_id)  # same fabric id, another key
    other_key = pki.generate_key()
    bad = [
        # another CA comes back
        (
            pki.cert_to_pem(_cert(other_ca, ident.private_key, ident.node_id)),
            pki.cert_to_pem(other_ca.cert),
        ),
        # our CA comes back but another CA signed the certificate
        (pki.cert_to_pem(_cert(other_ca, ident.private_key, ident.node_id)), ca_pem),
        # a certificate for another key
        (pki.cert_to_pem(_cert(ca, other_key, ident.node_id)), ca_pem),
        # for another node
        (pki.cert_to_pem(_cert(ca, ident.private_key, str(new_node_id()))), ca_pem),
        # garbage
        (b"-----BEGIN CERTIFICATE-----\nxx\n-----END CERTIFICATE-----\n", ca_pem),
    ]
    for cert_pem, returned_ca in bad:
        with pytest.raises(JoinError) as info:
            enrollment.check_renewed(
                cert_pem, returned_ca, current=current, key=ident.private_key
            )
        assert info.value.reason is JoinReason.BAD_RESPONSE
    # Expired by the time it arrives.
    with pytest.raises(JoinError):
        enrollment.check_renewed(
            pki.cert_to_pem(good),
            ca_pem,
            current=current,
            key=ident.private_key,
            now=good.not_valid_after_utc + dt.timedelta(seconds=1),
        )


# ----------------------------------------------------------------- rotation


def test_stage_rotation_key_is_idempotent_and_private(ident: NodeIdentity) -> None:
    assert staged_rotation_key(ident.path) is None
    created, fp = stage_rotation_key(ident.path)
    again, fp2 = stage_rotation_key(ident.path)
    assert created and not again and fp == fp2
    staged = staged_rotation_key(ident.path)
    assert staged is not None and identity._fingerprint(staged) == fp
    if os.name == "posix":
        mode = (ident.path / ROTATION_KEY_FILE_NAME).stat().st_mode
        assert mode & 0o077 == 0
        (ident.path / ROTATION_KEY_FILE_NAME).chmod(0o644)
        with pytest.raises(IdentityError):
            staged_rotation_key(ident.path)


def test_commit_rotation_swaps_the_trio_and_keeps_node_id_and_epoch(
    ident: NodeIdentity, ca: pki.CertificateAuthority
) -> None:
    advanced = next_epoch(next_epoch(ident))
    stage_rotation_key(ident.path)
    new_key = staged_rotation_key(ident.path)
    assert new_key is not None
    cert = _cert(ca, new_key, ident.node_id)
    rotated = commit_rotation(ident, new_key, pki.cert_to_pem(cert))
    assert rotated.node_id == ident.node_id
    assert rotated.session_epoch == advanced.session_epoch
    assert rotated.fingerprint != ident.fingerprint
    reloaded = load_or_create(ident.path)
    assert reloaded.fingerprint == rotated.fingerprint
    meta = json.loads((ident.path / "node.json").read_text())
    assert meta["key_fingerprint_sha256"] == rotated.fingerprint
    stored = enrollment.load(reloaded)
    assert stored is not None and stored.node_cert.serial_number == cert.serial_number
    assert not (ident.path / ROTATION_KEY_FILE_NAME).exists()
    assert not (ident.path / ROTATION_DIR_NAME).exists()


def test_commit_refuses_a_key_that_was_not_staged(
    ident: NodeIdentity, ca: pki.CertificateAuthority
) -> None:
    rogue = pki.generate_key()
    with pytest.raises(IdentityError):
        commit_rotation(ident, rogue, pki.cert_to_pem(_cert(ca, rogue, ident.node_id)))
    stage_rotation_key(ident.path)
    with pytest.raises(IdentityError):
        commit_rotation(ident, rogue, pki.cert_to_pem(_cert(ca, rogue, ident.node_id)))
    assert load_or_create(ident.path).fingerprint == ident.fingerprint


@pytest.mark.parametrize("committed", [True, False])
def test_an_interrupted_rotation_is_finished_or_dropped_by_the_next_lock_holder(
    ident: NodeIdentity,
    ca: pki.CertificateAuthority,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    stage_rotation_key(ident.path)
    new_key = staged_rotation_key(ident.path)
    assert new_key is not None
    cert = _cert(ca, new_key, ident.node_id)
    old_files = {
        name: (ident.path / name).read_bytes()
        for name in ("node.key", "node.json", "node.crt")
    }

    class CrashError(Exception):
        pass

    real_write = identity._write_protected
    replaced: list[str] = []
    real_replace = os.replace

    def crash_before_commit(
        path: Path, data: bytes, *, exclusive: bool = False
    ) -> None:
        if path.name == "COMMIT":
            raise CrashError
        real_write(path, data, exclusive=exclusive)

    def crash_mid_swap(src: object, dst: object) -> None:
        if Path(str(dst)).parent == ident.path and Path(str(src)).parent.name == (
            ROTATION_DIR_NAME
        ):
            replaced.append(str(dst))
            if len(replaced) == 2:  # the key moved in, node.json not yet
                raise CrashError
        real_replace(src, dst)  # type: ignore[arg-type]

    if committed:
        monkeypatch.setattr(identity.os, "replace", crash_mid_swap)
    else:
        monkeypatch.setattr(identity, "_write_protected", crash_before_commit)
    with pytest.raises(CrashError):
        commit_rotation(ident, new_key, pki.cert_to_pem(cert))
    monkeypatch.undo()
    # The next process: load_or_create takes the lock and recovers.
    after = load_or_create(ident.path)
    assert after.node_id == ident.node_id
    stored = enrollment.load(after)  # key, json and certificate agree
    assert stored is not None
    if committed:
        assert after.fingerprint == identity._fingerprint(new_key)
        assert stored.node_cert.serial_number == cert.serial_number
        assert not (ident.path / ROTATION_KEY_FILE_NAME).exists()
    else:
        assert {
            name: (ident.path / name).read_bytes()
            for name in ("node.key", "node.json", "node.crt")
        } == old_files
        assert staged_rotation_key(ident.path) is not None  # retried later
    assert not (ident.path / ROTATION_DIR_NAME).exists()


def test_next_epoch_follows_a_rotation_of_the_same_node(
    ident: NodeIdentity, ca: pki.CertificateAuthority
) -> None:
    stage_rotation_key(ident.path)
    new_key = staged_rotation_key(ident.path)
    assert new_key is not None
    commit_rotation(ident, new_key, pki.cert_to_pem(_cert(ca, new_key, ident.node_id)))
    # ``ident`` is the old in-memory identity (old key).
    bumped = next_epoch(ident)
    assert bumped.node_id == ident.node_id
    assert bumped.fingerprint == identity._fingerprint(new_key)
    assert bumped.session_epoch == ident.session_epoch + 1


def test_next_epoch_still_refuses_another_node(ident: NodeIdentity) -> None:
    identity.reset(ident.path)
    load_or_create(ident.path)  # a fresh node_id
    with pytest.raises(IdentityError):
        next_epoch(ident)


def test_reset_drops_a_staged_key(ident: NodeIdentity) -> None:
    stage_rotation_key(ident.path)
    identity.reset(ident.path)
    assert not (ident.path / ROTATION_KEY_FILE_NAME).exists()


def test_renewal_due_at_is_two_thirds_of_the_window() -> None:
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=30)
    assert renewal_due_at(start, end) == start + dt.timedelta(days=20)
