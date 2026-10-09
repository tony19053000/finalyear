"""V0.5 G4 D98: the controller's peer-token signing key file (fabric.token rules)."""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import load_public_key, verify_peer_token
from meshforge.controller.peer_signing import (
    MAX_KEY_FILE_BYTES,
    PeerSigningKeyError,
    PeerTokenIssuer,
    default_key_path,
    load_or_create_signing_key,
    public_key_bytes,
)
from meshforge.controller.settings import ControllerSettings

pytestmark = pytest.mark.unit
posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")


def test_create_then_reload_same_key(tmp_path: Path) -> None:
    path = default_key_path(tmp_path)
    key, created = load_or_create_signing_key(path)
    assert created
    again, created_again = load_or_create_signing_key(path)
    assert not created_again
    assert public_key_bytes(again) == public_key_bytes(key)
    assert len(public_key_bytes(key)) == 32
    assert [p.name for p in path.parent.glob(".*.tmp")] == []


@posix_only
def test_created_private(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "peer_signing.key"
    load_or_create_signing_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_loosened_file_refused(tmp_path: Path) -> None:
    path = tmp_path / "peer_signing.key"
    load_or_create_signing_key(path)
    os.chmod(path, 0o644)
    with pytest.raises(PeerSigningKeyError, match="other users"):
        load_or_create_signing_key(path)


def test_garbage_and_wrong_key_type_refused(tmp_path: Path) -> None:
    path = tmp_path / "peer_signing.key"
    path.write_bytes(b"not a key\n")
    os.chmod(path, 0o600)
    with pytest.raises(PeerSigningKeyError, match="not an unencrypted PEM"):
        load_or_create_signing_key(path)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    with pytest.raises(PeerSigningKeyError, match="not an Ed25519"):
        load_or_create_signing_key(path)


def test_size_bound_max_and_max_plus_one(tmp_path: Path) -> None:
    path = tmp_path / "peer_signing.key"
    load_or_create_signing_key(path)
    pem = path.read_bytes()
    # A valid PEM padded with trailing whitespace up to exactly the limit loads.
    path.write_bytes(pem + b"\n" * (MAX_KEY_FILE_BYTES - len(pem)))
    load_or_create_signing_key(path)
    path.write_bytes(pem + b"\n" * (MAX_KEY_FILE_BYTES - len(pem) + 1))
    with pytest.raises(PeerSigningKeyError, match="larger than"):
        load_or_create_signing_key(path)


def test_concurrent_creation_yields_one_key(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "peer_signing.key"
    results: list[bytes] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(public_key_bytes(load_or_create_signing_key(path)[0]))
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(results)) == 1


def test_issuer_uses_injected_clock_and_ttl(tmp_path: Path) -> None:
    key, _ = load_or_create_signing_key(tmp_path / "k")
    now = [1_800_000_000.0]
    issuer = PeerTokenIssuer(key, ttl_s=45, clock=lambda: now[0])
    holder = new_node_id()
    token = issuer.issue(
        tree=sha256_bytes(b"t"),
        requester=new_node_id(),
        holder=holder,
        attempt_id=new_attempt_id(),
    )
    claims = verify_peer_token(
        load_public_key(issuer.public_key), token, holder=holder, now=now[0]
    )
    assert claims.exp == int(now[0]) + 45
    with pytest.raises(ValueError, match="ttl"):
        PeerTokenIssuer(key, ttl_s=61)  # V0.7 G3 (D147): <= 60 s


def test_settings_default_path_and_override(tmp_path: Path) -> None:
    settings = ControllerSettings(data_dir=tmp_path)
    assert settings.peer_key_path == tmp_path / "controller" / "peer_signing.key"
    other = tmp_path / "x.key"
    settings = ControllerSettings(
        data_dir=tmp_path, controller={"peer_key_file": str(other)}
    )
    assert settings.peer_key_path == other
    assert settings.controller.jobs.max_peer_sources == 4
    assert settings.controller.jobs.peer_token_ttl_s == 60  # D147
    assert settings.controller.trust_peer_endpoints is False
