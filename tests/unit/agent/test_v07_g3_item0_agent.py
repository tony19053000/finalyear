"""V0.7 G3 item 0 (D153) on the agent side.

* (2) ``join --force``: the new pair is staged in full before anything is
  replaced, so a failure while writing keeps the old enrolment byte-identical
  and leaves no temporary file;
* (3) ``icacls`` / ``whoami`` run with a timeout; a hang is a typed error;
* (5) the canary for the private 3.12 ``get_unverified_chain`` the pin read
  relies on: a real TLS handshake returns the chain, CA included, as DER.
"""

from __future__ import annotations

import ssl
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509

from meshforge.agent import enrollment, identity
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common import pki, private_files
from meshforge.common.errors import IdentityError
from meshforge.common.ids import new_fabric_id

pytestmark = pytest.mark.unit


def _cert(ca: pki.CertificateAuthority, ident: NodeIdentity) -> x509.Certificate:
    csr = pki.build_csr(
        ident.private_key, fabric_id=ca.fabric_id, node_id=ident.node_id
    )
    return pki.issue_node_cert(ca, csr, node_id=ident.node_id, fabric_id=ca.fabric_id)


@pytest.fixture
def ident(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "identity")


# ------------------------------------------------------------------ item 2


@pytest.mark.security
@pytest.mark.parametrize("failing", [1, 2])
def test_failed_force_rejoin_keeps_the_old_enrolment(
    ident: NodeIdentity, monkeypatch: pytest.MonkeyPatch, failing: int
) -> None:
    old_ca = pki.create_ca(new_fabric_id())
    enrollment.store(ident, _cert(old_ca, ident), old_ca.cert, force=False)
    before = {p.name: p.read_bytes() for p in ident.path.iterdir() if p.is_file()}

    real = enrollment._stage
    calls: list[Path] = []

    def flaky(target: Path, data: bytes, staged: list[Path]) -> Path:
        calls.append(target)
        if len(calls) == failing:
            if failing == 2:  # the first temp file exists; it must be cleaned
                assert len(staged) == 1 and staged[0].exists()
            raise OSError(28, "No space left on device")
        return real(target, data, staged)

    monkeypatch.setattr(enrollment, "_stage", flaky)
    new_ca = pki.create_ca(new_fabric_id())
    with pytest.raises(JoinError) as info:
        enrollment.store(ident, _cert(new_ca, ident), new_ca.cert, force=True)
    assert info.value.reason is JoinReason.STORAGE
    after = {p.name: p.read_bytes() for p in ident.path.iterdir() if p.is_file()}
    assert after == before, "the old enrolment must be untouched"
    assert not list(ident.path.glob(".*.tmp"))
    loaded = enrollment.load(ident)
    assert loaded is not None
    assert loaded.ca_fingerprint == pki.cert_fingerprint(old_ca.cert)


def test_force_rejoin_swaps_in_the_new_pair(ident: NodeIdentity) -> None:
    old_ca, new_ca = pki.create_ca(new_fabric_id()), pki.create_ca(new_fabric_id())
    enrollment.store(ident, _cert(old_ca, ident), old_ca.cert, force=False)
    enrollment.store(ident, _cert(new_ca, ident), new_ca.cert, force=True)
    loaded = enrollment.load(ident)
    assert loaded is not None
    assert loaded.fabric_id == new_ca.fabric_id
    assert not list(ident.path.glob(".*.tmp"))


@pytest.mark.security
def test_mutation_unstaged_write_would_lose_the_old_pair(
    ident: NodeIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation check of the property above: the old store deleted node.crt
    first - with that order a write failure leaves 'not enrolled'. Our store
    must never get there: node.crt is removed only after both temps exist."""
    ca = pki.create_ca(new_fabric_id())
    enrollment.store(ident, _cert(ca, ident), ca.cert, force=False)
    removed: list[str] = []
    real_unlink = Path.unlink

    def watch(self: Path, missing_ok: bool = False) -> None:
        if self.name == "node.crt":
            removed.append("node.crt")
            assert len(list(ident.path.glob(".*.tmp"))) == 2, "unlinked too early"
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", watch)
    enrollment.store(ident, _cert(ca, ident), ca.cert, force=True)
    assert removed == ["node.crt"]


# ------------------------------------------------------------------ item 3


def _hang(*args: Any, **kwargs: Any) -> Any:
    assert kwargs.get("timeout") == private_files.WINDOWS_TOOL_TIMEOUT_S
    raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])


@pytest.mark.security
def test_private_files_icacls_and_whoami_hangs_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(private_files.subprocess, "run", _hang)
    with pytest.raises(private_files.PrivateFileError, match="whoami did not finish"):
        private_files._current_user_sid()
    monkeypatch.setattr(private_files, "_current_user_sid", lambda: "S-1-5-21-1")
    with pytest.raises(private_files.PrivateFileError, match="icacls did not finish"):
        private_files._protect_windows(tmp_path / "f")


@pytest.mark.security
def test_identity_icacls_and_whoami_hangs_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity.subprocess, "run", _hang)
    with pytest.raises(IdentityError, match="whoami did not finish"):
        identity._current_user_sid()
    monkeypatch.setattr(identity, "_current_user_sid", lambda: "S-1-5-21-1")
    with pytest.raises(IdentityError, match="icacls did not finish"):
        identity._protect_windows(tmp_path / "f")


# ------------------------------------------------------------------ item 5


def test_canary_unverified_chain_reader_returns_the_presented_ca(
    tmp_path: Path,
) -> None:
    """If a Python upgrade removes or changes the private 3.12 API, this fails
    here first (not in a lab join)."""
    ca = pki.create_ca(new_fabric_id())
    server = pki.issue_server_cert(ca, hosts=["127.0.0.1"])
    (tmp_path / "chain.pem").write_bytes(
        pki.cert_to_pem(server.cert) + pki.cert_to_pem(ca.cert)
    )
    (tmp_path / "key.pem").write_bytes(pki.key_to_pem(server.key))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "chain.pem", tmp_path / "key.pem")
    import socket

    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        presented = enrollment.fetch_presented_ca("127.0.0.1", port, timeout_s=5)
    finally:
        thread.join(5)
        listener.close()
    fingerprints = [pki.cert_fingerprint(c) for c in presented]
    assert fingerprints == [
        pki.cert_fingerprint(server.cert),
        pki.cert_fingerprint(ca.cert),
    ]
    assert enrollment.pin_ca(presented, pki.short_fingerprint(ca.cert)) is not None
