"""V1.0 G3 (D413, reviewer M2 socket half): every non-loopback listener of a real
controller demands TLS, and then a client certificate (mTLS) or a token.

A real ``python -m meshforge.controller`` process in ``mtls`` mode (the cluster
harness, no agents) binds its gRPC, HTTP and enrolment listeners on a
**non-loopback** address of this host. The test then reads the process's own
listening sockets from the OS (psutil) - not from the config - and probes each
non-loopback one from outside, at the socket level:

* **plain TCP**: an HTTP/1.1 request and an HTTP/2 preface get no plaintext
  HTTP or HTTP/2 answer (the port speaks TLS only);
* **TLS without a client certificate** (fabric CA trusted):
  - gRPC session port: refused - TLS 1.3 ``certificate required`` (mTLS);
  - HTTP port: TLS is fine (the CLI has no cert) but every API call needs a
    token: ``GET /v1/nodes`` / ``/v1/jobs`` 401, only ``/healthz`` is open;
  - enrolment port: TLS is fine (a joining node has no cert yet) but ``Enroll``
    with an unknown join token is refused ``token_unknown``;
* **TLS with a foreign fabric's node certificate**: the gRPC port still
  refuses it; on HTTP it is no credential (401).

Any other non-loopback listener of the controller process fails the test: a
new port must be added here with its auth rule. The unit half (every route of
the app) is ``tests/unit/controller/test_v10_g3_route_inventory.py``.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("psutil", reason="needs the agent extra")

import psutil  # type: ignore[import-untyped]  # no stubs; agent extra
from cryptography.hazmat.primitives import serialization
from mf_cluster import Fabric

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.docker,
    pytest.mark.security,
    pytest.mark.timeout(300),
]


def _lan_ipv4() -> str | None:
    """A non-loopback IPv4 address of an interface that is up."""
    stats = psutil.net_if_stats()
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        if name in stats and not stats[name].isup:
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(addr.address)
            if not ip.is_loopback and not ip.is_link_local:
                return str(ip)
    return None


LAN_IP = _lan_ipv4()


@pytest.fixture
def controller(pg_settings: DatabaseSettings, tmp_path: Path) -> Iterator[Fabric]:
    if LAN_IP is None:
        pytest.skip("this host has no non-loopback IPv4 address to bind")
    with Fabric(
        pg_settings,
        tmp_path / "fabric",
        agents=0,
        bind_host=LAN_IP,
    ) as fabric:
        yield fabric


def _port(target: str) -> int:
    return int(target.rsplit(":", 1)[1])


def _plain_ip(text: str) -> str:
    """``::ffff:a.b.c.d`` (an IPv4 address on a dual-stack socket) -> ``a.b.c.d``."""
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _listeners(pid: int) -> set[tuple[str, int]]:
    """(address, port) of every TCP LISTEN socket of ``pid`` and its children."""
    out: set[tuple[str, int]] = set()
    proc = psutil.Process(pid)
    for p in (proc, *proc.children(recursive=True)):
        for conn in p.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN:
                out.add((_plain_ip(str(conn.laddr.ip)), int(conn.laddr.port)))
    return out


def _ca_context(fabric: Fabric, *, alpn: list[str] | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(fabric.ca_dir / "ca.crt"))
    if alpn:
        context.set_alpn_protocols(alpn)
    return context


def _foreign_identity(tmp: Path) -> tuple[Path, Path]:
    """A real node certificate + key of **another** fabric (the wrong cert)."""
    fabric = new_fabric_id()
    node = new_node_id()
    ca = pki.create_ca(fabric)
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca, pki.build_csr(key, fabric_id=fabric, node_id=node),
        node_id=node, fabric_id=fabric,
    )  # fmt: skip
    cert_path, key_path = tmp / "foreign.crt", tmp / "foreign.key"
    cert_path.write_bytes(pki.cert_to_pem(cert))
    key_path.write_bytes(pki.key_to_pem(key))
    return cert_path, key_path


def _plain_reply(host: str, port: int, payload: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        sock.settimeout(3)
        try:
            return sock.recv(4096)
        except (TimeoutError, ConnectionResetError):
            return b""


def _tls_outcome(host: str, port: int, context: ssl.SSLContext) -> str:
    """``refused`` when the server rejects the client (no / wrong certificate),
    else ``open`` (the TLS session works and the server waits for a request)."""
    with socket.create_connection((host, port), timeout=5) as raw:
        try:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                # TLS 1.3: a missing/unknown client cert is reported after the
                # client's Finished - on the first read.
                tls.settimeout(2)
                try:
                    data = tls.recv(1)
                except TimeoutError:
                    return "open"
                return "refused" if data == b"" else "open"
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
            return "refused"


def _https(host: str, port: int, context: ssl.SSLContext, path: str) -> int:
    with (
        socket.create_connection((host, port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        tls.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        tls.settimeout(10)
        head = tls.recv(64)
    if not head:
        return 0  # closed without an answer: the TLS layer refused the client
    assert head.startswith(b"HTTP/1.1 "), head
    return int(head.split()[1])


H2_PREFACE = (
    b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"
)


# ------------------------------------------------------------------------ tests
def test_every_non_loopback_listener_is_a_known_authenticated_port(
    controller: Fabric,
) -> None:
    assert controller.controller is not None
    grpc_port = _port(controller.grpc_target)
    http_port = _port(controller.http_base)
    enroll_port = _port(controller.enroll_target)
    listening = _listeners(controller.controller.pid)
    exposed = {
        (ip, port) for ip, port in listening if not ipaddress.ip_address(ip).is_loopback
    }
    assert exposed, listening
    assert {port for _, port in exposed} == {grpc_port, http_port, enroll_port}, (
        f"unexpected non-loopback listener(s): {sorted(exposed)} - add it to this "
        "test with its auth rule"
    )
    for ip, _ in exposed:
        assert ip == LAN_IP, f"a wildcard or other address is bound: {ip}"


def test_plaintext_gets_no_http_answer_on_any_port(controller: Fabric) -> None:
    for target in (
        controller.grpc_target,
        controller.http_base,
        controller.enroll_target,
    ):
        port = _port(target)
        reply = _plain_reply(
            LAN_IP or "",
            port,
            f"GET /healthz HTTP/1.1\r\nHost: {LAN_IP}\r\n\r\n".encode(),
        )
        assert not reply.startswith(b"HTTP/"), (target, reply[:40])
        h2 = _plain_reply(LAN_IP or "", port, H2_PREFACE)
        # a plaintext HTTP/2 server answers with a SETTINGS frame (type 0x04)
        assert not (len(h2) >= 9 and h2[3] == 0x04), (target, h2[:16])


def test_grpc_session_port_demands_a_fabric_client_certificate(
    controller: Fabric, tmp_path: Path
) -> None:
    host, port = LAN_IP or "", _port(controller.grpc_target)
    assert _tls_outcome(host, port, _ca_context(controller, alpn=["h2"])) == "refused"
    foreign = _ca_context(controller, alpn=["h2"])
    foreign.load_cert_chain(*_foreign_identity(tmp_path))
    assert _tls_outcome(host, port, foreign) == "refused"
    # control: the probe can tell - a node certificate of THIS fabric gets in
    own = _ca_context(controller, alpn=["h2"])
    own.load_cert_chain(*_own_identity(controller, tmp_path))
    assert _tls_outcome(host, port, own) == "open"


def _own_identity(fabric: Fabric, tmp: Path) -> tuple[Path, Path]:
    from meshforge.controller.fabric_ca import load_fabric

    authority = load_fabric(fabric.ca_dir).authority
    node = new_node_id()
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        authority,
        pki.build_csr(key, fabric_id=authority.fabric_id, node_id=node),
        node_id=node,
        fabric_id=authority.fabric_id,
    )
    cert_path, key_path = tmp / "own.crt", tmp / "own.key"
    cert_path.write_bytes(pki.cert_to_pem(cert))
    key_path.write_bytes(pki.key_to_pem(key))
    return cert_path, key_path


def test_http_port_is_tls_and_every_api_call_needs_a_token(
    controller: Fabric, tmp_path: Path
) -> None:
    host, port = LAN_IP or "", _port(controller.http_base)
    context = _ca_context(controller, alpn=["http/1.1"])
    assert _https(host, port, context, "/healthz") == 200  # D148: the one open read
    for path in ("/v1/nodes", "/v1/jobs", "/v1/sessions", "/v1/fabric/invites"):
        assert _https(host, port, context, path) == 401, path
    foreign = _ca_context(controller, alpn=["http/1.1"])
    foreign.load_cert_chain(*_foreign_identity(tmp_path))
    # a client certificate is optional here (agents present one for blobs) -
    # but a foreign one either fails the handshake or is no credential.
    # TLS 1.3: the client's handshake is done before the server has checked the
    # client certificate, so a refusal surfaces on the first read, as one of: a
    # TLS alert (SSLError); a bare EOF (asyncio closes without the alert) -> 0;
    # or EPIPE / ECONNRESET when OpenSSL answers that EOF with its own alert
    # after our request bytes drew a RST (ConnectionError). All mean refused.
    try:
        status = _https(host, port, foreign, "/v1/nodes")
    except (ssl.SSLError, ConnectionError):
        status = 0
    assert status in (0, 401)


def test_enrollment_port_needs_a_valid_join_token(controller: Fabric) -> None:
    host, port = LAN_IP or "", _port(controller.enroll_target)
    # gRPC needs ALPN h2; no client certificate is asked for, by design (D150)
    context = _ca_context(controller, alpn=["h2"])
    assert _tls_outcome(host, port, context) == "open"
    ca_pem = (controller.ca_dir / "ca.crt").read_bytes()
    fabric_id = pki.parse_fabric_id(pki.cert_from_pem(ca_pem))
    key = pki.generate_key()
    csr = pki.build_csr(key, fabric_id=fabric_id, node_id=new_node_id())
    channel = grpc.secure_channel(
        f"{host}:{port}", grpc.ssl_channel_credentials(root_certificates=ca_pem)
    )
    try:
        stub: Any = enroll_pb2_grpc.EnrollmentStub(channel)
        with pytest.raises(grpc.RpcError) as refused:
            stub.Enroll(
                enroll_pb2.EnrollRequest(
                    join_token=b"\x01" * 16,
                    csr_pem=csr.public_bytes(serialization.Encoding.PEM),
                ),
                timeout=20,
            )
    finally:
        channel.close()
    error: Any = refused.value  # a grpc.Call too (code, trailing metadata)
    assert error.code() in (
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.NOT_FOUND,
    ), error
    reasons = {str(k): v for k, v in error.trailing_metadata() or ()}
    assert reasons.get("meshforge-reason") == "token_unknown", reasons
