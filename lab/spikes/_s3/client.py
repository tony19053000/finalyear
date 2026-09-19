"""One repetition of one transfer, per transport. Plain functions, no CLI.

Every function returns a sample dict with the same keys, so the report can put
raw TCP, gRPC and HTTP in one table:

``recv_mbps`` / ``recv_mib_s``
    Throughput **at the receiver**. The window is *request sent -> last byte*
    for downloads and the server's own *request seen -> last byte* for uploads.
    Connection establishment is excluded for every transport alike (the socket,
    the HTTP connections and the gRPC channel are all connected before the clock
    starts), but request latency is included - otherwise a 1 MiB payload that
    arrives in a single message would show an empty window and infinite speed.
``wall_s`` / ``ttfb_s`` / ``stream_s``
    The same window, the time to the first byte, and first-byte-to-last-byte.
    ``stream_s`` is the steady-state stream rate for large payloads; it is kept
    as raw evidence, not as the headline number.
``client_cpu_s`` / ``server_cpu_s`` / ``cpu_ms_per_mib``
    CPU seconds burned in each process during the transfer window - how much of
    the result is Python and framing rather than the link.
``client_rss_peak_mib`` / ``server_rss_peak_mib``
    Process-wide peak RSS: evidence that nothing buffers the whole payload.

Correctness is not optional: the receiving side CRC-32s what it got and compares
it with the locally generated payload, so a transport that loses or reorders
bytes raises ``checksum_mismatch`` instead of a fast number.
"""

from __future__ import annotations

import contextlib
import http.client
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

from spikes._s3 import payload
from spikes._s3.grpc_server import GRPC_IMPORT_ERROR, identity_bytes, require_grpc
from spikes._s3.grpc_server import decode as decode_json
from spikes._s3.grpc_server import encode as encode_json

if GRPC_IMPORT_ERROR is None:
    import grpc
from spikes._s3.meters import ClientMeter, peak_rss_mib
from spikes._s3.protocol import (
    GRPC_DOWNLOAD,
    GRPC_UPLOAD,
    HTTP_BLOB_PATH,
    PROTOCOL,
    FailureType,
    Message,
    ProtocolError,
    TransferError,
    classify_os_error,
    grpc_limit,
    grpc_target,
    recv_msg,
    send_msg,
)

MIB: Final = 1024 * 1024
_RECV_BUFFER_BYTES: Final = 256 * 1024


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    tcp_port: int
    grpc_port: int
    http_port: int
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 30.0
    #: hard cap on one repetition, so a vanished peer costs one timeout
    transfer_timeout_s: float = 600.0


def new_transfer_id(prefix: str = "t") -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _deadline(ep: Endpoint) -> float:
    return time.monotonic() + ep.transfer_timeout_s


def _check_deadline(deadline: float, doing: str) -> None:
    if time.monotonic() > deadline:
        raise TransferError(FailureType.TIMEOUT, f"{doing}: transfer timeout")


# --------------------------------------------------------------- control
def _connect(ep: Endpoint) -> socket.socket:
    try:
        sock = socket.create_connection(
            (ep.host, ep.tcp_port), timeout=ep.connect_timeout_s
        )
    except OSError as exc:
        raise classify_os_error(exc, f"connect {ep.host}:{ep.tcp_port}") from exc
    sock.settimeout(ep.io_timeout_s)
    return sock


def control(ep: Endpoint, msg: Message) -> Message:
    """One request/reply exchange on a fresh control connection."""
    sock = _connect(ep)
    try:
        send_msg(sock, {"protocol": PROTOCOL, **msg})
        reply = recv_msg(sock)
    except OSError as exc:
        raise classify_os_error(exc, f"{msg.get('op')} request") from exc
    except ProtocolError as exc:
        raise TransferError(FailureType.PROTOCOL_ERROR, str(exc)) from exc
    finally:
        sock.close()
    if reply.get("ok") is not True:
        raise TransferError(FailureType.SERVER_ERROR, str(reply.get("error", reply)))
    return reply


def hello(ep: Endpoint) -> Message:
    """Server identity and the ports it listens on."""
    reply = control(ep, {"op": "hello"})
    if reply.get("protocol") != PROTOCOL or not isinstance(reply.get("hostname"), str):
        raise TransferError(FailureType.PROTOCOL_ERROR, f"unexpected hello: {reply}")
    return reply


def fetch_stats(ep: Endpoint, transfer_id: str, parts: int = 1) -> Message:
    """The server's view of a finished transfer (any transport)."""
    try:
        reply = control(ep, {"op": "stats", "transfer_id": transfer_id, "parts": parts})
    except TransferError as exc:
        if exc.failure_type is FailureType.SERVER_ERROR:
            raise TransferError(
                FailureType.NO_STATS, f"server has no stats for {transfer_id}"
            ) from exc
        raise
    stats = reply.get("stats")
    if not isinstance(stats, dict):
        raise TransferError(FailureType.NO_STATS, "stats reply without a body")
    return stats


# ------------------------------------------------------------ sample build
def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def _sample(
    *,
    transport: str,
    direction: str,
    size: int,
    chunk: int,
    meter: ClientMeter,
    recv_seconds: float,
    stats: Message,
    parts: int = 1,
    extra: Message | None = None,
) -> dict[str, Any]:
    server_cpu = _number(stats.get("cpu_s")) or 0.0
    client_cpu = meter.cpu_s
    mib = size / MIB
    seconds = max(recv_seconds, 1e-9)
    return {
        "transport": transport,
        "direction": direction,
        "bytes": size,
        "chunk_bytes": chunk,
        "parts": parts,
        "wall_s": meter.wall_s,
        "ttfb_s": meter.ttfb_s,
        "stream_s": meter.transfer_s,
        "transfer_s": seconds,
        "recv_mbps": size * 8 / 1e6 / seconds,
        "recv_mib_s": mib / seconds,
        "client_cpu_s": client_cpu,
        "server_cpu_s": server_cpu,
        "cpu_ms_per_mib": (client_cpu + server_cpu) * 1000.0 / mib if mib else 0.0,
        "client_rss_peak_mib": peak_rss_mib(),
        "server_rss_peak_mib": _number(stats.get("rss_peak_mib")),
        "server_bytes": stats.get("bytes"),
        "server_seconds": _number(stats.get("seconds")),
        **(extra or {}),
    }


def _require_complete(verifier: payload.Verifier, size: int, doing: str) -> None:
    if verifier.received != size:
        raise TransferError(
            FailureType.SHORT_TRANSFER,
            f"{doing}: received {verifier.received} of {size} bytes",
        )
    if not verifier.matches(size):
        raise TransferError(
            FailureType.CHECKSUM_MISMATCH, f"{doing}: CRC-32 mismatch over {size} bytes"
        )


def _check_upload_reply(reply: Message, size: int, doing: str) -> Message:
    stats = reply.get("stats")
    if not isinstance(stats, dict):
        raise TransferError(FailureType.NO_STATS, f"{doing}: no server stats")
    if reply.get("ok") is not True:
        failure = (
            FailureType.CHECKSUM_MISMATCH
            if stats.get("crc_ok") is False
            else FailureType.SERVER_ERROR
        )
        raise TransferError(failure, f"{doing}: {reply.get('error')}")
    if stats.get("bytes") != size:
        raise TransferError(
            FailureType.SHORT_TRANSFER,
            f"{doing}: server received {stats.get('bytes')} of {size} bytes",
        )
    return stats


# -------------------------------------------------------------- raw TCP
def tcp_download(ep: Endpoint, size: int, chunk: int) -> dict[str, Any]:
    """Server -> client over one plain TCP connection (the control baseline)."""
    transfer_id = new_transfer_id("tcp")
    deadline = _deadline(ep)
    sock = _connect(ep)  # connect is excluded from the measured window
    verifier = payload.Verifier()
    buf = bytearray(_RECV_BUFFER_BYTES)
    view = memoryview(buf)
    meter = ClientMeter()
    try:
        send_msg(
            sock,
            {
                "protocol": PROTOCOL,
                "op": "download",
                "transfer_id": transfer_id,
                "size": size,
                "chunk": chunk,
            },
        )
        while verifier.received < size:
            n = sock.recv_into(view, min(len(view), size - verifier.received))
            if n == 0:
                break
            meter.first_byte()
            verifier.update(view[:n])
            _check_deadline(deadline, "tcp download")
    except OSError as exc:
        raise classify_os_error(exc, "tcp download") from exc
    finally:
        meter.stop()
        sock.close()
    _require_complete(verifier, size, "tcp download")
    stats = fetch_stats(ep, transfer_id)
    return _sample(
        transport="tcp",
        direction="download",
        size=size,
        chunk=chunk,
        meter=meter,
        recv_seconds=meter.wall_s,
        stats=stats,
    )


def tcp_upload(ep: Endpoint, size: int, chunk: int) -> dict[str, Any]:
    """Client -> server; the server reports the receiver view and the CRC."""
    transfer_id = new_transfer_id("tcp")
    deadline = _deadline(ep)
    sock = _connect(ep)  # connect is excluded from the measured window
    meter = ClientMeter()
    try:
        send_msg(
            sock,
            {
                "protocol": PROTOCOL,
                "op": "upload",
                "transfer_id": transfer_id,
                "size": size,
                "chunk": chunk,
            },
        )
        meter.first_byte()
        for chunk_view in payload.iter_chunks(0, size, chunk):
            sock.sendall(chunk_view)
            _check_deadline(deadline, "tcp upload")
        sock.shutdown(socket.SHUT_WR)
        reply = recv_msg(sock)
    except OSError as exc:
        raise classify_os_error(exc, "tcp upload") from exc
    except ProtocolError as exc:
        raise TransferError(FailureType.PROTOCOL_ERROR, str(exc)) from exc
    finally:
        meter.stop()
        sock.close()
    stats = _check_upload_reply(reply, size, "tcp upload")
    return _sample(
        transport="tcp",
        direction="upload",
        size=size,
        chunk=chunk,
        meter=meter,
        recv_seconds=_number(stats.get("seconds")) or meter.transfer_s,
        stats=stats,
    )


# ----------------------------------------------------------------- gRPC
def _grpc_channel(ep: Endpoint, limit: int) -> grpc.Channel:
    """A channel to the server's gRPC port; typed failure when either side lacks gRPC."""
    require_grpc("grpc client")
    if ep.grpc_port <= 0:
        raise TransferError(
            FailureType.GRPC_UNAVAILABLE, "server does not serve gRPC (port 0 in hello)"
        )
    return grpc.insecure_channel(
        grpc_target(ep.host, ep.grpc_port),
        options=[
            ("grpc.max_receive_message_length", limit),
            ("grpc.max_send_message_length", limit),
        ],
    )


def _wait_ready(channel: grpc.Channel, ep: Endpoint) -> None:
    """Connect the channel *before* the clock starts (gRPC connects lazily).

    Keeps gRPC comparable with raw TCP and HTTP, whose connections are also
    established outside the measured window; in the fabric the channel is
    long-lived anyway.
    """
    try:
        grpc.channel_ready_future(channel).result(timeout=ep.connect_timeout_s)
    except grpc.FutureTimeoutError as exc:
        channel.close()
        raise TransferError(
            FailureType.SERVER_UNREACHABLE,
            f"gRPC channel to {grpc_target(ep.host, ep.grpc_port)} not ready in "
            f"{ep.connect_timeout_s:g} s",
        ) from exc


def _grpc_error(exc: grpc.RpcError, doing: str) -> TransferError:
    # A failed call raises an object that is both RpcError and Call; the type
    # stubs model the two as unrelated, so read the status through Any.
    call: Any = exc
    code = call.code() if callable(getattr(call, "code", None)) else None
    details = call.details() if callable(getattr(call, "details", None)) else str(exc)
    if code in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
        failure = (
            FailureType.TIMEOUT
            if code is grpc.StatusCode.DEADLINE_EXCEEDED
            else FailureType.SERVER_UNREACHABLE
        )
    else:
        failure = FailureType.TRANSPORT_ERROR
    return TransferError(failure, f"{doing}: {code}: {details}")


def grpc_download(
    ep: Endpoint, size: int, chunk: int, *, limit: int | None = None
) -> dict[str, Any]:
    """Server-streaming ``bytes`` messages of ``chunk`` bytes."""
    transfer_id = new_transfer_id("grpc")
    message_limit = grpc_limit(chunk) if limit is None else limit
    request = encode_json({"transfer_id": transfer_id, "size": size, "chunk": chunk})
    verifier = payload.Verifier()
    channel = _grpc_channel(ep, message_limit)
    _wait_ready(channel, ep)
    meter = ClientMeter()
    try:
        call = channel.unary_stream(
            GRPC_DOWNLOAD,
            request_serializer=identity_bytes,
            response_deserializer=identity_bytes,
        )
        try:
            for message in call(request, timeout=ep.transfer_timeout_s):
                meter.first_byte()
                verifier.update(message)
        except grpc.RpcError as exc:
            raise _grpc_error(exc, "grpc download") from exc
    finally:
        meter.stop()
        channel.close()
    _require_complete(verifier, size, "grpc download")
    stats = fetch_stats(ep, transfer_id)
    return _sample(
        transport="grpc",
        direction="download",
        size=size,
        chunk=chunk,
        meter=meter,
        recv_seconds=meter.wall_s,
        stats=stats,
        extra={"grpc_message_limit_bytes": message_limit},
    )


def grpc_upload(
    ep: Endpoint, size: int, chunk: int, *, limit: int | None = None
) -> dict[str, Any]:
    """Client-streaming ``bytes`` messages; the server verifies and reports."""
    transfer_id = new_transfer_id("grpc")
    message_limit = grpc_limit(chunk) if limit is None else limit
    channel = _grpc_channel(ep, message_limit)
    _wait_ready(channel, ep)
    meter = ClientMeter()

    def messages() -> Iterator[bytes]:
        yield encode_json({"transfer_id": transfer_id, "size": size, "chunk": chunk})
        meter.first_byte()
        for view in payload.iter_chunks(0, size, chunk):
            yield bytes(view)

    try:
        call = channel.stream_unary(
            GRPC_UPLOAD,
            request_serializer=identity_bytes,
            response_deserializer=identity_bytes,
        )
        try:
            raw = call(messages(), timeout=ep.transfer_timeout_s)
        except grpc.RpcError as exc:
            raise _grpc_error(exc, "grpc upload") from exc
    finally:
        meter.stop()
        channel.close()
    try:
        reply = decode_json(bytes(raw))
    except ProtocolError as exc:
        raise TransferError(FailureType.PROTOCOL_ERROR, str(exc)) from exc
    stats = _check_upload_reply(reply, size, "grpc upload")
    return _sample(
        transport="grpc",
        direction="upload",
        size=size,
        chunk=chunk,
        meter=meter,
        recv_seconds=_number(stats.get("seconds")) or meter.transfer_s,
        stats=stats,
        extra={"grpc_message_limit_bytes": message_limit},
    )


# ----------------------------------------------------------------- HTTP
def _http_connection(ep: Endpoint) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(ep.host, ep.http_port, timeout=ep.io_timeout_s)


def _http_part(
    conn: http.client.HTTPConnection,
    transfer_id: str,
    size: int,
    chunk: int,
    offset: int,
    length: int,
    *,
    ranged: bool,
    deadline: float,
    meter: ClientMeter,
) -> payload.Verifier:
    """Fetch one (possibly ranged) part on an already-connected connection.

    Verifies the part against the payload; raises :class:`TransferError`.
    """
    verifier = payload.Verifier(offset)
    buf = bytearray(min(_RECV_BUFFER_BYTES, max(chunk, 4096)))
    view = memoryview(buf)
    headers = {"Range": f"bytes={offset}-{offset + length - 1}"} if ranged else {}
    url = f"{HTTP_BLOB_PATH}?size={size}&chunk={chunk}&transfer={transfer_id}"
    try:
        conn.request("GET", url, headers=headers)
        response = conn.getresponse()
        expected = 206 if ranged else 200
        if response.status != expected:
            body = response.read(512)
            raise TransferError(
                FailureType.SERVER_ERROR,
                f"http {response.status} {response.reason}: {body!r}",
            )
        while verifier.received < length:
            n = response.readinto(view)
            if n == 0:
                break
            meter.first_byte()
            verifier.update(view[:n])
            _check_deadline(deadline, "http download")
        response.read()
    except OSError as exc:
        raise classify_os_error(exc, "http download") from exc
    except http.client.HTTPException as exc:
        raise TransferError(FailureType.PROTOCOL_ERROR, f"http: {exc}") from exc
    if verifier.received != length or not verifier.matches(length):
        raise TransferError(
            FailureType.CHECKSUM_MISMATCH
            if verifier.received == length
            else FailureType.SHORT_TRANSFER,
            f"http part [{offset}, {offset + length}) failed verification "
            f"({verifier.received} of {length} bytes)",
        )
    return verifier


def http_download(
    ep: Endpoint, size: int, chunk: int, *, parts: int = 1
) -> dict[str, Any]:
    """``GET /blob`` - one plain request (``parts=1``) or N parallel ``Range`` parts.

    Every connection is established before the clock starts, so the parallel case
    measures transfer, not thread and TCP handshake startup.
    """
    transfer_id = new_transfer_id("http")
    deadline = _deadline(ep)
    ranged = parts > 1
    boundaries = range_parts(size, parts) if ranged else [(0, size)]
    conns = [_http_connection(ep) for _ in boundaries]
    errors: list[TransferError] = []
    lock = threading.Lock()

    def worker(
        conn: http.client.HTTPConnection, offset: int, length: int, meter: ClientMeter
    ) -> None:
        try:
            _http_part(
                conn, transfer_id, size, chunk, offset, length,
                ranged=ranged, deadline=deadline, meter=meter,
            )  # fmt: skip
        except TransferError as exc:
            with lock:
                errors.append(exc)

    try:
        for conn in conns:
            try:
                conn.connect()
            except OSError as exc:
                raise classify_os_error(exc, "http connect") from exc
        meter = ClientMeter()
        if not ranged:
            worker(conns[0], 0, size, meter)
        else:
            threads = [
                threading.Thread(
                    target=worker,
                    args=(conn, offset, length, meter),
                    name="s3-http-part",
                    daemon=True,
                )
                for conn, (offset, length) in zip(conns, boundaries, strict=True)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=ep.transfer_timeout_s + ep.io_timeout_s)
            alive = [t for t in threads if t.is_alive()]
            if alive:
                raise TransferError(
                    FailureType.TIMEOUT,
                    f"http range: {len(alive)} of {len(threads)} parts did not finish",
                )
        meter.stop()
    finally:
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.close()
    if errors:
        raise errors[0]
    stats = fetch_stats(ep, transfer_id, parts=len(boundaries))
    return _sample(
        transport="http",
        direction="download",
        size=size,
        chunk=chunk,
        meter=meter,
        recv_seconds=meter.wall_s,
        stats=stats,
        parts=len(boundaries),
        extra={"ranged": ranged},
    )


def range_parts(size: int, parts: int) -> list[tuple[int, int]]:
    """Split ``size`` into ``parts`` contiguous ``(offset, length)`` ranges.

    The first ``size % parts`` ranges get one extra byte, so the parts differ by
    at most one byte and always cover exactly ``size``.
    """
    if parts < 1:
        raise ValueError("parts must be >= 1")
    parts = min(parts, size)
    base, remainder = divmod(size, parts)
    out: list[tuple[int, int]] = []
    offset = 0
    for index in range(parts):
        length = base + (1 if index < remainder else 0)
        out.append((offset, length))
        offset += length
    return out
