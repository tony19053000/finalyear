"""S1 client measurements: plain functions, no CLI, typed errors.

* :func:`tcp_throughput` - ``upload`` (this host sends) or ``download`` (the
  server sends) over ``streams`` parallel TCP connections for ``seconds``.
  The **receiver view** (bytes counted where they arrive, first-to-last byte on
  the receiver's clock) is the primary number; the sender view (bytes accepted
  by the sender's kernel over its send window) is returned too, because at the
  end of a short run up to one socket buffer may still be in flight.
* :func:`udp_rtt` - ``count`` UDP echo pings at a fixed interval -> RTT
  mean/stdev/min/p95/max, jitter and loss % (see :func:`ping_summary`).

All streams of one repetition are connected first; each worker sends its
request header only after a common start signal, so no data is queued in
kernel buffers before the measured window begins.

Every blocking call has a timeout; failures raise :class:`MeasureError`.
"""

from __future__ import annotations

import contextlib
import secrets
import select
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

from spikes._harness.stats import mean_abs_delta, rfc3550_jitter, summarize
from spikes._s1.protocol import (
    MAGIC,
    MAX_PING_BYTES,
    MAX_STREAMS,
    MIN_PING_BYTES,
    PING,
    PROTOCOL,
    RECV_BUFFER_BYTES,
    SEND_CHUNK_BYTES,
    FailureType,
    MeasureError,
    Message,
    ProtocolError,
    classify_os_error,
    family_of,
    recv_msg,
    send_msg,
)

type Direction = Literal["upload", "download"]
DIRECTIONS: Final[tuple[Direction, Direction]] = ("upload", "download")
_JOIN_GRACE_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 10.0


def mbps(num_bytes: int, seconds: float) -> float:
    return num_bytes * 8 / 1e6 / seconds


def _connect(ep: Endpoint) -> socket.socket:
    try:
        sock = socket.create_connection(
            (ep.host, ep.port), timeout=ep.connect_timeout_s
        )
    except OSError as exc:
        raise classify_os_error(exc, f"connect {ep.host}:{ep.port}") from exc
    sock.settimeout(ep.io_timeout_s)
    return sock


def request(ep: Endpoint, msg: Message) -> Message:
    """One request/reply exchange on a fresh connection."""
    sock = _connect(ep)
    try:
        send_msg(sock, {"protocol": PROTOCOL, **msg})
        reply = recv_msg(sock)
    except OSError as exc:
        raise classify_os_error(exc, f"{msg.get('op')} request") from exc
    except ProtocolError as exc:
        raise MeasureError(FailureType.PROTOCOL_ERROR, str(exc)) from exc
    finally:
        sock.close()
    if reply.get("ok") is not True:
        raise MeasureError(FailureType.SERVER_ERROR, str(reply.get("error", reply)))
    return reply


def hello(ep: Endpoint) -> Message:
    """Server identity (``hostname``, ``os``) and the address it sees us as."""
    reply = request(ep, {"op": "hello"})
    if reply.get("protocol") != PROTOCOL or not isinstance(reply.get("hostname"), str):
        raise MeasureError(FailureType.PROTOCOL_ERROR, f"unexpected hello: {reply}")
    return reply


def local_address_towards(ep: Endpoint) -> str | None:
    """The local IP the OS routes to ``ep`` (no packet is sent)."""
    with socket.socket(family_of(ep.host), socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((ep.host, ep.port))
            return str(sock.getsockname()[0])
        except OSError:
            return None


def socket_buffer_defaults() -> dict[str, int]:
    """Initial kernel buffer sizes of a fresh TCP socket (autotuning may grow them)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return {
            "so_sndbuf_initial": sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
            "so_rcvbuf_initial": sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            "app_send_chunk": SEND_CHUNK_BYTES,
            "app_recv_buffer": RECV_BUFFER_BYTES,
        }


class _Stream:
    """One stream's outcome, filled in by its worker thread."""

    def __init__(self, sock: socket.socket, header: Message) -> None:
        self.sock = sock
        self.header = header
        self.bytes = 0
        self.t_a: float | None = None  # first send / first byte received
        self.t_b: float | None = None  # last send / last byte received
        self.error: MeasureError | None = None


def _upload_worker(st: _Stream, start: threading.Event, seconds: float) -> None:
    payload = secrets.token_bytes(SEND_CHUNK_BYTES)
    try:
        start.wait()
        send_msg(st.sock, st.header)
        st.t_a = time.perf_counter()
        deadline = st.t_a + seconds
        while time.perf_counter() < deadline:
            st.bytes += st.sock.send(payload)
        st.t_b = time.perf_counter()
        st.sock.shutdown(socket.SHUT_WR)
        while st.sock.recv(4096):  # server closes after counting everything
            pass
    except OSError as exc:
        st.error = classify_os_error(exc, "upload stream")


def _download_worker(st: _Stream, start: threading.Event, _seconds: float) -> None:
    buf = bytearray(RECV_BUFFER_BYTES)
    try:
        start.wait()
        send_msg(st.sock, st.header)
        while True:
            n = st.sock.recv_into(buf)
            if n == 0:
                break
            now = time.perf_counter()
            if st.t_a is None:
                st.t_a = now
            st.t_b = now
            st.bytes += n
    except OSError as exc:
        st.error = classify_os_error(exc, "download stream")


def _span(pairs: list[tuple[float | None, float | None]]) -> float:
    starts = [a for a, _ in pairs if a is not None]
    ends = [b for _, b in pairs if b is not None]
    if not starts or not ends:
        return 0.0
    return max(ends) - min(starts)


def _server_view(reply: Message, streams: int, kind: str) -> tuple[int, float]:
    """(bytes, seconds) from the server's per-stream stats."""
    stats: list[dict[str, Any]] = reply.get("streams", [])
    if len(stats) != streams:
        raise MeasureError(
            FailureType.PROTOCOL_ERROR, "server returned wrong stream count"
        )
    for s in stats:
        if s.get("error"):
            raise MeasureError(
                FailureType.CONNECTION_LOST, f"server-side stream error: {s['error']}"
            )
    total = sum(int(s.get("bytes", 0)) for s in stats)
    keys = ("t_first", "t_last") if kind == "receiver" else ("t_start", "t_end")
    return total, _span([(s.get(keys[0]), s.get(keys[1])) for s in stats])


def tcp_throughput(
    ep: Endpoint, *, direction: Direction, streams: int, seconds: float
) -> dict[str, Any]:
    """One repetition of a bulk TCP transfer. Returns a sample dict (Mbit/s)."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if not 1 <= streams <= MAX_STREAMS:
        raise ValueError(f"streams must be 1..{MAX_STREAMS}")
    test_id = secrets.token_hex(8)
    socks: list[socket.socket] = []
    states: list[_Stream] = []
    try:
        for i in range(streams):
            sock = _connect(ep)
            socks.append(sock)
            header: Message = {
                "protocol": PROTOCOL,
                "op": direction,
                "test_id": test_id,
                "stream": i,
            }
            if direction == "download":
                header["seconds"] = seconds
            states.append(_Stream(sock, header))
        start = threading.Event()
        worker = _upload_worker if direction == "upload" else _download_worker
        threads = [
            threading.Thread(
                target=worker,
                args=(st, start, seconds),
                name=f"s1-{direction}-{i}",
                daemon=True,
            )
            for i, st in enumerate(states)
        ]
        for t in threads:
            t.start()
        start.set()
        limit = seconds + 2 * ep.io_timeout_s + _JOIN_GRACE_S
        deadline = time.monotonic() + limit
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in threads):
            for s in socks:  # unblocks the stuck worker; it exits with an OSError
                with contextlib.suppress(OSError):
                    s.close()
            raise MeasureError(
                FailureType.TIMEOUT,
                f"{direction}: streams did not finish in {limit:g} s",
            )
    finally:
        for s in socks:
            with contextlib.suppress(OSError):
                s.close()
    for st in states:
        if st.error is not None:
            raise st.error
    reply = request(ep, {"op": "result", "test_id": test_id, "streams": streams})
    local_bytes = sum(st.bytes for st in states)
    local_span = _span([(st.t_a, st.t_b) for st in states])
    if direction == "upload":
        sent, sender_s = local_bytes, local_span
        received, receiver_s = _server_view(reply, streams, "receiver")
    else:
        received, receiver_s = local_bytes, local_span
        sent, sender_s = _server_view(reply, streams, "sender")
    if received <= 0 or receiver_s <= 0 or sender_s <= 0:
        raise MeasureError(
            FailureType.MEASUREMENT_INVALID,
            f"{direction}: nothing measurable (received={received} B, "
            f"receiver window={receiver_s:.3f} s)",
        )
    per_stream = [
        mbps(st.bytes, st.t_b - st.t_a)
        for st in states
        if st.t_a is not None and st.t_b is not None and st.t_b > st.t_a
    ]
    return {
        "receiver_mbps": mbps(received, receiver_s),
        "sender_mbps": mbps(sent, sender_s),
        "bytes_received": received,
        "bytes_sent": sent,
        "bytes_match": received == sent,
        "receiver_s": receiver_s,
        "sender_s": sender_s,
        "streams": streams,
        "client_per_stream_mbps": per_stream,
    }


def udp_rtt(
    ep: Endpoint,
    *,
    count: int,
    interval_s: float,
    size: int = 64,
    reply_timeout_s: float = 1.0,
) -> dict[str, Any]:
    """One repetition of ``count`` UDP echo pings. Returns a sample dict (ms, %)."""
    if count < 2:
        raise ValueError("count must be >= 2")
    if not MIN_PING_BYTES <= size <= MAX_PING_BYTES:
        raise ValueError(f"size must be {MIN_PING_BYTES}..{MAX_PING_BYTES}")
    token = secrets.randbits(64)
    padding = b"\x00" * (size - PING.size)
    sent_at: dict[int, int] = {}
    rtts: dict[int, float] = {}
    icmp_errors = 0
    interval_ns = int(interval_s * 1e9)
    sock = socket.socket(family_of(ep.host), socket.SOCK_DGRAM)
    try:
        sock.connect((ep.host, ep.port))  # only the server's replies are delivered
        sock.setblocking(False)
        seq = 0
        next_send = time.perf_counter_ns()
        deadline: int | None = None
        while True:
            now = time.perf_counter_ns()
            if seq < count and now >= next_send:
                try:
                    sock.send(PING.pack(MAGIC, seq, token, 0) + padding)
                    sent_at[seq] = time.perf_counter_ns()
                except (ConnectionRefusedError, ConnectionResetError):
                    icmp_errors += 1
                except OSError as exc:
                    raise classify_os_error(exc, "udp send") from exc
                seq += 1
                next_send += interval_ns
                continue
            if seq >= count and deadline is None:
                deadline = now + int(reply_timeout_s * 1e9)
            if deadline is not None and (now >= deadline or len(rtts) >= len(sent_at)):
                break
            wait_ns = (next_send - now) if seq < count else (deadline or now) - now
            readable, _, _ = select.select([sock], [], [], max(0.0, wait_ns / 1e9))
            if not readable:
                continue
            while True:
                try:
                    data = sock.recv(MAX_PING_BYTES + 1)
                except (BlockingIOError, InterruptedError):
                    break
                except (ConnectionRefusedError, ConnectionResetError):
                    icmp_errors += 1  # ICMP port unreachable: server not listening
                    break
                arrived = time.perf_counter_ns()
                if len(data) < PING.size:
                    continue
                magic, rseq, rtoken, _ = PING.unpack_from(data)
                if magic != MAGIC or rtoken != token or rseq not in sent_at:
                    continue
                if rseq not in rtts:  # duplicates are ignored
                    rtts[rseq] = (arrived - sent_at[rseq]) / 1e6
    finally:
        sock.close()
    if not rtts:
        detail = f" ({icmp_errors} ICMP unreachable)" if icmp_errors else ""
        raise MeasureError(
            FailureType.NO_REPLY,
            f"no UDP echo replies from {ep.host}:{ep.port} out of {count}{detail} "
            "(server down, or UDP blocked by a firewall)",
        )
    sample = ping_summary(count, rtts)
    sample["icmp_errors"] = icmp_errors
    return sample


def ping_summary(count: int, rtts: dict[int, float]) -> dict[str, Any]:
    """Sample dict for ``count`` pings with replies ``rtts`` (seq -> RTT in ms).

    * ``jitter_ms``: mean ``|RTT(i) - RTT(i-1)|`` over consecutive replies in
      sequence order - RFC 3550's ``D(i-1, i)`` (for an echo, the transit
      difference is the RTT difference) averaged without the 1/16 smoothing.
    * ``jitter_rfc3550_ms``: the RFC 3550 §6.4.1 smoothed estimator's final value.
    * ``loss_pct``: pings without a reply (late replies count as lost).
    """
    if count < 1 or not rtts:
        raise ValueError("need count >= 1 and at least one reply")
    ordered = [rtts[s] for s in sorted(rtts)]
    summary = summarize(ordered)
    return {
        "rtt_mean_ms": summary.mean,
        "rtt_stdev_ms": summary.stdev if summary.stdev is not None else 0.0,
        "rtt_min_ms": summary.min,
        "rtt_median_ms": summary.median,
        "rtt_p95_ms": summary.p95,
        "rtt_max_ms": summary.max,
        "jitter_ms": mean_abs_delta(ordered) or 0.0,
        "jitter_rfc3550_ms": rfc3550_jitter(ordered) or 0.0,
        "loss_pct": 100.0 * (count - len(rtts)) / count,
        "sent": count,
        "received": len(rtts),
        "rtts_ms": [round(v, 4) for v in ordered],
    }
