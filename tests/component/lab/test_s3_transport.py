"""Component tests for spike S3 (0.2.7): real loopback servers, all transports.

One in-process :class:`S3Server` (raw TCP + gRPC + HTTP on ``127.0.0.1``) and
the measurement functions of ``_s3/client.py`` against it: download and upload
per transport with CRC verification, HTTP parallel ranges, and the failure
paths - malformed and hostile clients, peers vanishing mid-transfer on both
sides, oversized frames, bounded admission - all with typed errors and without
leaked threads or ports. Small payloads keep this fast; sizes are not measured
here, the mechanics are.
"""

from __future__ import annotations

import contextlib
import http.client
import itertools
import json
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s3 import payload  # noqa: E402
from spikes._s3.client import (  # noqa: E402
    Endpoint,
    control,
    fetch_stats,
    grpc_download,
    grpc_upload,
    hello,
    http_download,
    range_parts,
    tcp_download,
    tcp_upload,
)
from spikes._s3.context import ServerLimits  # noqa: E402
from spikes._s3.grpc_server import GRPC_IMPORT_ERROR  # noqa: E402
from spikes._s3.protocol import (  # noqa: E402
    GRPC_UPLOAD,
    KIB,
    MAX_CHUNK_BYTES,
    MAX_HEADER_BYTES,
    MIB,
    PROTOCOL,
    FailureType,
    TransferError,
    grpc_limit,
    grpc_target,
    recv_msg,
    send_msg,
)
from spikes._s3.server import (  # noqa: E402
    STOP_DURATION,
    STOP_IDLE,
    STOP_REQUESTED,
    S3Server,
)

pytestmark = pytest.mark.timeout(180)

SIZE: Final = 256 * KIB
CHUNK: Final = 64 * KIB
needs_grpc = pytest.mark.skipif(
    GRPC_IMPORT_ERROR is not None, reason=GRPC_IMPORT_ERROR or ""
)


def _endpoint(server: S3Server, **kw: Any) -> Endpoint:
    ports = server.ports
    base: dict[str, Any] = {
        "connect_timeout_s": 3,
        "io_timeout_s": 5,
        "transfer_timeout_s": 30,
    }
    base.update(kw)
    return Endpoint("127.0.0.1", ports["tcp"], ports["grpc"], ports["http"], **base)


def _wait_threads(baseline: int, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    while threading.active_count() > baseline and time.monotonic() < deadline:
        time.sleep(0.05)
    return threading.active_count()


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.fixture
def server() -> Iterator[S3Server]:
    srv = S3Server(
        "127.0.0.1",
        0,
        ServerLimits(idle_s=60, io_timeout_s=3, header_timeout_s=2, result_wait_s=3),
        name="inproc",
    )
    thread = srv.start_thread()
    deadline = time.monotonic() + 10
    while not srv.ports and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.ports, "server did not open its ports"
    yield srv
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive(), "server thread leaked"


def _check_sample(sample: dict[str, Any], *, transport: str, direction: str) -> None:
    assert sample["transport"] == transport and sample["direction"] == direction
    assert sample["bytes"] == SIZE and sample["server_bytes"] == SIZE
    assert sample["recv_mbps"] > 0 and sample["recv_mib_s"] > 0
    assert sample["wall_s"] > 0 and sample["transfer_s"] > 0
    assert sample["client_cpu_s"] >= 0 and sample["server_cpu_s"] >= 0
    assert sample["cpu_ms_per_mib"] >= 0
    assert sample["client_rss_peak_mib"] is None or sample["client_rss_peak_mib"] > 0


# ------------------------------------------------------------- happy paths
def test_hello_reports_identity_and_real_ports(server: S3Server) -> None:
    reply = hello(_endpoint(server))
    assert reply["name"] == "inproc" and reply["peer"] == "127.0.0.1"
    assert reply["protocol"] == PROTOCOL and reply["ports"] == server.ports
    assert reply["grpc_error"] == GRPC_IMPORT_ERROR
    assert server.ports["tcp"] != server.ports["http"]


def test_tcp_download_and_upload_verify_payload(server: S3Server) -> None:
    ep = _endpoint(server)
    down = tcp_download(ep, SIZE, CHUNK)
    _check_sample(down, transport="tcp", direction="download")
    up = tcp_upload(ep, SIZE, CHUNK)
    _check_sample(up, transport="tcp", direction="upload")
    assert up["server_seconds"] is not None and up["server_seconds"] > 0
    assert server.ctx.counters.transfers >= 2 and server.ctx.counters.errors == []


@needs_grpc
def test_grpc_download_and_upload_verify_payload(server: S3Server) -> None:
    ep = _endpoint(server)
    down = grpc_download(ep, SIZE, CHUNK)
    _check_sample(down, transport="grpc", direction="download")
    assert down["grpc_message_limit_bytes"] == grpc_limit(CHUNK)
    up = grpc_upload(ep, SIZE, CHUNK)
    _check_sample(up, transport="grpc", direction="upload")
    # the largest chunk (= gRPC message) works with the server's raised limit
    big = grpc_download(ep, 2 * MAX_CHUNK_BYTES, MAX_CHUNK_BYTES)
    assert big["bytes"] == 2 * MAX_CHUNK_BYTES


@pytest.mark.parametrize("parts", [1, 4])
def test_http_get_and_parallel_ranges(server: S3Server, parts: int) -> None:
    sample = http_download(_endpoint(server), SIZE, CHUNK, parts=parts)
    _check_sample(sample, transport="http", direction="download")
    assert sample["parts"] == parts and sample["ranged"] == (parts > 1)
    item = server.ctx.transfers.get(sample_transfer_id(server))
    assert item is not None and item.parts == parts and item.bytes == SIZE


def sample_transfer_id(server: S3Server) -> str:
    # the most recent transfer the server saw
    return next(reversed(server.ctx.transfers._items))


def test_range_parts_cover_size_exactly() -> None:
    for size, parts in ((SIZE, 4), (10, 3), (1, 4), (7, 7), (1_000_003, 16)):
        ranges = range_parts(size, parts)
        assert len(ranges) == min(parts, size)
        assert ranges[0][0] == 0 and sum(length for _, length in ranges) == size
        for (a, la), (b, _) in itertools.pairwise(ranges):
            assert a + la == b
        assert (
            max(length for _, length in ranges) - min(length for _, length in ranges)
            <= 1
        )
    with pytest.raises(ValueError):
        range_parts(10, 0)


def test_stats_for_unknown_transfer_is_typed(server: S3Server) -> None:
    with pytest.raises(TransferError) as exc:
        fetch_stats(_endpoint(server), "tcp-nope")
    assert exc.value.failure_type is FailureType.NO_STATS
    assert not exc.value.fatal


# ------------------------------------------------------- malformed clients
def _raw(server: S3Server, port_key: str = "tcp") -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.ports[port_key]), timeout=5)
    sock.settimeout(5)
    return sock


def test_tcp_garbage_and_oversized_frames_are_dropped(server: S3Server) -> None:
    for junk in (
        b"\xff\xff\xff\xff",  # 4 GiB length prefix: never allocated
        struct.pack("!I", MAX_HEADER_BYTES + 1) + b"x" * 64,
        b"\x00\x00\x00\x05hello",
        b"GET / HTTP/1.0\r\n\r\n",
        b"",
    ):
        # Dropping the peer *is* the behaviour under test, so the server may
        # already have reset the connection before we finish writing: any of
        # sendall/shutdown/recv can then raise (ECONNRESET, EPIPE, ENOTCONN).
        # That is a pass, not a flake - the proof the junk was dropped is the
        # counters and the still-healthy hello below, never this recv.
        with _raw(server) as sock, contextlib.suppress(OSError):
            sock.sendall(junk)
            sock.shutdown(socket.SHUT_WR)
            assert sock.recv(100) == b""
    # slow-loris: no header within header_timeout -> dropped, counted
    with _raw(server) as sock:
        sock.settimeout(10)
        assert sock.recv(10) == b""
    assert server.ctx.counters.header_timeouts >= 1
    assert server.ctx.counters.bad_requests >= 3
    assert hello(_endpoint(server))["ok"] is True


def test_tcp_invalid_requests_get_typed_refusals(server: S3Server) -> None:
    ep = _endpoint(server)
    with pytest.raises(TransferError, match="unknown op") as exc:
        control(ep, {"op": "format-disk"})
    assert exc.value.failure_type is FailureType.SERVER_ERROR
    with pytest.raises(TransferError, match="protocol must be"):
        control(ep, {"op": "hello", "protocol": 99})
    with pytest.raises(TransferError, match="bad size"):
        control(ep, {"op": "download", "transfer_id": "x", "size": 1, "chunk": CHUNK})
    with pytest.raises(TransferError, match="bad chunk"):
        control(
            ep,
            {"op": "download", "transfer_id": "x", "size": SIZE, "chunk": 8 * MIB},
        )
    with pytest.raises(TransferError, match="bad transfer_id"):
        control(
            ep, {"op": "upload", "transfer_id": "../x", "size": SIZE, "chunk": CHUNK}
        )
    with pytest.raises(TransferError, match="bad parts"):
        control(ep, {"op": "stats", "transfer_id": "x", "parts": 1000})
    assert hello(ep)["ok"] is True


def test_tcp_upload_with_wrong_bytes_is_a_checksum_mismatch(server: S3Server) -> None:
    with _raw(server) as sock:
        send_msg(
            sock,
            {
                "protocol": PROTOCOL,
                "op": "upload",
                "transfer_id": "tcp-bad",
                "size": SIZE,
                "chunk": CHUNK,
            },
        )
        sock.sendall(b"\x00" * SIZE)
        sock.shutdown(socket.SHUT_WR)
        reply = recv_msg(sock)
    assert reply["ok"] is False and reply["stats"]["crc_ok"] is False
    assert reply["stats"]["bytes"] == SIZE and "checksum" in reply["error"]


def test_tcp_upload_short_then_close_is_reported(server: S3Server) -> None:
    with _raw(server) as sock:
        send_msg(
            sock,
            {
                "protocol": PROTOCOL,
                "op": "upload",
                "transfer_id": "tcp-short",
                "size": SIZE,
                "chunk": CHUNK,
            },
        )
        sock.sendall(bytes(payload.chunk_view(0, 1000)))
        sock.shutdown(socket.SHUT_WR)
        reply = recv_msg(sock)
    assert reply["ok"] is False and reply["stats"]["bytes"] == 1000


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/blob?size=262144&chunk=65536&transfer=t1", {"Range": "bytes=5-2"}, 416),
        ("/blob?size=262144&chunk=65536&transfer=t1", {"Range": "bytes=0-1,5-6"}, 416),
        ("/blob?size=262144&chunk=65536&transfer=t1", {"Range": "bytes=999999-"}, 416),
        ("/blob?size=262144&chunk=65536&transfer=t1", {"Range": "items=0-1"}, 416),
        ("/blob?chunk=65536&transfer=t1", {}, 400),
        ("/blob?size=1&transfer=t1", {}, 400),
        ("/blob?size=999999999999&transfer=t1", {}, 400),
        ("/blob?size=262144&chunk=1&transfer=t1", {}, 400),
        ("/blob?size=262144&chunk=65536&transfer=../x", {}, 400),
        ("/blob?size=abc&transfer=t1", {}, 400),
        ("/stats?transfer=nope", {}, 404),
        ("/stats?transfer=t1&parts=0", {}, 400),
        ("/nope", {}, 404),
    ],
)
def test_http_bad_requests_are_refused(
    server: S3Server, path: str, headers: dict[str, str], status: int
) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.ports["http"], timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        assert response.status == status
        body = response.read()
        assert len(body) < 4096
        if status == 416:
            assert response.getheader("Content-Range") == "bytes */262144"
    finally:
        conn.close()
    assert hello(_endpoint(server))["ok"] is True


def test_http_range_forms_and_hello(server: S3Server) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.ports["http"], timeout=5)
    try:
        for header, offset, length in (
            ("bytes=10-19", 10, 10),
            ("bytes=262000-", 262000, 144),
            ("bytes=-100", SIZE - 100, 100),
            ("bytes=0-999999", 0, SIZE),
        ):
            conn.request(
                "GET",
                "/blob?size=262144&chunk=65536&transfer=t-range",
                headers={"Range": header},
            )
            response = conn.getresponse()
            assert response.status == 206, header
            body = response.read()
            assert len(body) == length
            assert body == bytes(payload.chunk_view(offset, length))
            assert (
                response.getheader("Content-Range")
                == f"bytes {offset}-{offset + length - 1}/{SIZE}"
            )
        conn.request("GET", "/hello")
        doc = json.loads(conn.getresponse().read())
        assert doc["ok"] is True and doc["name"] == "inproc"
        conn.request("GET", "/stats?transfer=t-range&parts=4")
        doc = json.loads(conn.getresponse().read())
        assert doc["ok"] is True and doc["stats"]["parts"] == 4
        assert doc["stats"]["bytes"] == 10 + 144 + 100 + SIZE
    finally:
        conn.close()


@needs_grpc
def test_grpc_bad_request_and_oversized_message_are_typed(server: S3Server) -> None:
    import grpc

    ep = _endpoint(server)
    channel = grpc.insecure_channel(grpc_target("127.0.0.1", ep.grpc_port))
    try:
        upload = channel.stream_unary(GRPC_UPLOAD, lambda b: b, lambda b: b)
        with pytest.raises(grpc.RpcError) as exc:
            upload(iter([b"not json"]), timeout=5)
        assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.RpcError) as exc:
            upload(iter([]), timeout=5)
        assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    finally:
        channel.close()
    # a message larger than the server's raised limit is refused, never buffered
    huge = grpc_limit(MAX_CHUNK_BYTES) + 1
    channel = grpc.insecure_channel(
        grpc_target("127.0.0.1", ep.grpc_port),
        options=[("grpc.max_send_message_length", huge + KIB)],
    )
    try:
        upload = channel.stream_unary(GRPC_UPLOAD, lambda b: b, lambda b: b)
        header = json.dumps(
            {"transfer_id": "grpc-huge", "size": 2 * MAX_CHUNK_BYTES, "chunk": CHUNK}
        ).encode()
        with pytest.raises(grpc.RpcError) as exc:
            upload(iter([header, b"x" * huge]), timeout=10)
        assert exc.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        channel.close()
    assert server.ctx.counters.bad_requests >= 1
    # the client wrapper maps the same refusal to a typed, non-fatal failure
    with pytest.raises(TransferError) as terr:
        grpc_download(
            ep, 2 * MAX_CHUNK_BYTES, MAX_CHUNK_BYTES, limit=MAX_CHUNK_BYTES - 1
        )
    assert terr.value.failure_type is FailureType.TRANSPORT_ERROR
    assert not terr.value.fatal
    assert grpc_download(ep, SIZE, CHUNK)["bytes"] == SIZE


# --------------------------------------------------- peer vanishes (server)
def test_tcp_client_vanishing_mid_download_is_recorded(server: S3Server) -> None:
    before = threading.active_count()
    with _raw(server) as sock:
        send_msg(
            sock,
            {
                "protocol": PROTOCOL,
                "op": "download",
                "transfer_id": "tcp-vanish",
                "size": 64 * MIB,
                "chunk": MIB,
            },
        )
        assert len(sock.recv(4096)) > 0
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    item = server.ctx.transfers.wait_for("tcp-vanish", 1, timeout_s=15)
    assert item is not None and item.parts == 1 and item.error is not None
    assert item.bytes < 64 * MIB
    assert _wait_threads(before) <= before
    assert tcp_download(_endpoint(server), SIZE, CHUNK)["bytes"] == SIZE


def test_http_client_vanishing_mid_download_is_recorded(server: S3Server) -> None:
    before = threading.active_count()
    with _raw(server, "http") as sock:
        sock.sendall(
            b"GET /blob?size=67108864&chunk=1048576&transfer=http-vanish HTTP/1.1\r\n"
            b"Host: x\r\n\r\n"
        )
        assert len(sock.recv(4096)) > 0
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    item = server.ctx.transfers.wait_for("http-vanish", 1, timeout_s=15)
    assert item is not None and item.parts == 1 and item.bytes < 64 * MIB
    assert item.error is not None
    assert _wait_threads(before) <= before
    assert http_download(_endpoint(server), SIZE, CHUNK)["bytes"] == SIZE


@needs_grpc
def test_grpc_client_cancelling_mid_download_is_recorded(server: S3Server) -> None:
    import grpc

    from spikes._s3.protocol import GRPC_DOWNLOAD

    ep = _endpoint(server)
    channel = grpc.insecure_channel(grpc_target("127.0.0.1", ep.grpc_port))
    try:
        download = channel.unary_stream(GRPC_DOWNLOAD, lambda b: b, lambda b: b)
        request = json.dumps(
            {"transfer_id": "grpc-vanish", "size": 64 * MIB, "chunk": CHUNK}
        ).encode()
        call = download(request, timeout=30)
        first = next(iter(call))
        assert len(first) == CHUNK
        call.cancel()
    finally:
        channel.close()
    item = server.ctx.transfers.wait_for("grpc-vanish", 1, timeout_s=15)
    assert item is not None and item.parts == 1 and item.bytes < 64 * MIB
    assert item.error is not None
    assert grpc_download(ep, SIZE, CHUNK)["bytes"] == SIZE


# --------------------------------------------------- peer vanishes (client)
@contextlib.contextmanager
def fake_tcp_server(
    handler: Callable[[socket.socket], None],
) -> Iterator[int]:
    """A one-shot server on 127.0.0.1 that runs ``handler`` on the first client."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(10)
                handler(conn)
        except OSError as exc:  # pragma: no cover - reported via errors
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()
        thread.join(timeout=10)
    assert not thread.is_alive()


def _download_then(
    action: Callable[[socket.socket], None],
) -> Callable[[socket.socket], None]:
    def handler(conn: socket.socket) -> None:
        msg = recv_msg(conn)
        assert msg["op"] == "download"
        action(conn)

    return handler


def test_client_short_transfer_when_server_closes_early() -> None:
    with (
        fake_tcp_server(
            _download_then(lambda c: c.sendall(bytes(payload.chunk_view(0, 1000))))
        ) as port,
        pytest.raises(TransferError) as exc,
    ):
        tcp_download(Endpoint("127.0.0.1", port, 0, 0, io_timeout_s=5), SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.SHORT_TRANSFER
    assert "1000 of 262144" in str(exc.value) and not exc.value.fatal


def test_client_checksum_mismatch_when_server_sends_wrong_bytes() -> None:
    with (
        fake_tcp_server(_download_then(lambda c: c.sendall(b"\x00" * SIZE))) as port,
        pytest.raises(TransferError) as exc,
    ):
        tcp_download(Endpoint("127.0.0.1", port, 0, 0, io_timeout_s=5), SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.CHECKSUM_MISMATCH


def test_client_times_out_when_server_stalls() -> None:
    def stall(conn: socket.socket) -> None:
        conn.sendall(bytes(payload.chunk_view(0, 100)))
        time.sleep(1.5)

    with fake_tcp_server(_download_then(stall)) as port:
        started = time.monotonic()
        with pytest.raises(TransferError) as exc:
            tcp_download(
                Endpoint("127.0.0.1", port, 0, 0, io_timeout_s=0.5), SIZE, CHUNK
            )
        assert time.monotonic() - started < 5
    assert exc.value.failure_type is FailureType.TIMEOUT and exc.value.fatal


def test_client_upload_reply_garbage_is_a_protocol_error() -> None:
    def garbage(conn: socket.socket) -> None:
        recv_msg(conn)
        while conn.recv(65536):
            pass
        conn.sendall(b"\x00\x00\x00\x03[1]")

    with fake_tcp_server(garbage) as port, pytest.raises(TransferError) as exc:
        tcp_upload(Endpoint("127.0.0.1", port, 0, 0, io_timeout_s=5), SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.PROTOCOL_ERROR


def test_dead_server_gives_typed_fatal_errors_quickly() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound but not listening: connections refused
        ep = Endpoint(
            "127.0.0.1", port, port, port, connect_timeout_s=2, io_timeout_s=2
        )
        started = time.monotonic()
        for fn in (tcp_download, tcp_upload, http_download):
            with pytest.raises(TransferError) as exc:
                fn(ep, SIZE, CHUNK)
            assert exc.value.failure_type is FailureType.SERVER_UNREACHABLE, fn
            assert exc.value.fatal
        if GRPC_IMPORT_ERROR is None:
            with pytest.raises(TransferError) as exc:
                grpc_download(ep, SIZE, CHUNK)
            assert exc.value.failure_type is FailureType.SERVER_UNREACHABLE
        assert time.monotonic() - started < 15


def test_grpc_port_zero_means_typed_unavailable(server: S3Server) -> None:
    ep = _endpoint(server)
    no_grpc = Endpoint(ep.host, ep.tcp_port, 0, ep.http_port)
    with pytest.raises(TransferError) as exc:
        grpc_download(no_grpc, SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.GRPC_UNAVAILABLE
    assert not exc.value.fatal


# ------------------------------------------------------- admission bounded
@pytest.fixture
def small_server() -> Iterator[S3Server]:
    srv = S3Server(
        "127.0.0.1",
        0,
        ServerLimits(idle_s=60, io_timeout_s=3, header_timeout_s=8, max_connections=2),
        name="small",
    )
    thread = srv.start_thread()
    while not srv.ports:
        time.sleep(0.02)
    yield srv
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive()


def test_tcp_admission_is_bounded_and_recovers(small_server: S3Server) -> None:
    idle = [_raw(small_server) for _ in range(2)]
    try:
        time.sleep(0.2)
        with _raw(small_server) as third:
            assert third.recv(10) == b""  # refused: no slot
        assert small_server.ctx.counters.refused_busy == 1
    finally:
        for sock in idle:
            sock.close()
    deadline = time.monotonic() + 5
    while small_server.tcp._conns and time.monotonic() < deadline:
        time.sleep(0.05)
    assert hello(_endpoint(small_server))["ok"] is True


def test_http_admission_is_bounded_and_refusals_do_not_free_slots(
    small_server: S3Server,
) -> None:
    http_srv = small_server._http
    assert http_srv is not None
    idle = [_raw(small_server, "http") for _ in range(2)]
    try:
        deadline = time.monotonic() + 5
        while http_srv.active_connections < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert http_srv.active_connections == 2
        for _ in range(3):  # refused connections must not decrement the count
            with _raw(small_server, "http") as extra:
                assert extra.recv(10) == b""
        assert small_server.ctx.counters.refused_busy >= 3
        assert http_srv.active_connections == 2
    finally:
        for sock in idle:
            sock.close()
    deadline = time.monotonic() + 5
    while http_srv.active_connections and time.monotonic() < deadline:
        time.sleep(0.05)
    assert http_srv.active_connections == 0
    assert http_download(_endpoint(small_server), SIZE, CHUNK)["bytes"] == SIZE


# ------------------------------------------------------------ lifecycle
def test_stop_joins_threads_and_frees_ports() -> None:
    before = threading.active_count()
    srv = S3Server("127.0.0.1", 0, ServerLimits(idle_s=60, io_timeout_s=3))
    thread = srv.start_thread()
    while not srv.ports:
        time.sleep(0.02)
    ports = dict(srv.ports)
    ep = _endpoint(srv)
    tcp_download(ep, SIZE, CHUNK)
    http_download(ep, SIZE, CHUNK, parts=4)
    if GRPC_IMPORT_ERROR is None:
        grpc_upload(ep, SIZE, CHUNK)
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive() and srv.stop_reason == STOP_REQUESTED
    assert _wait_threads(before) <= before, threading.enumerate()
    for port in ports.values():
        if port:
            assert _port_is_free(port), port


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (ServerLimits(idle_s=0.3), STOP_IDLE),
        (ServerLimits(duration_s=0.3), STOP_DURATION),
    ],
)
def test_server_auto_stops(limits: ServerLimits, reason: str) -> None:
    srv = S3Server("127.0.0.1", 0, limits)
    started = time.monotonic()
    assert srv.serve() == reason
    assert time.monotonic() - started < 10


def test_open_fails_cleanly_on_a_busy_port() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        srv = S3Server("127.0.0.1", port, ServerLimits(idle_s=5))
        with pytest.raises(OSError):
            srv.open()
        srv.close()
    assert _port_is_free(port)


def test_ready_file_is_written_atomically(tmp_path: Path) -> None:
    srv = S3Server("127.0.0.1", 0, ServerLimits(idle_s=5))
    ports = srv.open()
    try:
        target = tmp_path / "sub" / "ports.json"
        srv.write_ready_file(target)
        assert json.loads(target.read_text(encoding="utf-8")) == ports
        assert not list(target.parent.glob("*.tmp"))
    finally:
        srv.close()
