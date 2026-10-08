"""V0.7 G2 tester: the agent side of joining - pin edge cases, hostile controller
responses, and the crash/partial-write behaviour of ``node.crt`` / ``ca.crt``.

Derived from brief 5b item 4 ("store node.crt + ca.crt ... atomic, locked ...
refuse to overwrite without --force") and checklist 19-22. Real files, a real
killed process for the crash cases; no network.
"""

# ruff: noqa: PT017  (tester file: adversarial literals, blunt asserts)

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from meshforge.agent import enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX modes / symlinks / SIGKILL"
)


@pytest.fixture
def identity(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "identity")


def _cert(ca: pki.CertificateAuthority, identity: NodeIdentity, **kw: object):  # type: ignore[no-untyped-def]
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


def _resp(ca, cert, node_id):  # type: ignore[no-untyped-def]
    return enroll_pb2.EnrollResponse(
        node_cert_pem=pki.cert_to_pem(cert),
        ca_cert_pem=pki.cert_to_pem(ca.cert),
        fabric_id=ca.fabric_id,
        node_id=node_id,
    )


# ----------------------------------------------------------------------- pin_ca


def test_pin_ca_rejects_clone_of_the_ca_with_another_key() -> None:
    """Same subject DN and fabric as the pinned CA, other key: pin differs."""
    real = pki.create_ca(new_fabric_id())
    fake = pki.create_ca(real.fabric_id)
    server = pki.issue_server_cert(fake, hosts=["h"]).cert
    with pytest.raises(JoinError) as info:
        enrollment.pin_ca([server, fake.cert], pki.short_fingerprint(real.cert))
    assert info.value.reason is JoinReason.PIN_MISMATCH


def test_pin_ca_rejects_expired_ca_and_non_ca_self_signed_match() -> None:
    ca = pki.create_ca(new_fabric_id())
    server = pki.issue_server_cert(ca, hosts=["h"]).cert
    pin = pki.short_fingerprint(ca.cert)
    assert enrollment.pin_ca([server, ca.cert], pin) == ca.cert
    # A self-signed *leaf* (basicConstraints CA=false) whose own hash is the pin
    key = pki.generate_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(JoinError):
        enrollment.pin_ca([leaf], pki.short_fingerprint(leaf))


def test_pin_ca_pin_must_be_full_128_bits_not_a_prefix() -> None:
    ca = pki.create_ca(new_fabric_id())
    server = pki.issue_server_cert(ca, hosts=["h"]).cert
    pin = pki.short_fingerprint(ca.cert)
    for bad in (pin[:16], pin[:-1], "", pin.upper()[:-2] + "00", "0" * 32, "x" * 32):
        with pytest.raises((JoinError, ValueError)):
            enrollment.pin_ca([server, ca.cert], bad)


def test_pin_ca_flipping_any_single_pin_bit_is_a_mismatch() -> None:
    ca = pki.create_ca(new_fabric_id())
    server = pki.issue_server_cert(ca, hosts=["h"]).cert
    pin = int(pki.short_fingerprint(ca.cert), 16)
    for bit in range(128):
        flipped = format(pin ^ (1 << bit), "032x")
        with pytest.raises(JoinError) as info:
            enrollment.pin_ca([server, ca.cert], flipped)
        assert info.value.reason is JoinReason.PIN_MISMATCH


# ----------------------------------------------------------- hostile controller


def test_hostile_controller_responses_store_nothing(
    identity: NodeIdentity, tmp_path: Path
) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=40)
    ca = pki.create_ca(new_fabric_id(), now=long_ago)
    other = pki.create_ca(new_fabric_id())
    good = _cert(ca, identity)
    expired = _cert(ca, identity, now=long_ago, lifetime=timedelta(days=1))
    future = _cert(ca, identity, now=datetime.now(UTC) + timedelta(days=2))
    stranger_key_cert = _cert(ca, load_or_create(tmp_path / "other-id"))
    cases = {
        "expired": _resp(ca, expired, identity.node_id),
        "not yet valid": _resp(ca, future, identity.node_id),
        "someone else's key": _resp(ca, stranger_key_cert, identity.node_id),
        "ca swapped for the attacker's": enroll_pb2.EnrollResponse(
            node_cert_pem=pki.cert_to_pem(good),
            ca_cert_pem=pki.cert_to_pem(other.cert),
            fabric_id=ca.fabric_id,
            node_id=identity.node_id,
        ),
        "fabric id lies": enroll_pb2.EnrollResponse(
            node_cert_pem=pki.cert_to_pem(good),
            ca_cert_pem=pki.cert_to_pem(ca.cert),
            fabric_id=str(new_fabric_id()),
            node_id=identity.node_id,
        ),
        "empty": enroll_pb2.EnrollResponse(),
        "garbage pem": enroll_pb2.EnrollResponse(
            node_cert_pem=b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
            ca_cert_pem=pki.cert_to_pem(ca.cert),
            fabric_id=ca.fabric_id,
            node_id=identity.node_id,
        ),
        "huge pem": enroll_pb2.EnrollResponse(
            node_cert_pem=b"A" * 5_000_000,
            ca_cert_pem=pki.cert_to_pem(ca.cert),
            fabric_id=ca.fabric_id,
            node_id=identity.node_id,
        ),
    }
    for name, response in cases.items():
        try:
            enrollment.check_issued(response, pinned_ca=ca.cert, identity=identity)
        except JoinError as exc:
            assert exc.reason is JoinReason.BAD_RESPONSE, name
            assert "nothing was stored" in exc.message
        except pki.PkiError as exc:  # pragma: no cover - would be a defect
            pytest.fail(f"{name}: raw PkiError escaped check_issued: {exc}")
    assert not enrollment.is_enrolled(identity.path)


# -------------------------------------------------------------- storage atomicity


def _enrol(identity: NodeIdentity, ca=None, **kw: object):  # type: ignore[no-untyped-def]
    ca = ca or pki.create_ca(new_fabric_id())
    cert = _cert(ca, identity)
    enrollment.store(identity, cert, ca.cert, force=bool(kw.get("force")))
    return ca, cert


def test_second_store_without_force_changes_nothing(identity: NodeIdentity) -> None:
    _enrol(identity)
    before = {n: (identity.path / n).read_bytes() for n in ("node.crt", "ca.crt")}
    ca2 = pki.create_ca(new_fabric_id())
    with pytest.raises(JoinError) as info:
        enrollment.store(identity, _cert(ca2, identity), ca2.cert, force=False)
    assert info.value.reason is JoinReason.ALREADY_ENROLLED
    assert before == {n: (identity.path / n).read_bytes() for n in before}
    assert enrollment.load(identity) is not None


@posix_only
def test_files_are_owner_only_even_under_a_permissive_umask(
    identity: NodeIdentity,
) -> None:
    old = os.umask(0)
    try:
        _enrol(identity)
    finally:
        os.umask(old)
    for name in ("node.crt", "ca.crt", "node.key"):
        mode = stat.S_IMODE((identity.path / name).stat().st_mode)
        assert mode == 0o600, (name, oct(mode))
    assert enrollment.load(identity) is not None


@posix_only
def test_symlinked_ca_crt_is_replaced_not_written_through(
    identity: NodeIdentity, tmp_path: Path
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (identity.path / "ca.crt").symlink_to(victim)
    _enrol(identity)
    assert victim.read_text() == "precious"
    assert not (identity.path / "ca.crt").is_symlink()


@posix_only
def test_force_over_a_symlinked_node_crt_does_not_touch_the_target(
    identity: NodeIdentity, tmp_path: Path
) -> None:
    victim = tmp_path / "victim2.txt"
    victim.write_text("precious")
    (identity.path / "node.crt").symlink_to(victim)
    ca = pki.create_ca(new_fabric_id())
    with pytest.raises(JoinError) as info:
        enrollment.store(identity, _cert(ca, identity), ca.cert, force=False)
    assert info.value.reason is JoinReason.ALREADY_ENROLLED
    enrollment.store(identity, _cert(ca, identity), ca.cert, force=True)
    assert victim.read_text() == "precious"
    assert not (identity.path / "node.crt").is_symlink()


@posix_only
def test_read_only_directory_is_a_storage_error_with_no_partial_files(
    identity: NodeIdentity,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    ca = pki.create_ca(new_fabric_id())
    cert = _cert(ca, identity)
    identity.path.chmod(0o500)
    try:
        with pytest.raises(JoinError) as info:
            enrollment.store(identity, cert, ca.cert, force=False)
    finally:
        identity.path.chmod(0o700)
    assert info.value.reason is JoinReason.STORAGE
    assert not (identity.path / "node.crt").exists()
    assert not (identity.path / "ca.crt").exists()
    assert enrollment.load(identity) is None


def test_ca_crt_blocked_by_a_directory_leaves_the_laptop_not_enrolled(
    identity: NodeIdentity,
) -> None:
    (identity.path / "ca.crt").mkdir()
    ca = pki.create_ca(new_fabric_id())
    with pytest.raises(JoinError) as info:
        enrollment.store(identity, _cert(ca, identity), ca.cert, force=False)
    assert info.value.reason is JoinReason.STORAGE
    assert not (identity.path / "node.crt").exists()
    assert enrollment.load(identity) is None


def test_unicode_and_space_paths(tmp_path: Path) -> None:
    ident = load_or_create(tmp_path / "Mein Ordner ü日本" / "identity dir")
    _enrol(ident)
    assert enrollment.load(ident) is not None


def test_concurrent_stores_leave_one_consistent_pair(identity: NodeIdentity) -> None:
    cas = [pki.create_ca(new_fabric_id()) for _ in range(8)]
    certs = [_cert(ca, identity) for ca in cas]
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            enrollment.store(identity, certs[i], cas[i].cert, force=False)
            outcomes.append("ok")
        except JoinError as exc:
            outcomes.append(exc.reason.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert outcomes.count("ok") == 1, outcomes
    assert set(outcomes) == {"ok", "already_enrolled"}
    assert enrollment.load(identity) is not None  # the pair matches itself


_CRASH = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from meshforge.agent import enrollment
    from meshforge.agent.identity import load_or_create
    from meshforge.common import pki
    from meshforge.common.ids import new_fabric_id

    idir, mode, step = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    identity = load_or_create(idir)
    ca = pki.create_ca(new_fabric_id())
    csr = pki.build_csr(identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id)
    cert = pki.issue_node_cert(ca, csr, node_id=identity.node_id, fabric_id=ca.fabric_id)
    seen = []

    def point(name):
        seen.append(name)
        if name == step:
            os._exit(9)          # power cut right before this step

    real_stage, real_replace, real_unlink = enrollment._stage, os.replace, Path.unlink

    def stage(target, data, staged):
        point(f"stage{sum(n.startswith('stage') for n in seen) + 1}")
        return real_stage(target, data, staged)

    def replace(src, dst):
        if Path(dst).parent == idir:
            point(f"replace{sum(n.startswith('replace') for n in seen) + 1}")
        return real_replace(src, dst)

    def unlink(self, missing_ok=False):
        if self == idir / "node.crt":
            point("unlink")
        return real_unlink(self, missing_ok=missing_ok)

    enrollment._stage = stage
    os.replace = replace
    Path.unlink = unlink
    enrollment.store(identity, cert, ca.cert, force=(mode == "force"))
    """
)
_FRESH_STEPS = ["stage1", "stage2", "replace1", "replace2"]
_FORCE_STEPS = ["stage1", "stage2", "unlink", "replace1", "replace2"]


def _run_crash(idir: Path, mode: str, step: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", _CRASH, str(idir), mode, step],
        capture_output=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("step", _FRESH_STEPS)
def test_process_death_during_a_fresh_join_never_looks_enrolled(
    tmp_path: Path, step: str
) -> None:
    idir = tmp_path / "identity"
    done = _run_crash(idir, "fresh", step)
    assert done.returncode == 9, done.stderr
    identity = load_or_create(idir)
    assert enrollment.load(identity) is None  # not enrolled, not "invalid"
    leftovers = sorted(p.name for p in idir.iterdir())
    assert "node.crt" not in leftovers
    # and the laptop can simply join again, no --force needed
    _enrol(identity)
    assert enrollment.load(identity) is not None


@pytest.mark.parametrize("step", _FORCE_STEPS)
def test_process_death_during_force_rejoin_never_pairs_old_cert_with_new_ca(
    tmp_path: Path, step: str
) -> None:
    """D153 item 2: dying while the new pair is being *written* keeps the old
    enrolment (it still loads, unchanged); dying while it is being *swapped in*
    leaves "not enrolled". Never an old node.crt next to the new CA."""
    idir = tmp_path / "identity"
    identity = load_or_create(idir)
    _enrol(identity)
    old = enrollment.load(identity)
    assert old is not None
    done = _run_crash(idir, "force", step)
    assert done.returncode == 9, done.stderr
    state = enrollment.load(identity)  # never raises invalid_enrollment
    if step.startswith("stage") or step == "unlink":  # crash *before* the step
        assert state is not None
        assert state.cert_fingerprint == old.cert_fingerprint
        assert state.ca_fingerprint == old.ca_fingerprint
    else:
        assert state is None, "a half-swapped --force must leave 'not enrolled'"
        assert enrollment.is_enrolled(idir) is False
