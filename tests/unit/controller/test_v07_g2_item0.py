"""V0.7 G2 item 0 (D149): the G1 reviewer's non-blocking findings, each with a test.

(a) one bounded, regular-file-only, owner-checked reader for every secret file
    (``common.private_files.read_bounded`` / ``read_private``): CA key (via
    ``pki``), ``peer_signing.key``, ``fabric.token``; the agent's ``node.crt`` is
    tested with the agent (``tests/unit/agent/test_enrolment_store.py``).
(b) duplicated / unsupported extensions in a certificate that *verifies*:
    server cert -> re-issued ``malformed``; CA -> typed ``config_error``;
    ``fabric show`` -> a typed result, never a traceback (reviewer N-1).
(c) a crash between "old CA aside" and "new CA in" leaves ``.ca.old-*`` and no
    ``ca/``: the old CA is restored, never deleted (reviewer N-2).
(d) ``fingerprint_matches(require_full=True)`` never accepts the 128-bit form.
(f) ``fabric_present`` is the CA question only; the TLS folder has its own.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from mf_hostile_certs import (
    as_pem,
    duplicate_extension,
    unsupported_general_name,
)
from typer.testing import CliRunner

from meshforge.common import pki
from meshforge.common.errors import ExitCode, FailureType
from meshforge.common.fabric_token import FabricTokenError, read_token_file
from meshforge.common.ids import new_fabric_id
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    read_bounded,
    read_private,
)
from meshforge.controller import fabric_ca
from meshforge.controller.app import app as controller_app
from meshforge.controller.fabric_ca import (
    FabricCaError,
    FabricExistsError,
    ServerCertAction,
    create_fabric,
    ensure_server_cert,
    load_fabric,
    recover_interrupted,
)
from meshforge.controller.peer_signing import (
    PeerSigningKeyError,
    load_or_create_signing_key,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX FIFOs / symlinks / uids"
)


@pytest.fixture(autouse=True)
def _no_meshforge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)


# ------------------------------------------------------------------ (a) readers


@posix_only
def test_read_bounded_refuses_symlink_fifo_device_and_folder(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    cases = {
        link: PrivateFileReason.NOT_REGULAR,
        fifo: PrivateFileReason.NOT_REGULAR,  # O_NONBLOCK: the open never hangs
        Path("/dev/null"): PrivateFileReason.NOT_REGULAR,
        tmp_path: PrivateFileReason.NOT_REGULAR,
        tmp_path / "absent": PrivateFileReason.MISSING,
    }
    for path, reason in cases.items():
        with pytest.raises(PrivateFileError) as info:
            read_bounded(path, limit=16, what="test file")
        assert info.value.reason is reason, path
        assert info.value.failure_type is FailureType.CONFIG_ERROR
    # A symlink a *user* pointed a tool at may be followed when asked to.
    assert read_bounded(link, limit=16, what="copy", follow_symlinks=True)[0] == b"x"


def test_read_bounded_size_bound_max_and_max_plus_one(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"a" * 64)
    assert read_bounded(path, limit=64, what="f")[0] == b"a" * 64
    with pytest.raises(PrivateFileError) as info:
        read_bounded(path, limit=63, what="f")
    assert info.value.reason is PrivateFileReason.OVERSIZED


@pytest.mark.security
@posix_only
def test_read_private_refuses_loose_mode_and_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    path.write_bytes(b"s")
    path.chmod(0o600)
    assert read_private(path, limit=8, what="secret") == b"s"
    path.chmod(0o640)
    with pytest.raises(PrivateFileError, match="mode 0640") as info:
        read_private(path, limit=8, what="secret")
    assert info.value.reason is PrivateFileReason.NOT_PRIVATE
    path.chmod(0o600)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    with pytest.raises(PrivateFileError, match="owned by another user"):
        read_private(path, limit=8, what="secret")


@pytest.mark.security
@posix_only
def test_peer_signing_key_uses_the_strict_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "controller" / "peer_signing.key"
    load_or_create_signing_key(key_path)
    load_or_create_signing_key(key_path)  # loads fine
    real = key_path.with_name("real.key")
    key_path.rename(real)
    key_path.symlink_to(real)
    with pytest.raises(PeerSigningKeyError, match="symlink"):
        load_or_create_signing_key(key_path)
    key_path.unlink()
    os.mkfifo(key_path, 0o600)
    with pytest.raises(PeerSigningKeyError, match="not a regular file"):
        load_or_create_signing_key(key_path)
    key_path.unlink()
    real.rename(key_path)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    with pytest.raises(PeerSigningKeyError, match="owned by another user"):
        load_or_create_signing_key(key_path)


@pytest.mark.security
@posix_only
def test_fabric_token_file_uses_the_strict_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "fabric.token"
    token.write_bytes(b"A" * 43 + b"\n")
    token.chmod(0o600)
    assert len(read_token_file(token, require_private=True).get_secret_value()) == 43
    fifo = tmp_path / "fifo.token"
    os.mkfifo(fifo, 0o600)
    for private in (True, False):  # the CLI copy may not be a FIFO either
        with pytest.raises(FabricTokenError, match="not a regular file"):
            read_token_file(fifo, require_private=private)
    link = tmp_path / "link.token"
    link.symlink_to(token)
    with pytest.raises(FabricTokenError, match="symlink"):
        read_token_file(link, require_private=True)
    assert read_token_file(link, require_private=False)  # a user's copy: followed
    with pytest.raises(FabricTokenError, match="not found"):
        read_token_file(tmp_path / "absent", require_private=True)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    with pytest.raises(FabricTokenError, match="owned by another user"):
        read_token_file(token, require_private=True)
    assert read_token_file(token, require_private=False)  # the CLI does not care


# ------------------------------------------------- (b) malformed extensions


def _hosts() -> list[str]:
    return ["127.0.0.1", "ctl.example"]


@pytest.mark.parametrize("damage", ["duplicate_san", "x400_san"])
def test_server_cert_with_unparseable_extensions_is_reissued_malformed(
    tmp_path: Path, damage: str
) -> None:
    """Reproduced first (D149b): before the fix ``ensure_server_cert`` raised
    ``DuplicateExtension`` / ``UnsupportedGeneralNameType`` out of the start-up."""
    ca_dir, tls_dir = tmp_path / "ca", tmp_path / "tls"
    fabric = create_fabric(ca_dir, now=NOW)
    first = ensure_server_cert(tls_dir, fabric, hosts=_hosts(), now=NOW)
    bad = (
        duplicate_extension(first.cert, fabric.authority.key)
        if damage == "duplicate_san"
        else unsupported_general_name(first.cert, fabric.authority.key, "ctl.example")
    )
    pki.verify_issued_by(bad, fabric.cert)  # the signature is fine
    (tls_dir / "server.crt").write_bytes(as_pem(bad))
    again = ensure_server_cert(tls_dir, fabric, hosts=_hosts(), now=NOW)
    assert again.action is ServerCertAction.REISSUED
    assert again.reason == "malformed"
    assert pki.cert_sans(again.cert) == [pki.SERVER_NAME, *_hosts()]


def _ca_with_san(ca: pki.CertificateAuthority) -> x509.Certificate:
    """The same CA certificate plus a DNS SAN (so it can be re-tagged x400)."""
    builder = (
        x509.CertificateBuilder()
        .subject_name(ca.cert.subject)
        .issuer_name(ca.cert.issuer)
        .public_key(ca.key.public_key())
        .serial_number(ca.cert.serial_number)
        .not_valid_before(ca.cert.not_valid_before_utc)
        .not_valid_after(ca.cert.not_valid_after_utc)
    )
    for ext in ca.cert.extensions:
        builder = builder.add_extension(ext.value, critical=ext.critical)
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName("ca.example")]), critical=False
    )
    return builder.sign(ca.key, hashes.SHA256())


@pytest.mark.parametrize("damage", ["duplicate_bc", "x400_san"])
def test_ca_with_unparseable_extensions_is_a_typed_config_error(
    tmp_path: Path, damage: str
) -> None:
    ca_dir = tmp_path / "controller" / "ca"
    fabric = create_fabric(ca_dir, now=NOW)
    authority = fabric.authority
    bad = (
        duplicate_extension(authority.cert, authority.key, target="bc")
        if damage == "duplicate_bc"
        else unsupported_general_name(
            _ca_with_san(authority), authority.key, "ca.example"
        )
    )
    (ca_dir / "ca.crt").write_bytes(as_pem(bad))
    with pytest.raises(FabricCaError, match="malformed") as info:
        load_fabric(ca_dir, now=NOW)
    assert info.value.failure_type is FailureType.CONFIG_ERROR


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(f'data_dir = "{tmp_path.as_posix()}/data"\n', encoding="utf-8")
    return config


def test_fabric_show_with_malformed_server_cert_or_ca_is_typed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runner = CliRunner()
    created = runner.invoke(
        controller_app, ["fabric", "create", "--config", str(config)]
    )
    assert created.exit_code == 0, created.output
    data = tmp_path / "data" / "controller"
    fabric = load_fabric(data / "ca")
    server = pki.read_cert(data / "tls" / "server.crt")
    (data / "tls" / "server.crt").write_bytes(
        as_pem(duplicate_extension(server, fabric.authority.key))
    )
    shown = runner.invoke(
        controller_app, ["fabric", "show", "--json", "--config", str(config)]
    )
    assert shown.exit_code == 0, shown.output
    assert "malformed" in shown.output and "Traceback" not in shown.output
    (data / "ca" / "ca.crt").write_bytes(
        as_pem(duplicate_extension(fabric.cert, fabric.authority.key, target="bc"))
    )
    broken = runner.invoke(controller_app, ["fabric", "show", "--config", str(config)])
    assert broken.exit_code == ExitCode.CONFIG_ERROR, broken.output
    assert "malformed" in broken.output and broken.exception is not None
    assert isinstance(broken.exception, SystemExit)  # typer.Exit, not a crash


# ------------------------------------------------------- (c) interrupted replace


def _crash_mid_replace(ca_dir: Path) -> str:
    """What a kill between "old aside" and "new in" leaves: ``.ca.old-*`` (the
    real old CA), a complete ``.ca.new-*`` staging folder, and no ``ca/``."""
    old = load_fabric(ca_dir, now=NOW)
    ca_dir.rename(ca_dir.with_name(".ca.old-1a2b3c4d"))
    staging = ca_dir.with_name(".ca.new-5e6f7a8b")
    staging.mkdir(mode=0o700)
    newer = pki.create_ca(new_fabric_id(), now=NOW)
    (staging / "ca.key").write_bytes(pki.key_to_pem(newer.key))
    (staging / "ca.crt").write_bytes(pki.cert_to_pem(newer.cert))
    return old.fabric_id


def test_create_after_a_crash_mid_replace_restores_the_old_ca(tmp_path: Path) -> None:
    """Reproduced first (D149c): before the fix the sweep deleted ``.ca.old-*``
    and this ``create`` (no ``--force``) silently made a brand-new fabric."""
    ca_dir = tmp_path / "controller" / "ca"
    create_fabric(ca_dir, now=NOW)
    old_id = _crash_mid_replace(ca_dir)
    with pytest.raises(FabricExistsError):
        create_fabric(ca_dir, now=NOW)
    assert load_fabric(ca_dir, now=NOW).fabric_id == old_id
    assert (
        sorted(p.name for p in ca_dir.parent.iterdir() if p.name.startswith(".")) == []
    )


def test_controller_start_hook_restores_the_old_ca(tmp_path: Path) -> None:
    ca_dir = tmp_path / "controller" / "ca"
    create_fabric(ca_dir, now=NOW)
    old_id = _crash_mid_replace(ca_dir)
    recover_interrupted(ca_dir)
    assert load_fabric(ca_dir, now=NOW).fabric_id == old_id


def test_two_old_copies_and_no_ca_is_a_typed_error_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    ca_dir = tmp_path / "controller" / "ca"
    create_fabric(ca_dir, now=NOW)
    _crash_mid_replace(ca_dir)
    second = ca_dir.with_name(".ca.old-99999999")
    second.mkdir(mode=0o700)
    with pytest.raises(FabricCaError, match="2 earlier copies"):
        recover_interrupted(ca_dir)
    with pytest.raises(FabricCaError, match="2 earlier copies"):
        create_fabric(ca_dir, now=NOW, force=True)
    names = sorted(p.name for p in ca_dir.parent.iterdir())
    assert ".ca.old-1a2b3c4d" in names and ".ca.old-99999999" in names


def test_debris_next_to_a_present_ca_is_still_removed(tmp_path: Path) -> None:
    ca_dir = tmp_path / "controller" / "ca"
    fabric = create_fabric(ca_dir, now=NOW)
    for name in (".ca.old-00000000", ".ca.new-11111111"):
        (ca_dir.parent / name).mkdir()
    recover_interrupted(ca_dir)
    assert load_fabric(ca_dir, now=NOW).fabric_id == fabric.fabric_id
    assert not any(p.name.startswith(".ca.") for p in ca_dir.parent.iterdir())


def test_recover_without_a_controller_folder_creates_nothing(tmp_path: Path) -> None:
    recover_interrupted(tmp_path / "nothing" / "ca")
    assert not (tmp_path / "nothing").exists()


# ------------------------------------------------------------ (d) fingerprints


def test_require_full_refuses_the_short_form(tmp_path: Path) -> None:
    ca = pki.create_ca(new_fabric_id(), now=NOW)
    full, short = pki.cert_fingerprint(ca.cert), pki.short_fingerprint(ca.cert)
    assert pki.fingerprint_matches(ca.cert, full, require_full=True)
    assert not pki.fingerprint_matches(ca.cert, short, require_full=True)
    assert pki.fingerprint_matches(ca.cert, short, require_full=False)
    with pytest.raises(TypeError):
        pki.fingerprint_matches(ca.cert, full)  # type: ignore[call-arg]


# ------------------------------------------------------------ (f) presence


def test_tls_presence_is_not_the_fabric_question(tmp_path: Path) -> None:
    assert fabric_ca.fabric_present is not fabric_ca._present
    tls = tmp_path / "tls"
    assert not fabric_ca._present(tls)
    tls.symlink_to(tmp_path / "dangling") if os.name == "posix" else tls.mkdir()
    assert fabric_ca._present(tls)
