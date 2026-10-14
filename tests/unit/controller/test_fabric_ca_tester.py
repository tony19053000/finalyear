# ruff: noqa: S104, SIM105, PT017  # hostile-input fixtures: wildcard hosts, SHA-1, bidi/unicode lookalikes
"""Independent tester suite for ``controller/fabric_ca.py`` (V0.7 G1, D134).

Hostile files on disk (symlinks, FIFOs, loose modes, torn/garbled/foreign material),
umask independence, expiry/clock edges, SAN churn, crash windows. Spec: brief §2 D134,
§4 items 19-22 (fail closed; no secret in errors). THREAT_MODEL rows LF-I1, LF-T3,
PK-D1, PK-S2.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from meshforge.common import pki
from meshforge.common.errors import ExitCode
from meshforge.common.ids import new_fabric_id, validate_fabric_id
from meshforge.controller.fabric_ca import (
    FabricCaError,
    FabricExistsError,
    FabricMissingError,
    ServerCertAction,
    create_fabric,
    ensure_server_cert,
    load_fabric,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX mode bits / symlinks / FIFOs"
)


@pytest.fixture
def ca_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "controller" / "ca"


@pytest.fixture
def tls_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "controller" / "tls"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _tree(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


# ------------------------------------------------------------------ permissions


@posix_only
@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o007])
def test_modes_do_not_depend_on_the_umask(
    ca_dir: Path, tls_dir: Path, umask: int
) -> None:
    old = os.umask(umask)
    try:
        fabric = create_fabric(ca_dir, now=NOW)
        ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    finally:
        os.umask(old)
    for folder in (ca_dir, tls_dir):
        assert _mode(folder) == 0o700, folder
        for f in folder.iterdir():
            assert _mode(f) == 0o600, f
    assert _mode(ca_dir.parent / "ca.lock") & 0o077 == 0
    assert _mode(ca_dir.parent / "tls.lock") & 0o077 == 0
    load_fabric(ca_dir, now=NOW)  # and the strict loader agrees


@posix_only
def test_create_under_a_missing_parent_makes_private_ca_dir(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "ca"
    create_fabric(deep, now=NOW)
    assert _mode(deep) == 0o700
    assert _mode(deep / "ca.key") == 0o600


@posix_only
@pytest.mark.parametrize("bits", [0o640, 0o604, 0o644, 0o666, 0o660, 0o606])
def test_any_group_or_other_bit_on_the_ca_key_is_refused(
    ca_dir: Path, bits: int
) -> None:
    create_fabric(ca_dir, now=NOW)
    (ca_dir / "ca.key").chmod(bits)
    with pytest.raises(FabricCaError) as exc:
        load_fabric(ca_dir, now=NOW)
    assert exc.value.exit_code == ExitCode.CONFIG_ERROR
    assert str(ca_dir / "ca.key") in str(exc.value)
    # the remedy is printed and no key text is
    assert "BEGIN" not in str(exc.value)


@posix_only
@pytest.mark.parametrize("bits", [0o750, 0o705, 0o755, 0o777])
def test_any_group_or_other_bit_on_the_ca_folder_is_refused(
    ca_dir: Path, bits: int
) -> None:
    create_fabric(ca_dir, now=NOW)
    ca_dir.chmod(bits)
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


@posix_only
def test_loose_ca_cert_is_tolerated_but_key_is_not(ca_dir: Path) -> None:
    """ca.crt is public by nature; only the key and the folder gate loading."""
    create_fabric(ca_dir, now=NOW)
    (ca_dir / "ca.crt").chmod(0o644)
    load_fabric(ca_dir, now=NOW)


@posix_only
def test_force_repairs_a_world_readable_ca_folder(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    ca_dir.chmod(0o755)
    (ca_dir / "ca.key").chmod(0o644)
    create_fabric(ca_dir, now=NOW, force=True)
    assert _mode(ca_dir) == 0o700 and _mode(ca_dir / "ca.key") == 0o600
    load_fabric(ca_dir, now=NOW)


@posix_only
def test_loose_server_key_or_folder_is_reissued_and_tightened(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    first = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    before = first.cert.fingerprint(hashes.SHA256())
    (tls_dir / "server.key").chmod(0o644)  # key leaked once: must not be kept
    second = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert second.action is ServerCertAction.REISSUED
    assert second.cert.fingerprint(hashes.SHA256()) != before
    assert _mode(tls_dir / "server.key") == 0o600
    tls_dir.chmod(0o755)
    third = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert third.action is ServerCertAction.REISSUED
    assert _mode(tls_dir) == 0o700


# --------------------------------------------------------------------- symlinks


@pytest.mark.security
@posix_only
def test_force_over_a_symlinked_ca_never_writes_or_deletes_through_it(
    tmp_path: Path, ca_dir: Path
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me")
    (victim / "ca.key").write_text("not mine")
    ca_dir.parent.mkdir(parents=True)
    ca_dir.symlink_to(victim, target_is_directory=True)
    with pytest.raises(FabricExistsError):  # present (even though not a CA)
        create_fabric(ca_dir, now=NOW)
    create_fabric(ca_dir, now=NOW, force=True)
    assert not ca_dir.is_symlink() and ca_dir.is_dir()
    assert (victim / "precious.txt").read_text() == "keep me"
    assert (victim / "ca.key").read_text() == "not mine"
    assert sorted(p.name for p in victim.iterdir()) == ["ca.key", "precious.txt"]
    load_fabric(ca_dir, now=NOW)


@posix_only
def test_dangling_symlink_at_ca_is_present_and_refused_then_forceable(
    tmp_path: Path, ca_dir: Path
) -> None:
    ca_dir.parent.mkdir(parents=True)
    ca_dir.symlink_to(tmp_path / "nowhere", target_is_directory=True)
    with pytest.raises(FabricExistsError):
        create_fabric(ca_dir, now=NOW)
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)
    create_fabric(ca_dir, now=NOW, force=True)
    load_fabric(ca_dir, now=NOW)
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.security
@posix_only
def test_symlinked_key_files_inside_ca_are_not_written_through_by_force(
    tmp_path: Path, ca_dir: Path
) -> None:
    create_fabric(ca_dir, now=NOW)
    elsewhere = tmp_path / "elsewhere.key"
    elsewhere.write_text("sentinel")
    elsewhere.chmod(0o600)
    (ca_dir / "ca.key").unlink()
    (ca_dir / "ca.key").symlink_to(elsewhere)
    try:
        load_fabric(ca_dir, now=NOW)
    except FabricCaError:
        pass  # refusing a symlinked key is fine; so is following it - but never writing
    create_fabric(ca_dir, now=NOW, force=True)
    assert elsewhere.read_text() == "sentinel"
    assert not (ca_dir / "ca.key").is_symlink()


@posix_only
def test_symlinked_key_to_a_world_readable_file_is_refused(
    tmp_path: Path, ca_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    shared = tmp_path / "shared.key"
    shared.write_bytes(pki.key_to_pem(fabric.authority.key))
    shared.chmod(0o644)
    (ca_dir / "ca.key").unlink()
    (ca_dir / "ca.key").symlink_to(shared)
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


@pytest.mark.security
@posix_only
def test_symlinked_tls_folder_is_never_written_through(
    tmp_path: Path, ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    attacker = tmp_path / "attacker_dir"
    attacker.mkdir(mode=0o777)
    attacker.chmod(0o777)  # not private: must not be trusted
    (attacker / "server.key").write_text("planted")
    tls_dir.symlink_to(attacker, target_is_directory=True)
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert got.action in {ServerCertAction.REISSUED, ServerCertAction.ISSUED}
    assert (attacker / "server.key").read_text() == "planted"
    assert sorted(p.name for p in attacker.iterdir()) == ["server.key"]
    assert not tls_dir.is_symlink()
    assert _mode(tls_dir) == 0o700


@posix_only
def test_tls_path_that_is_a_regular_file_is_replaced_by_a_folder(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    tls_dir.write_text("junk")
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert tls_dir.is_dir() and got.action is ServerCertAction.REISSUED
    again = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert again.action is ServerCertAction.KEPT


@pytest.mark.security
@posix_only
def test_ca_key_that_is_a_fifo_or_device_does_not_hang_and_is_refused(
    tmp_path: Path, ca_dir: Path
) -> None:
    create_fabric(ca_dir, now=NOW)
    (ca_dir / "ca.key").unlink()
    os.mkfifo(ca_dir / "ca.key", 0o600)
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from meshforge.controller.fabric_ca import load_fabric, FabricCaError
        try:
            load_fabric(Path({str(ca_dir)!r}))
        except FabricCaError:
            raise SystemExit(0)
        raise SystemExit(3)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=20, check=False
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.security
@posix_only
@pytest.mark.parametrize("victim", ["server.key", "server.crt"])
def test_fifo_in_tls_folder_does_not_hang_the_start(
    ca_dir: Path, tls_dir: Path, victim: str
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    (tls_dir / victim).unlink()
    os.mkfifo(tls_dir / victim, 0o600)
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from meshforge.controller.fabric_ca import load_fabric, ensure_server_cert
        fab = load_fabric(Path({str(ca_dir)!r}))
        ensure_server_cert(Path({str(tls_dir)!r}), fab, hosts=["10.0.0.1"])
        """
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, timeout=15, check=False
        )
    except subprocess.TimeoutExpired:
        pytest.fail("ensure_server_cert hung on a FIFO")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.security
@posix_only
def test_dev_zero_symlinked_as_server_key_is_bounded_and_reissued(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    (tls_dir / "server.key").unlink()
    (tls_dir / "server.key").symlink_to("/dev/zero")
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert got.action is ServerCertAction.REISSUED
    assert (tls_dir / "server.key").stat().st_size < pki.MAX_KEY_BYTES


def test_gigantic_ca_files_are_refused_without_reading_them_whole(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    with open(ca_dir / "ca.crt", "r+b") as fh:
        fh.truncate(2 * 1024**3)  # sparse 2 GiB
    with pytest.raises(FabricCaError, match="larger than"):
        load_fabric(ca_dir, now=NOW)


# ---------------------------------------------------- foreign / hostile CA material


def _forge_ca_cert(
    key: ec.EllipticCurvePrivateKey,
    signer: ec.EllipticCurvePrivateKey,
    *,
    ca: bool = True,
    fabric_id: str | None = None,
    ou_count: int = 1,
) -> bytes:
    fid = fabric_id or new_fabric_id()
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, "MeshForge fabric CA")]
    attrs += [x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, fid)] * ou_count
    name = x509.Name(attrs)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(11)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(signer, hashes.SHA256())
    )
    return pki.cert_to_pem(cert)


def _plant(ca_dir: Path, key: ec.EllipticCurvePrivateKey, cert_pem: bytes) -> None:
    ca_dir.mkdir(parents=True, mode=0o700)
    ca_dir.chmod(0o700)
    (ca_dir / "ca.key").write_bytes(pki.key_to_pem(key))
    (ca_dir / "ca.key").chmod(0o600)
    (ca_dir / "ca.crt").write_bytes(cert_pem)


def test_planted_ca_with_a_forged_self_signature_is_refused(ca_dir: Path) -> None:
    key, other = pki.generate_key(), pki.generate_key()
    _plant(ca_dir, key, _forge_ca_cert(key, other))  # pubkey matches key; sig does not
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


def test_planted_ca_that_is_not_a_ca_is_refused(ca_dir: Path) -> None:
    key = pki.generate_key()
    _plant(ca_dir, key, _forge_ca_cert(key, key, ca=False))
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


def test_planted_ca_with_a_bad_or_duplicated_fabric_id_is_refused(ca_dir: Path) -> None:
    key = pki.generate_key()
    _plant(ca_dir, key, _forge_ca_cert(key, key, fabric_id="not-a-fabric"))
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)
    import shutil

    shutil.rmtree(ca_dir)
    _plant(ca_dir, key, _forge_ca_cert(key, key, ou_count=2))
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


def test_p384_ca_key_is_refused(ca_dir: Path) -> None:
    key = ec.generate_private_key(ec.SECP384R1())
    ca_dir.mkdir(parents=True, mode=0o700)
    from cryptography.hazmat.primitives import serialization

    (ca_dir / "ca.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (ca_dir / "ca.key").chmod(0o600)
    ca_dir.chmod(0o700)
    (ca_dir / "ca.crt").write_bytes(
        pki.cert_to_pem(pki.create_ca(new_fabric_id(), now=NOW).cert)
    )
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b[: len(b) // 2],  # truncated
        lambda b: b + b,  # two PEM blocks
        lambda b: b.replace(b"\n", b"\r\n"),  # CRLF
        lambda b: b"\xef\xbb\xbf" + b,  # BOM
        lambda b: b"",
        lambda b: b"\x00" * 100,
    ],
    ids=["truncated", "doubled", "crlf", "bom", "empty", "nul"],
)
def test_mangled_ca_files_never_load_silently_or_crash(
    ca_dir: Path, mutate: object
) -> None:
    """Whatever the mangling, the result is either a clean load of the *same* CA or a
    typed FabricCaError - never another exception type, never a different identity."""
    original = create_fabric(ca_dir, now=NOW)
    for name in ("ca.crt", "ca.key"):
        path = ca_dir / name
        saved = path.read_bytes()
        path.write_bytes(mutate(saved))  # type: ignore[operator]
        try:
            loaded = load_fabric(ca_dir, now=NOW)
        except FabricCaError as exc:
            assert exc.exit_code == ExitCode.CONFIG_ERROR
            assert "PRIVATE KEY" not in str(exc)
        else:
            assert loaded.fingerprint == original.fingerprint
        path.write_bytes(saved)


# ------------------------------------------------------------- clock / expiry edges


def test_load_fabric_expiry_and_not_yet_valid_edges(ca_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW, lifetime=timedelta(days=30))
    nb, na = fabric.cert.not_valid_before_utc, fabric.cert.not_valid_after_utc
    load_fabric(ca_dir, now=nb)
    load_fabric(ca_dir, now=na - timedelta(microseconds=1))
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=na)
    with pytest.raises(FabricCaError):
        load_fabric(ca_dir, now=nb - timedelta(seconds=1))


def test_server_cert_renew_edge_and_ca_expiry_edge(ca_dir: Path, tls_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    first = ensure_server_cert(
        tls_dir, fabric, hosts=["10.0.0.1"], now=NOW, lifetime=timedelta(days=30)
    )
    start, end = first.cert.not_valid_before_utc, first.cert.not_valid_after_utc
    edge = start + (end - start) * pki.RENEW_FRACTION
    kept = ensure_server_cert(
        tls_dir, fabric, hosts=["10.0.0.1"], now=edge - timedelta(seconds=1)
    )
    assert kept.action is ServerCertAction.KEPT
    renewed = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=edge)
    assert renewed.action is ServerCertAction.RENEWED
    assert renewed.cert != first.cert
    # a start after the cert expired outright: re-issued, not kept
    late = renewed.cert.not_valid_after_utc + timedelta(hours=1)
    after = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=late)
    assert after.action is ServerCertAction.REISSUED and after.reason == "expired"
    # but never from an expired CA, and the existing files then stay untouched
    snapshot = _tree(tls_dir)
    with pytest.raises(FabricCaError):
        ensure_server_cert(
            tls_dir, fabric, hosts=["10.0.0.1"], now=fabric.cert.not_valid_after_utc
        )
    assert _tree(tls_dir) == snapshot


def test_clock_rolled_back_far_reissues_a_not_yet_valid_cert(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW + timedelta(days=100))
    got = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW + timedelta(days=1))
    assert got.action is ServerCertAction.REISSUED and got.reason == "not_yet_valid"


# ------------------------------------------------------------- server-cert hosts


def test_failed_issue_does_not_destroy_the_existing_server_cert(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    before = _tree(tls_dir)
    for bad in ("0.0.0.0", "*.lan", "", "a..b"):
        with pytest.raises(FabricCaError) as exc:
            ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1", bad], now=NOW)
        assert exc.value.exit_code == ExitCode.CONFIG_ERROR
    assert _tree(tls_dir) == before


def test_san_set_changes_always_reissue_and_then_settle(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    plans = [
        ["10.0.0.1"],
        ["10.0.0.1", "10.0.0.2"],
        ["10.0.0.2"],
        [],
        ["Host.LAN", "host.lan", "[::1]", "::1"],
        [pki.SERVER_NAME, "10.0.0.1", "10.0.0.1"],
    ]
    for hosts in plans:
        a = ensure_server_cert(tls_dir, fabric, hosts=hosts, now=NOW)
        assert a.action in {ServerCertAction.REISSUED, ServerCertAction.ISSUED}
        b = ensure_server_cert(tls_dir, fabric, hosts=hosts, now=NOW)
        assert b.action is ServerCertAction.KEPT, hosts  # no reissue loop
        assert b.cert == a.cert


@pytest.mark.security
def test_scoped_ipv6_host_does_not_reissue_forever(ca_dir: Path, tls_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["fe80::1%eth0"], now=NOW)
    again = ensure_server_cert(tls_dir, fabric, hosts=["fe80::1%eth0"], now=NOW)
    assert again.action is ServerCertAction.KEPT


@pytest.mark.security
def test_server_cert_from_a_same_subject_foreign_ca_is_reissued(
    ca_dir: Path, tls_dir: Path
) -> None:
    """Fabric A's tls/ pair planted under fabric A' that has the same fabric_id text."""
    fabric = create_fabric(ca_dir, now=NOW)
    clone = pki.create_ca(fabric.fabric_id, now=NOW)  # same subject, other key
    assert clone.cert.subject == fabric.cert.subject
    foreign = pki.issue_server_cert(clone, hosts=["10.0.0.1"], now=NOW)
    tls_dir.mkdir(parents=True, mode=0o700)
    tls_dir.chmod(0o700)
    (tls_dir / "server.key").write_bytes(pki.key_to_pem(foreign.key))
    (tls_dir / "server.crt").write_bytes(pki.cert_to_pem(foreign.cert))
    for f in tls_dir.iterdir():
        f.chmod(0o600)
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert got.action is ServerCertAction.REISSUED and got.reason == "other_ca"
    pki.verify_issued_by(got.cert, fabric.cert)
    assert got.cert != foreign.cert


def test_mismatched_server_key_and_cert_is_reissued(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    (tls_dir / "server.key").write_bytes(pki.key_to_pem(pki.generate_key()))
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert got.action is ServerCertAction.REISSUED
    assert pki.public_keys_equal(got.key.public_key(), got.cert.public_key())


# ------------------------------------------------------------ create semantics


def test_each_create_is_a_fresh_identity_and_force_changes_everything(
    ca_dir: Path,
) -> None:
    a = create_fabric(ca_dir, now=NOW)
    validate_fabric_id(a.fabric_id)
    assert a.fabric_id.startswith("fab_")
    with pytest.raises(FabricExistsError) as exc:
        create_fabric(ca_dir, now=NOW)
    assert exc.value.exit_code == ExitCode.CONFLICT
    snapshot = _tree(ca_dir)
    b = create_fabric(ca_dir, now=NOW, force=True)
    assert b.fabric_id != a.fabric_id and b.fingerprint != a.fingerprint
    assert _tree(ca_dir) != snapshot
    assert not pki.public_keys_equal(
        a.authority.key.public_key(), b.authority.key.public_key()
    )


def test_refused_create_changes_nothing_on_disk(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    before = {p: p.stat().st_mtime_ns for p in ca_dir.iterdir()}
    snapshot = _tree(ca_dir)
    for _ in range(3):
        with pytest.raises(FabricExistsError):
            create_fabric(ca_dir, now=NOW)
    assert _tree(ca_dir) == snapshot
    assert {p: p.stat().st_mtime_ns for p in ca_dir.iterdir()} == before
    assert sorted(p.name for p in ca_dir.parent.iterdir()) == ["ca", "ca.lock"]


def test_create_over_a_garbage_ca_folder_needs_force(ca_dir: Path) -> None:
    ca_dir.mkdir(parents=True)
    (ca_dir / "ca.key").write_text("garbage")
    with pytest.raises(FabricExistsError):
        create_fabric(ca_dir, now=NOW)
    create_fabric(ca_dir, now=NOW, force=True)
    load_fabric(ca_dir, now=NOW)


def test_missing_ca_error_names_the_remedy(ca_dir: Path) -> None:
    with pytest.raises(FabricMissingError, match="fabric create"):
        load_fabric(ca_dir, now=NOW)


# ------------------------------------------------------------------ crash windows


@posix_only
@pytest.mark.parametrize("nth_rename", [1, 2])
def test_hard_kill_at_each_rename_of_a_server_cert_renewal(
    ca_dir: Path, tls_dir: Path, nth_rename: int
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    first = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from meshforge.controller.fabric_ca import load_fabric, ensure_server_cert
        real, n = os.rename, [0]
        def rename(a, b):
            n[0] += 1
            if n[0] == {nth_rename}:
                os._exit(9)
            real(a, b)
        os.rename = rename
        fab = load_fabric(Path({str(ca_dir)!r}))
        ensure_server_cert(Path({str(tls_dir)!r}), fab, hosts=["10.0.0.1", "10.0.0.9"])
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=60, check=False
    )
    assert proc.returncode == 9, proc.stderr
    # whatever state the crash left, the next start converges to a valid cert
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1", "10.0.0.9"])
    assert pki.cert_sans(got.cert) == [pki.SERVER_NAME, "10.0.0.1", "10.0.0.9"]
    assert pki.public_keys_equal(got.key.public_key(), got.cert.public_key())
    assert got.cert != first.cert
    assert [p.name for p in tls_dir.parent.iterdir() if p.name.startswith(".")] == []


# ---------------------------------------------------------- no secrets in messages


@pytest.mark.parametrize("which", ["ca.key", "ca.crt"])
def test_error_messages_never_contain_file_contents(ca_dir: Path, which: str) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    marker = "SUPERSECRETMARKER" + "Q" * 40
    pem = pki.key_to_pem(fabric.authority.key).decode()
    body = pem.splitlines()[1]
    lines = pem.splitlines()
    if which == "ca.key":  # corrupt the body: marker inside the PEM armour
        content = "\n".join([lines[0], marker, *lines[1:]]) + "\n"
    else:
        content = f"{marker}\ngarbage"
    (ca_dir / which).write_text(content)
    with pytest.raises(FabricCaError) as exc:
        load_fabric(ca_dir, now=NOW)
    text = f"{exc.value} {exc.value!r} {exc.value.details}"
    assert marker not in text and body not in text and "BEGIN" not in text
