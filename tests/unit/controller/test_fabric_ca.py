"""``meshforge.controller.fabric_ca`` (V0.7 G1, D134): CA + server cert on disk.

Create/load strictness (fails closed), private files, atomic pair installs under a
real killed process, leftover sweep, lock contention, the server cert's
issue/keep/renew/re-issue decisions, ``[controller.tls]`` settings and the
start-up hook. THREAT_MODEL rows LF-I1, LF-I3, LF-T3, PK-D1, PK-S2 point here.
"""

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
from pydantic import ValidationError

from meshforge.common import pki
from meshforge.common.errors import ExitCode, FailureType
from meshforge.common.filelock import locked
from meshforge.controller import fabric_ca
from meshforge.controller.fabric_ca import (
    FabricCaError,
    FabricExistsError,
    FabricMissingError,
    ServerCertAction,
    create_fabric,
    ensure_server_cert,
    load_fabric,
    server_cert_hosts,
)
from meshforge.controller.settings import ControllerSettings, TlsSettings

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
WILDCARD_V4 = "0.0.0.0"  # noqa: S104 - a bind string being filtered, never bound
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX mode bits / symlinks"
)


@pytest.fixture
def ca_dir(tmp_path: Path) -> Path:
    return tmp_path / "controller" / "ca"


@pytest.fixture
def tls_dir(tmp_path: Path) -> Path:
    return tmp_path / "controller" / "tls"


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def _hidden_leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir() if p.name.startswith("."))


# ------------------------------------------------------------------------- create


def test_create_then_load_round_trip(ca_dir: Path) -> None:
    created = create_fabric(ca_dir, now=NOW)
    loaded = load_fabric(ca_dir, now=NOW)
    assert loaded.fabric_id == created.fabric_id
    assert loaded.fingerprint == created.fingerprint
    assert loaded.short_fingerprint == created.fingerprint[:32]
    assert sorted(os.listdir(ca_dir)) == ["ca.crt", "ca.key"]
    assert _hidden_leftovers(ca_dir.parent) == []
    assert "authority" not in repr(loaded)


@posix_only
def test_files_are_private(ca_dir: Path, tls_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["127.0.0.1"], now=NOW)
    for directory in (ca_dir, tls_dir, ca_dir.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    for path in [*ca_dir.iterdir(), *tls_dir.iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


@posix_only
def test_server_key_is_private(ca_dir: Path, tls_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW)
    assert stat.S_IMODE((tls_dir / "server.key").stat().st_mode) == 0o600


def test_create_refuses_to_overwrite(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    before = _snapshot(ca_dir)
    with pytest.raises(FabricExistsError) as exc:
        create_fabric(ca_dir, now=NOW)
    assert exc.value.exit_code == ExitCode.CONFLICT
    assert exc.value.failure_type is FailureType.CONFLICT
    assert "--force" in str(exc.value)
    assert _snapshot(ca_dir) == before


def test_create_refuses_over_a_partial_folder_without_force(ca_dir: Path) -> None:
    ca_dir.mkdir(parents=True)
    with pytest.raises(FabricExistsError):
        create_fabric(ca_dir, now=NOW)
    fabric = create_fabric(ca_dir, now=NOW, force=True)
    assert load_fabric(ca_dir, now=NOW).fabric_id == fabric.fabric_id


def test_force_replaces_atomically(ca_dir: Path) -> None:
    old = create_fabric(ca_dir, now=NOW)
    new = create_fabric(ca_dir, now=NOW, force=True)
    assert new.fabric_id != old.fabric_id
    assert load_fabric(ca_dir, now=NOW).fingerprint == new.fingerprint
    assert _hidden_leftovers(ca_dir.parent) == []


def test_two_racing_creates_end_with_one_ca(ca_dir: Path) -> None:
    results: list[object] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(create_fabric(ca_dir, now=NOW).fabric_id)
        except FabricExistsError as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if isinstance(r, str)]
    assert len(winners) == 1
    assert sum(isinstance(r, FabricExistsError) for r in results) == 3
    assert load_fabric(ca_dir, now=NOW).fabric_id == winners[0]


def test_held_lock_is_a_typed_error(
    ca_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fabric_ca, "_LOCK_TIMEOUT_S", 0.05)
    ca_dir.parent.mkdir(parents=True)
    with locked(ca_dir.parent / "ca.lock"):
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys, pathlib
                    from meshforge.controller import fabric_ca
                    fabric_ca._LOCK_TIMEOUT_S = 0.05
                    try:
                        fabric_ca.create_fabric(pathlib.Path({str(ca_dir)!r}))
                    except fabric_ca.FabricCaError as exc:
                        print("held" if "held by another process" in str(exc) else exc)
                        sys.exit(0)
                    sys.exit(3)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        out, _ = holder.communicate(timeout=60)
    assert holder.returncode == 0, out
    assert out.strip() == "held"
    assert not ca_dir.exists()


def test_leftovers_are_swept(ca_dir: Path) -> None:
    # Next to a present ca/ both kinds are debris. (A lone .ca.old-* with NO ca/
    # is the only copy of the old CA and is restored instead - V0.7 G2 D149c,
    # tests/unit/controller/test_v07_g2_item0.py.)
    parent = ca_dir.parent
    create_fabric(ca_dir, now=NOW)
    for name in (".ca.new-deadbeef", ".ca.old-cafebabe"):
        (parent / name).mkdir()
        (parent / name / "ca.key").write_bytes(b"stale")
    create_fabric(ca_dir, now=NOW, force=True)
    assert _hidden_leftovers(parent) == []


_KILL_AT_RENAME = """
import os, pathlib, sys
from meshforge.controller import fabric_ca
real_rename = os.rename
def rename(src, dst):
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    if src.name.startswith(".ca.new-") and {phase!r} == "before_new_in":
        os._exit(9)
    real_rename(src, dst)
    if dst.name.startswith(".ca.old-") and {phase!r} == "after_old_aside":
        os._exit(9)
os.rename = rename
fabric_ca.create_fabric(pathlib.Path({ca_dir!r}), force=True)
sys.exit(0)
"""


@pytest.mark.parametrize("phase", ["before_new_in", "after_old_aside"])
def test_crash_between_files_never_leaves_a_mismatched_pair(
    ca_dir: Path, phase: str
) -> None:
    """A real process killed (``os._exit``) mid-replace: old pair, or no folder."""
    old = create_fabric(ca_dir, now=NOW)
    old_files = _snapshot(ca_dir)
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_AT_RENAME.format(phase=phase, ca_dir=str(ca_dir))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 9, proc.stderr
    if ca_dir.exists():
        # Killed before the old folder moved: the old CA is intact and loads.
        assert _snapshot(ca_dir) == old_files
        assert load_fabric(ca_dir, now=NOW).fabric_id == old.fabric_id
    else:
        # Killed with the old folder aside: no CA (never a torn one).
        with pytest.raises(FabricMissingError):
            load_fabric(ca_dir, now=NOW)
    # The next create sweeps whatever the crash left and works.
    create_fabric(ca_dir, now=NOW, force=True)
    load_fabric(ca_dir, now=NOW)
    assert _hidden_leftovers(ca_dir.parent) == []


def test_write_failure_leaves_the_old_ca(
    ca_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = create_fabric(ca_dir, now=NOW)
    real_rename = os.rename

    def failing_rename(src: object, dst: object) -> None:
        if Path(str(src)).name.startswith(".ca.new-"):
            raise OSError(28, "No space left on device")
        real_rename(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(FabricCaError, match="No space left"):
        create_fabric(ca_dir, now=NOW, force=True)
    monkeypatch.undo()
    assert load_fabric(ca_dir, now=NOW).fabric_id == old.fabric_id  # rolled back
    assert _hidden_leftovers(ca_dir.parent) == []


# --------------------------------------------------------------------------- load


def test_missing_ca_is_its_own_error(ca_dir: Path) -> None:
    with pytest.raises(FabricMissingError, match="fabric create"):
        load_fabric(ca_dir)


@pytest.mark.parametrize("gone", ["ca.key", "ca.crt"])
def test_incomplete_ca_is_refused(ca_dir: Path, gone: str) -> None:
    create_fabric(ca_dir, now=NOW)
    (ca_dir / gone).unlink()
    with pytest.raises(FabricCaError, match="incomplete") as exc:
        load_fabric(ca_dir, now=NOW)
    assert exc.value.exit_code == ExitCode.CONFIG_ERROR


@posix_only
def test_loose_ca_key_is_refused(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    os.chmod(ca_dir / "ca.key", 0o644)
    with pytest.raises(FabricCaError, match="chmod 600") as exc:
        load_fabric(ca_dir, now=NOW)
    assert "PRIVATE KEY" not in str(exc.value)


@posix_only
def test_loose_ca_folder_is_refused(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    os.chmod(ca_dir, 0o755)  # noqa: S103 - the loose mode under test
    with pytest.raises(FabricCaError, match="chmod 700"):
        load_fabric(ca_dir, now=NOW)


@posix_only
def test_symlinked_ca_folder_is_refused(tmp_path: Path, ca_dir: Path) -> None:
    real = tmp_path / "elsewhere"
    create_fabric(real, now=NOW)
    ca_dir.parent.mkdir(parents=True)
    ca_dir.symlink_to(real)
    with pytest.raises(FabricCaError, match="not a folder"):
        load_fabric(ca_dir, now=NOW)


def test_mismatched_pair_is_refused(tmp_path: Path, ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW)
    other = create_fabric(tmp_path / "other", now=NOW)
    (ca_dir / "ca.crt").write_bytes(pki.cert_to_pem(other.cert))
    with pytest.raises(FabricCaError, match="key_mismatch"):
        load_fabric(ca_dir, now=NOW)


def test_a_leaf_cert_as_ca_is_refused(ca_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    leaf = pki.issue_server_cert(fabric.authority, hosts=[], now=NOW)
    pki.write_private_key(ca_dir / "ca.key", leaf.key)
    pki.write_cert(ca_dir / "ca.crt", leaf.cert)
    with pytest.raises(FabricCaError, match="not_a_ca"):
        load_fabric(ca_dir, now=NOW)


def test_expired_ca_is_refused(ca_dir: Path) -> None:
    create_fabric(ca_dir, now=NOW, lifetime=timedelta(days=1))
    load_fabric(ca_dir, now=NOW + timedelta(hours=23))
    with pytest.raises(FabricCaError, match="expired"):
        load_fabric(ca_dir, now=NOW + timedelta(days=1))


@pytest.mark.parametrize(
    ("name", "data", "match"),
    [
        ("ca.crt", b"garbage", "not a PEM certificate"),
        ("ca.crt", b"x" * (pki.MAX_CERT_BYTES + 1), "larger than"),
        ("ca.key", b"garbage", "not an unencrypted PEM"),
        ("ca.key", b"x" * (pki.MAX_KEY_BYTES + 1), "larger than"),
    ],
)
def test_garbled_files_are_refused(
    ca_dir: Path, name: str, data: bytes, match: str
) -> None:
    create_fabric(ca_dir, now=NOW)
    pki_path = ca_dir / name
    pki_path.write_bytes(data)
    with pytest.raises(FabricCaError, match=match):
        load_fabric(ca_dir, now=NOW)


# -------------------------------------------------------------------- server cert


@pytest.fixture
def fabric(ca_dir: Path) -> fabric_ca.FabricCa:
    return create_fabric(ca_dir, now=NOW)


def test_server_cert_sans_follow_hosts(
    fabric: fabric_ca.FabricCa, tls_dir: Path
) -> None:
    first = ensure_server_cert(tls_dir, fabric, hosts=["192.168.1.10"], now=NOW)
    assert first.action is ServerCertAction.ISSUED
    assert pki.cert_sans(first.cert) == [pki.SERVER_NAME, "192.168.1.10"]
    pki.verify_issued_by(first.cert, fabric.cert)
    again = ensure_server_cert(tls_dir, fabric, hosts=["192.168.1.10"], now=NOW)
    assert again.action is ServerCertAction.KEPT
    assert again.cert == first.cert
    assert "issued" not in repr(again)  # the key never reaches repr


def test_server_cert_sans_changed_reissues(
    fabric: fabric_ca.FabricCa, tls_dir: Path
) -> None:
    first = ensure_server_cert(tls_dir, fabric, hosts=["192.168.1.10"], now=NOW)
    moved = ensure_server_cert(tls_dir, fabric, hosts=["192.168.1.11"], now=NOW)
    assert (moved.action, moved.reason) == (ServerCertAction.REISSUED, "sans_changed")
    assert not pki.public_keys_equal(first.key.public_key(), moved.key.public_key())
    assert pki.cert_sans(moved.cert) == [pki.SERVER_NAME, "192.168.1.11"]


def test_server_cert_renewed_at_two_thirds(
    fabric: fabric_ca.FabricCa, tls_dir: Path
) -> None:
    life = timedelta(days=30)
    first = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW, lifetime=life)
    start, end = first.cert.not_valid_before_utc, first.cert.not_valid_after_utc
    due = start + (end - start) * 2 / 3
    kept = ensure_server_cert(
        tls_dir, fabric, hosts=[], now=due - timedelta(seconds=1), lifetime=life
    )
    assert kept.action is ServerCertAction.KEPT
    renewed = ensure_server_cert(tls_dir, fabric, hosts=[], now=due, lifetime=life)
    assert (renewed.action, renewed.reason) == (ServerCertAction.RENEWED, "renewal_due")
    assert renewed.cert.not_valid_after_utc == due + life


def test_server_cert_expired_or_future_reissues(ca_dir: Path, tls_dir: Path) -> None:
    fabric = create_fabric(ca_dir, now=NOW - timedelta(days=10))
    first = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW)
    # The clock went back two days: the CA is still valid, the server cert not yet.
    early = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW - timedelta(days=2))
    assert (early.action, early.reason) == (ServerCertAction.REISSUED, "not_yet_valid")
    assert early.cert != first.cert
    late = early.cert.not_valid_after_utc + timedelta(seconds=1)
    expired = ensure_server_cert(tls_dir, fabric, hosts=[], now=late)
    assert (expired.action, expired.reason) == (ServerCertAction.REISSUED, "expired")


def test_server_cert_refused_while_the_ca_is_not_valid(
    fabric: fabric_ca.FabricCa, tls_dir: Path
) -> None:
    with pytest.raises(FabricCaError, match="not valid yet"):
        ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW - timedelta(days=1))
    assert not tls_dir.exists()


def test_server_cert_from_old_ca_reissued_after_force(
    ca_dir: Path, tls_dir: Path
) -> None:
    old = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, old, hosts=[], now=NOW)
    new = create_fabric(ca_dir, now=NOW, force=True)
    server = ensure_server_cert(tls_dir, new, hosts=[], now=NOW)
    assert (server.action, server.reason) == (ServerCertAction.REISSUED, "other_ca")
    pki.verify_issued_by(server.cert, new.cert)


@pytest.mark.parametrize(
    "damage",
    ["garbage_cert", "missing_key", "mismatched_key", "loose_key", "loose_dir"],
)
def test_unusable_server_cert_is_reissued(
    fabric: fabric_ca.FabricCa, tls_dir: Path, damage: str
) -> None:
    if damage.startswith("loose") and sys.platform.startswith("win"):
        pytest.skip("POSIX mode bits")
    first = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW)
    if damage == "garbage_cert":
        (tls_dir / "server.crt").write_bytes(b"garbage")
    elif damage == "missing_key":
        (tls_dir / "server.key").unlink()
    elif damage == "mismatched_key":
        pki.write_private_key(tls_dir / "server.key", pki.generate_key())
    elif damage == "loose_key":
        os.chmod(tls_dir / "server.key", 0o644)
    else:
        os.chmod(tls_dir, 0o755)  # noqa: S103 - the loose mode under test
    server = ensure_server_cert(tls_dir, fabric, hosts=[], now=NOW)
    assert (server.action, server.reason) == (ServerCertAction.REISSUED, "unreadable")
    assert not pki.public_keys_equal(first.key.public_key(), server.key.public_key())
    reloaded = fabric_ca.load_server_cert(tls_dir)
    assert reloaded.cert == server.cert
    if os.name == "posix":
        assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((tls_dir / "server.key").stat().st_mode) == 0o600


def test_server_cert_bad_host_is_a_config_error(
    fabric: fabric_ca.FabricCa, tls_dir: Path
) -> None:
    with pytest.raises(FabricCaError, match="extra_sans") as exc:
        ensure_server_cert(tls_dir, fabric, hosts=["*.lan"], now=NOW)
    assert exc.value.exit_code == ExitCode.CONFIG_ERROR
    assert not tls_dir.exists()


def test_server_cert_hosts_from_binds() -> None:
    assert server_cert_hosts(
        [f"{WILDCARD_V4}:47100", "192.168.1.10:47110", "[::]:1", "[fe80::1]:2"],
        advertise_url="https://controller.lan:47110",
        extra=("192.168.1.10", "10.0.0.5"),
    ) == ["192.168.1.10", "fe80::1", "controller.lan", "10.0.0.5"]
    assert server_cert_hosts(
        ["127.0.0.1:0"], advertise_url="http://[fd00::5]:47110"
    ) == ["127.0.0.1", "fd00::5"]


# ----------------------------------------------------------------------- settings


def test_tls_settings_bounds() -> None:
    assert TlsSettings().server_cert_days == 365
    TlsSettings(server_cert_days=825)
    for bad in (0, 826):
        with pytest.raises(ValidationError):
            TlsSettings(server_cert_days=bad)
    hosts = tuple(f"10.0.0.{i}" for i in range(1, 33))
    assert TlsSettings(extra_sans=hosts).extra_sans == hosts
    with pytest.raises(ValidationError):
        TlsSettings(extra_sans=(*hosts, "10.0.1.1"))
    for bad_host in (WILDCARD_V4, "*.lan", "bad host"):
        with pytest.raises(ValidationError):
            TlsSettings(extra_sans=(bad_host,))


def test_settings_paths_and_hosts(tmp_path: Path) -> None:
    settings = ControllerSettings(
        data_dir=tmp_path,
        controller={
            "grpc_bind": "192.168.1.10:47100",
            "http_bind": "192.168.1.10:47110",
            "tls": {"extra_sans": ["controller.lan"]},
        },
    )
    assert settings.ca_dir == tmp_path / "controller" / "ca"
    assert settings.tls_dir == tmp_path / "controller" / "tls"
    assert settings.server_cert_hosts == ["192.168.1.10", "controller.lan"]


# ---------------------------------------------------------------- start-up hook


def test_prepare_fabric_at_start(tmp_path: Path) -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.server import _prepare_fabric

    settings = ControllerSettings(data_dir=tmp_path)
    assert _prepare_fabric(settings) == (None, None)  # no CA yet: allowed in G1
    assert not settings.tls_dir.exists()
    created = create_fabric(settings.ca_dir)
    fabric, server = _prepare_fabric(settings)
    assert fabric is not None and server is not None
    assert fabric.fabric_id == created.fabric_id
    assert server.action is ServerCertAction.ISSUED
    assert pki.cert_sans(server.cert) == [pki.SERVER_NAME, "127.0.0.1"]
    _, again = _prepare_fabric(settings)
    assert again is not None and again.action is ServerCertAction.KEPT
    (settings.ca_dir / "ca.crt").unlink()
    with pytest.raises(FabricCaError, match="incomplete"):
        _prepare_fabric(settings)  # a broken CA stops the start (fails closed)


def test_logs_carry_fingerprints_never_key_material(
    ca_dir: Path, tls_dir: Path
) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        fabric = create_fabric(ca_dir, now=NOW)
        ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    events = {entry["event"] for entry in logs}
    assert {"controller.fabric.created", "controller.tls.server_cert_issued"} <= events
    text = repr(logs)
    for secret in (ca_dir / "ca.key", tls_dir / "server.key"):
        body = secret.read_text().splitlines()[1]  # a base64 line of the key
        assert body not in text
    assert "PRIVATE" not in text
    assert fabric.short_fingerprint in text


# ------------------------------------------------- fix round 1 (T-1, T-2, T-3, O-2)


def test_scoped_ipv6_is_refused_in_extra_sans_but_stripped_from_binds(
    tmp_path: Path, ca_dir: Path, tls_dir: Path
) -> None:
    with pytest.raises(ValidationError, match="zone id"):
        TlsSettings(extra_sans=("fe80::1%eth0",))
    settings = ControllerSettings(
        data_dir=tmp_path,
        controller={
            "grpc_bind": "[fe80::1%eth0]:47100",
            "http_bind": "[fe80::1%eth0]:47110",
        },
    )
    fabric = create_fabric(ca_dir, now=NOW)
    first = ensure_server_cert(
        tls_dir, fabric, hosts=settings.server_cert_hosts, now=NOW
    )
    assert pki.cert_sans(first.cert) == [pki.SERVER_NAME, "fe80::1"]
    again = ensure_server_cert(
        tls_dir, fabric, hosts=settings.server_cert_hosts, now=NOW
    )
    assert again.action is ServerCertAction.KEPT


@posix_only
@pytest.mark.parametrize("victim", ["server.key", "server.crt"])
def test_symlinked_tls_file_is_treated_as_unreadable_and_reissued(
    tmp_path: Path, ca_dir: Path, tls_dir: Path, victim: str
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    real = tmp_path / f"real-{victim}"
    (tls_dir / victim).rename(real)
    (tls_dir / victim).symlink_to(real)  # same bytes, but through a link
    got = ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    assert got.action is ServerCertAction.REISSUED and got.reason == "unreadable"
    assert not (tls_dir / victim).is_symlink()


@posix_only
def test_read_private_key_refuses_a_symlink_and_a_fifo(tmp_path: Path) -> None:
    key = tmp_path / "k.key"
    key.write_bytes(pki.key_to_pem(pki.generate_key()))
    key.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(key)
    with pytest.raises(pki.PkiFileError, match="symlink"):
        pki.read_private_key(link)
    fifo = tmp_path / "fifo.key"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(pki.PkiFileError, match="not a regular file"):
        pki.read_private_key(fifo)


@posix_only
def test_key_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "k.key"
    key.write_bytes(pki.key_to_pem(pki.generate_key()))
    key.chmod(0o600)
    pki.read_private_key(key)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # "we" are someone else now
    with pytest.raises(pki.PkiFileError, match="owned by another user"):
        pki.read_private_key(key)


@posix_only
def test_ca_folder_owned_by_another_user_is_refused(
    ca_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_fabric(ca_dir, now=NOW)
    me = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)
    with pytest.raises(FabricCaError, match="owned by another user"):
        load_fabric(ca_dir)


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes"
)
def test_unwritable_parent_is_a_typed_error_for_the_server_cert_too(
    ca_dir: Path, tls_dir: Path
) -> None:
    fabric = create_fabric(ca_dir, now=NOW)
    parent = tls_dir.parent
    parent.chmod(0o500)
    try:
        with pytest.raises(FabricCaError) as exc:
            ensure_server_cert(tls_dir, fabric, hosts=["10.0.0.1"], now=NOW)
    finally:
        parent.chmod(0o700)
    assert exc.value.exit_code == ExitCode.CONFIG_ERROR
    assert str(parent) in exc.value.message


def test_errors_inside_the_lock_are_not_rewritten(ca_dir: Path) -> None:
    with pytest.raises(OSError, match="inner"), fabric_ca._lock(ca_dir):
        raise OSError("inner")
