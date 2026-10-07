"""V0.7 G2 tester: a hostile or fake controller at the code's address.

Real TLS, real gRPC, real sockets (no database needed). The success criterion is
brief 5b item 5 / D136: *the agent checks the CA pin BEFORE it sends the token*, so
a controller that is not the one the code names must learn nothing - not even the
bytes of an HTTP/2 preface. Every attacker below is a raw TLS server that records
every application byte it receives after the handshake.

* a whole second fabric (own CA, own server cert);
* the REAL CA certificate (public) next to the attacker's own server certificate;
* a rogue member: a genuine node certificate of the real fabric (EKU serverAuth is
  allowed for nodes) served as if it were the controller;
* a legitimate-CA controller that answers with a poisoned response or never answers.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import grpc
import pytest
from cryptography import x509

from meshforge.agent import enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common import join_code, pki
from meshforge.common.ids import new_fabric_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc

pytestmark = pytest.mark.component

LOOP = "127.0.0.1"


class SpyTlsServer:
    """TLS listener presenting ``chain`` with ``key``; records post-handshake bytes."""

    def __init__(self, tmp: Path, key: object, chain: list[x509.Certificate]) -> None:
        cert_file, key_file = tmp / "chain.pem", tmp / "key.pem"
        cert_file.write_bytes(b"".join(pki.cert_to_pem(c) for c in chain))
        key_file.write_bytes(pki.key_to_pem(key))  # type: ignore[arg-type]
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert_file), str(key_file))
        self._ctx.set_alpn_protocols(["h2"])
        self._sock = socket.socket()
        self._sock.bind((LOOP, 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.received = bytearray()
        self.handshakes = 0
        self.handshake_failures = 0
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                raw, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._one, args=(raw,), daemon=True).start()

    def _one(self, raw: socket.socket) -> None:
        raw.settimeout(3)
        try:
            tls = self._ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            self.handshake_failures += 1
            raw.close()
            return
        self.handshakes += 1
        tls.settimeout(1.5)
        try:
            while data := tls.recv(65536):
                self.received += data
        except (TimeoutError, ssl.SSLError, OSError):
            pass
        finally:
            tls.close()

    def close(self) -> None:
        self._stop = True
        self._sock.close()


def _code(port: int, ca: pki.CertificateAuthority, token: bytes | None = None) -> str:
    return join_code.encode(
        join_code.JoinCode(
            host=LOOP,
            port=port,
            token=token or join_code.new_token(),
            ca_pin=pki.short_fingerprint(ca.cert),
        )
    )


@pytest.fixture
def real_ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id())


@pytest.fixture
def identity(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "agent" / "identity")


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[list[SpyTlsServer]]:
    made: list[SpyTlsServer] = []
    yield made
    for server in made:
        server.close()


def _spy(
    servers: list[SpyTlsServer],
    tmp_path: Path,
    key: object,
    chain: list[x509.Certificate],
) -> SpyTlsServer:
    directory = tmp_path / f"spy{len(servers)}"
    directory.mkdir()
    server = SpyTlsServer(directory, key, chain)
    servers.append(server)
    return server


def _join_refused(identity: NodeIdentity, code: str) -> JoinError:
    with pytest.raises(JoinError) as info:
        enrollment.join(identity, code, timeout_s=6)
    return info.value


def _assert_learned_nothing(
    server: SpyTlsServer, identity: NodeIdentity, code: str
) -> None:
    time.sleep(0.3)
    token = join_code.decode(code).token
    assert bytes(server.received) == b"", (
        f"{len(server.received)} bytes reached the attacker"
    )
    assert token not in bytes(server.received)
    assert not enrollment.is_enrolled(identity.path)
    assert not (identity.path / "ca.crt").exists()


def test_a_whole_second_fabric_gets_no_bytes_after_the_handshake(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    servers: list[SpyTlsServer],
    tmp_path: Path,
) -> None:
    evil_ca = pki.create_ca(new_fabric_id())
    issued = pki.issue_server_cert(evil_ca, hosts=[LOOP])
    server = _spy(servers, tmp_path, issued.key, [issued.cert, evil_ca.cert])
    code = _code(server.port, real_ca)
    err = _join_refused(identity, code)
    assert err.reason is JoinReason.PIN_MISMATCH
    assert code not in err.message
    _assert_learned_nothing(server, identity, code)


def test_attacker_server_cert_next_to_the_real_public_ca_cert(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    servers: list[SpyTlsServer],
    tmp_path: Path,
) -> None:
    evil_ca = pki.create_ca(real_ca.fabric_id)
    issued = pki.issue_server_cert(evil_ca, hosts=[LOOP])
    # The pin matches the real CA that the attacker simply includes in the chain,
    # but the presented server certificate is not issued by it.
    server = _spy(servers, tmp_path, issued.key, [issued.cert, real_ca.cert])
    code = _code(server.port, real_ca)
    assert _join_refused(identity, code).reason is JoinReason.PIN_MISMATCH
    _assert_learned_nothing(server, identity, code)


def test_a_rogue_enrolled_node_serving_its_member_certificate_gets_no_token(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    servers: list[SpyTlsServer],
    tmp_path: Path,
) -> None:
    """A member holds a real fabric certificate (EKU client+server, issued by the
    pinned CA) and its key. Served as the 'controller', the pin check passes -
    the TLS name check must stop the token."""
    rogue = load_or_create(tmp_path / "rogue" / "identity")
    csr = pki.build_csr(
        rogue.private_key, fabric_id=real_ca.fabric_id, node_id=rogue.node_id
    )
    member = pki.issue_node_cert(
        real_ca, csr, node_id=rogue.node_id, fabric_id=real_ca.fabric_id
    )
    server = _spy(servers, tmp_path, rogue.private_key, [member, real_ca.cert])
    code = _code(server.port, real_ca)
    err = _join_refused(identity, code)
    assert err.reason in (
        JoinReason.UNREACHABLE,
        JoinReason.TLS_ERROR,
        JoinReason.REFUSED,
    )
    time.sleep(0.5)
    # non-vacuous: the agent really went past the pin check and tried the
    # verifying channel (a second connection). TLS 1.3 lets the *server* see its
    # handshake finish before the client's certificate check aborts it, so the
    # proof is: two connections, zero application bytes.
    assert server.handshakes >= 2, server.handshakes
    _assert_learned_nothing(server, identity, code)


def test_flipping_one_bit_of_the_pin_means_nothing_is_sent_even_to_the_real_ca_holder(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    servers: list[SpyTlsServer],
    tmp_path: Path,
) -> None:
    issued = pki.issue_server_cert(real_ca, hosts=[LOOP])
    server = _spy(servers, tmp_path, issued.key, [issued.cert, real_ca.cert])
    pin = int(pki.short_fingerprint(real_ca.cert), 16) ^ 1
    code = join_code.encode(
        join_code.JoinCode(
            host=LOOP,
            port=server.port,
            token=join_code.new_token(),
            ca_pin=format(pin, "032x"),
        )
    )
    assert _join_refused(identity, code).reason is JoinReason.PIN_MISMATCH
    _assert_learned_nothing(server, identity, code)


def test_a_server_that_presents_no_ca_in_the_chain_is_not_trusted(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    servers: list[SpyTlsServer],
    tmp_path: Path,
) -> None:
    issued = pki.issue_server_cert(real_ca, hosts=[LOOP])
    server = _spy(servers, tmp_path, issued.key, [issued.cert])  # CA withheld
    code = _code(server.port, real_ca)
    assert _join_refused(identity, code).reason is JoinReason.PIN_MISMATCH
    _assert_learned_nothing(server, identity, code)


def test_tls_downgrade_and_non_tls_servers_are_refused(
    real_ca: pki.CertificateAuthority, identity: NodeIdentity
) -> None:
    listener = socket.socket()
    listener.bind((LOOP, 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    got: list[bytes] = []

    def serve() -> None:
        listener.settimeout(8)
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.settimeout(1)
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                got.append(conn.recv(100))
            except OSError:
                pass
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        code = _code(port, real_ca)
        assert _join_refused(identity, code).reason in (
            JoinReason.TLS_ERROR,
            JoinReason.UNREACHABLE,
        )
    finally:
        listener.close()
    assert not enrollment.is_enrolled(identity.path)


# ------------------------------------------- right CA, misbehaving controller


class _Servicer(enroll_pb2_grpc.EnrollmentServicer):
    def __init__(self, handler) -> None:  # type: ignore[no-untyped-def]
        self.handler = handler
        self.calls = 0

    def Enroll(self, request, context):  # type: ignore[no-untyped-def]  # noqa: N802
        self.calls += 1
        return self.handler(request, context)


@pytest.fixture
def legit_ca_server(real_ca: pki.CertificateAuthority) -> Iterator[object]:
    started: list[grpc.Server] = []

    def start(handler):  # type: ignore[no-untyped-def]
        issued = pki.issue_server_cert(real_ca, hosts=[LOOP])
        server = grpc.server(futures.ThreadPoolExecutor(4))
        servicer = _Servicer(handler)
        enroll_pb2_grpc.add_EnrollmentServicer_to_server(servicer, server)
        creds = grpc.ssl_server_credentials(
            [
                (
                    pki.key_to_pem(issued.key),
                    pki.cert_to_pem(issued.cert) + pki.cert_to_pem(real_ca.cert),
                )
            ]
        )
        port = server.add_secure_port(f"{LOOP}:0", creds)
        server.start()
        started.append(server)
        return port, servicer

    yield start
    for server in started:
        server.stop(0)


def test_poisoned_responses_from_a_correctly_pinned_controller_store_nothing(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    legit_ca_server,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    other = load_or_create(tmp_path / "someone" / "identity")
    other_csr = pki.build_csr(
        other.private_key, fabric_id=real_ca.fabric_id, node_id=other.node_id
    )
    others_cert = pki.issue_node_cert(
        real_ca, other_csr, node_id=other.node_id, fabric_id=real_ca.fabric_id
    )

    def poisoned(request, context):  # type: ignore[no-untyped-def]
        return enroll_pb2.EnrollResponse(
            node_cert_pem=pki.cert_to_pem(others_cert),
            ca_cert_pem=pki.cert_to_pem(real_ca.cert),
            fabric_id=real_ca.fabric_id,
            node_id=identity.node_id,
        )

    port, servicer = legit_ca_server(poisoned)
    code = _code(port, real_ca)
    err = _join_refused(identity, code)
    assert err.reason is JoinReason.BAD_RESPONSE
    assert servicer.calls == 1
    assert not enrollment.is_enrolled(identity.path)
    assert not (identity.path / "ca.crt").exists()


def test_empty_ok_response_and_a_huge_response_are_clean_errors(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    legit_ca_server,  # type: ignore[no-untyped-def]
) -> None:
    port, _ = legit_ca_server(lambda r, c: enroll_pb2.EnrollResponse())
    assert (
        _join_refused(identity, _code(port, real_ca)).reason is JoinReason.BAD_RESPONSE
    )
    port, _ = legit_ca_server(
        lambda r, c: enroll_pb2.EnrollResponse(node_cert_pem=b"A" * (8 * 1024 * 1024))
    )
    err = _join_refused(identity, _code(port, real_ca))
    assert err.reason in (
        JoinReason.REFUSED,
        JoinReason.BAD_RESPONSE,
        JoinReason.UNREACHABLE,
    )
    assert not enrollment.is_enrolled(identity.path)


def test_a_controller_that_never_answers_times_out_cleanly(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    legit_ca_server,  # type: ignore[no-untyped-def]
) -> None:
    def slow(request, context):  # type: ignore[no-untyped-def]
        time.sleep(6)
        return enroll_pb2.EnrollResponse()

    port, _ = legit_ca_server(slow)
    start = time.monotonic()
    with pytest.raises(JoinError) as info:
        enrollment.join(identity, _code(port, real_ca), timeout_s=1.5)
    assert info.value.reason is JoinReason.UNREACHABLE
    assert time.monotonic() - start < 5
    assert not enrollment.is_enrolled(identity.path)


def test_controller_error_text_is_sanitised_before_it_reaches_the_user(
    real_ca: pki.CertificateAuthority,
    identity: NodeIdentity,
    legit_ca_server,  # type: ignore[no-untyped-def]
) -> None:
    nasty = "\x1b[31mred\x1b]0;pwned\x07" + "A" * 5000

    def refuse(request, context):  # type: ignore[no-untyped-def]
        context.set_trailing_metadata((("meshforge-reason", "x" * 500),))
        context.abort(grpc.StatusCode.UNAUTHENTICATED, nasty)

    port, _ = legit_ca_server(refuse)
    err = _join_refused(identity, _code(port, real_ca))
    assert err.reason is JoinReason.REFUSED
    assert "\x1b" not in err.message and "\x07" not in err.message
    assert len(err.message) < 800
    assert isinstance(err.controller_reason, str) and len(err.controller_reason) <= 64
