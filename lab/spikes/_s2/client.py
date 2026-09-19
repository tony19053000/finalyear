"""S2 client measurements: plain functions, typed failures, no CLI coupling.

Every probe dials **out** to the peer (ADR-0002): the client never binds a
listening socket, so a machine running only ``s2_crossos.py client`` needs no
inbound firewall rule at all - which is exactly what the outbound-only case
verifies.

* :func:`http_ping` / :func:`http_blob` - stdlib ``http.client``: connect time,
  time to first byte, and throughput of a large-ish streamed body.
* :func:`grpc_unary` / :func:`grpc_download` - channel-ready time, unary round
  trip, server-streaming throughput.
* :func:`grpc_session` - the long-lived bidirectional stream: messages both
  ways for ``hold_s``, then ``idle_s`` of complete silence, then one more
  round trip. ``survived`` is 1 only when that last round trip works, which is
  what tells us whether a firewall/NAT drops idle HTTP/2 connections.

Timeouts are mandatory on every call; failures raise
:class:`~spikes._s2.protocol.ProbeError` with a diagnosis and a fix hint.
"""

from __future__ import annotations

import contextlib
import http.client
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import grpc

from spikes._s2.protocol import (
    CHUNK_BYTES,
    METHOD_DOWNLOAD,
    METHOD_ECHO,
    METHOD_SESSION,
    PROTOCOL,
    FailureType,
    Message,
    ProbeError,
    ProtocolError,
    channel_options,
    classify_grpc_error,
    classify_os_error,
    decode,
    encode,
)

type Sample = dict[str, Any]
_JOIN_S: Final = 5.0
#: Extra time added to a session's gRPC deadline over hold + idle.
_SESSION_MARGIN_S: Final = 15.0


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    http_port: int
    grpc_port: int
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 20.0

    @property
    def grpc_target(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.grpc_port}"


def mbps(num_bytes: int, seconds: float) -> float:
    if seconds <= 0:
        raise ProbeError(
            FailureType.MEASUREMENT_INVALID, "transfer took no measurable time"
        )
    return num_bytes * 8 / 1e6 / seconds


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def _rpc_error(exc: grpc.RpcError, doing: str, *, connecting: bool) -> ProbeError:
    code = getattr(exc, "code", lambda: None)()
    call: Any = exc
    details = getattr(call, "details", lambda: "")() or ""
    debug = getattr(call, "debug_error_string", lambda: "")() or ""
    name = getattr(code, "name", None) or str(code)
    return classify_grpc_error(
        str(name), str(details), str(debug), doing, connecting=connecting
    )


# ---------------------------------------------------------------------- HTTP
def _http_connect(ep: Endpoint) -> tuple[http.client.HTTPConnection, float]:
    conn = http.client.HTTPConnection(
        ep.host, ep.http_port, timeout=ep.connect_timeout_s
    )
    start = time.perf_counter()
    try:
        conn.connect()
    except OSError as exc:
        conn.close()
        raise classify_os_error(
            exc, f"HTTP connect {ep.host}:{ep.http_port}", connecting=True
        ) from exc
    elapsed = time.perf_counter() - start
    if conn.sock is not None:
        conn.sock.settimeout(ep.io_timeout_s)
    return conn, elapsed


def http_ping(ep: Endpoint) -> Sample:
    """``GET /ping``: connect, time to first byte, total, plus peer identity."""
    conn, connect_s = _http_connect(ep)
    try:
        start = time.perf_counter()
        conn.request("GET", "/ping", headers={"Accept": "application/json"})
        response = conn.getresponse()
        ttfb_s = time.perf_counter() - start
        body = response.read()
        total_s = time.perf_counter() - start
        if response.status != 200:
            raise ProbeError(
                FailureType.SERVER_ERROR,
                f"HTTP /ping returned {response.status} {response.reason}",
                detail=body[:500].decode("utf-8", "replace"),
            )
        identity = decode(body)
    except (OSError, http.client.HTTPException) as exc:
        raise classify_os_error(exc, "HTTP /ping", connecting=False) from exc
    except ProtocolError as exc:
        raise ProbeError(FailureType.PROTOCOL_ERROR, f"HTTP /ping: {exc}") from exc
    finally:
        conn.close()
    if identity.get("protocol") != PROTOCOL:
        raise ProbeError(
            FailureType.PROTOCOL_ERROR,
            f"peer speaks protocol {identity.get('protocol')!r}",
        )
    return {
        "connect_ms": _ms(connect_s),
        "ttfb_ms": _ms(ttfb_s),
        "total_ms": _ms(total_s),
        "peer_identity": identity,
    }


def http_blob(ep: Endpoint, size: int) -> Sample:
    """``GET /blob?bytes=N``: time to first byte and receive throughput."""
    conn, connect_s = _http_connect(ep)
    try:
        start = time.perf_counter()
        conn.request("GET", f"/blob?bytes={size}")
        response = conn.getresponse()
        ttfb_s = time.perf_counter() - start
        if response.status != 200:
            raise ProbeError(
                FailureType.SERVER_ERROR,
                f"HTTP /blob returned {response.status} {response.reason}",
            )
        received = 0
        body_start = time.perf_counter()
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
        elapsed = time.perf_counter() - body_start
    except (OSError, http.client.HTTPException) as exc:
        raise classify_os_error(exc, "HTTP /blob", connecting=False) from exc
    finally:
        conn.close()
    if received != size:
        raise ProbeError(
            FailureType.STREAM_DROPPED,
            f"HTTP /blob: got {received} of {size} bytes before the body ended",
        )
    return {
        "connect_ms": _ms(connect_s),
        "ttfb_ms": _ms(ttfb_s),
        "mbps": mbps(received, elapsed),
        "bytes": received,
    }


# ---------------------------------------------------------------------- gRPC
def open_channel(ep: Endpoint, keepalive: str) -> grpc.Channel:
    return grpc.insecure_channel(ep.grpc_target, options=channel_options(keepalive))


def _wait_ready(channel: grpc.Channel, ep: Endpoint, doing: str) -> float:
    start = time.perf_counter()
    try:
        grpc.channel_ready_future(channel).result(timeout=ep.connect_timeout_s)
    except grpc.FutureTimeoutError as exc:
        raise ProbeError(
            FailureType.CONNECT_TIMEOUT,
            f"{doing}: no usable gRPC connection to {ep.grpc_target} within "
            f"{ep.connect_timeout_s:g} s",
            detail=str(exc),
        ) from exc
    except grpc.RpcError as exc:
        raise _rpc_error(exc, doing, connecting=True) from exc
    return time.perf_counter() - start


def grpc_unary(ep: Endpoint, keepalive: str = "on", *, seq: int = 0) -> Sample:
    """``Echo``: time until the channel is usable, then one round trip."""
    channel = open_channel(ep, keepalive)
    try:
        ready_s = _wait_ready(channel, ep, "gRPC connect")
        call: grpc.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(
            METHOD_ECHO
        )
        start = time.perf_counter()
        try:
            reply = decode(
                call(encode({"op": "echo", "seq": seq}), timeout=ep.io_timeout_s)
            )
        except grpc.RpcError as exc:
            raise _rpc_error(exc, "gRPC Echo", connecting=False) from exc
        except ProtocolError as exc:
            raise ProbeError(FailureType.PROTOCOL_ERROR, f"gRPC Echo: {exc}") from exc
        rtt_s = time.perf_counter() - start
    finally:
        channel.close()
    if reply.get("protocol") != PROTOCOL:
        raise ProbeError(
            FailureType.PROTOCOL_ERROR,
            f"peer speaks protocol {reply.get('protocol')!r}",
        )
    return {
        "channel_ready_ms": _ms(ready_s),
        "rtt_ms": _ms(rtt_s),
        "peer_identity": reply,
    }


def grpc_download(ep: Endpoint, size: int, keepalive: str = "on") -> Sample:
    """``Download``: server-streaming time to first byte and throughput."""
    channel = open_channel(ep, keepalive)
    try:
        ready_s = _wait_ready(channel, ep, "gRPC connect")
        call: grpc.UnaryStreamMultiCallable[bytes, bytes] = channel.unary_stream(
            METHOD_DOWNLOAD
        )
        start = time.perf_counter()
        received = 0
        ttfb_s: float | None = None
        try:
            stream = call(
                encode({"op": "download", "bytes": size}), timeout=ep.io_timeout_s
            )
            for chunk in stream:
                if ttfb_s is None:
                    ttfb_s = time.perf_counter() - start
                received += len(chunk)
        except grpc.RpcError as exc:
            raise _rpc_error(exc, "gRPC Download", connecting=False) from exc
        elapsed = time.perf_counter() - start
    finally:
        channel.close()
    if received != size:
        raise ProbeError(
            FailureType.STREAM_DROPPED,
            f"gRPC Download: got {received} of {size} bytes before the stream ended",
        )
    return {
        "channel_ready_ms": _ms(ready_s),
        "ttfb_ms": _ms(ttfb_s if ttfb_s is not None else elapsed),
        "mbps": mbps(received, elapsed),
        "bytes": received,
    }


# ------------------------------------------------------- long-lived session
@dataclass(slots=True)
class SessionCounters:
    msgs_up: int = 0
    msgs_down: int = 0
    beats: int = 0
    pongs: dict[int, float] = field(default_factory=dict)


class _SessionCall:
    """Drives one bidirectional stream: an outbox the request iterator drains,
    and a reader thread that decodes responses and wakes waiters."""

    def __init__(self, channel: grpc.Channel, budget_s: float) -> None:
        self.outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self.counters = SessionCounters()
        self.error: ProbeError | None = None
        self.hello: Message | None = None
        self.acks: dict[str, float] = {}
        self._cond = threading.Condition()
        self._done = threading.Event()
        self.started = time.perf_counter()
        method: grpc.StreamStreamMultiCallable[bytes, bytes] = channel.stream_stream(
            METHOD_SESSION
        )
        self.responses = method(self._requests(), timeout=budget_s)
        self.reader = threading.Thread(
            target=self._read, name="s2-session-client", daemon=True
        )
        self.reader.start()

    def _requests(self) -> Iterator[bytes]:
        while True:
            item = self.outbox.get()
            if item is None:
                return
            yield item

    def _read(self) -> None:
        try:
            for body in self.responses:
                msg = decode(body)
                with self._cond:
                    self.counters.msgs_down += 1
                    op = msg.get("op")
                    if op == "pong":
                        seq = msg.get("seq")
                        if isinstance(seq, int):
                            self.counters.pongs[seq] = time.perf_counter()
                    elif op == "beat":
                        self.counters.beats += 1
                    else:
                        if op == "hello_ack":
                            self.hello = msg
                        self.acks[str(op)] = time.perf_counter()
                    self._cond.notify_all()
        except grpc.RpcError as exc:
            self._fail(_rpc_error(exc, "gRPC Session", connecting=False))
        except ProtocolError as exc:
            self._fail(ProbeError(FailureType.PROTOCOL_ERROR, f"gRPC Session: {exc}"))
        finally:
            self._done.set()
            with self._cond:
                self._cond.notify_all()

    def _fail(self, error: ProbeError) -> None:
        with self._cond:
            if self.error is None:
                self.error = error

    @property
    def alive_s(self) -> float:
        return time.perf_counter() - self.started

    def send(self, msg: Message) -> None:
        self._raise_if_failed()
        try:
            self.outbox.put(encode(msg), timeout=5.0)
        except queue.Full as exc:
            raise ProbeError(
                FailureType.MEASUREMENT_INVALID,
                "session outbox full (peer not reading)",
            ) from exc
        self.counters.msgs_up += 1

    def _raise_if_failed(self) -> None:
        with self._cond:
            error = self.error
        if error is not None:
            raise error

    def wait_pong(self, seq: int, timeout_s: float) -> float:
        """Round-trip seconds for ``seq``; raises when the stream died first."""
        sent = time.perf_counter()
        with self._cond:
            ok = self._cond.wait_for(
                lambda: (
                    seq in self.counters.pongs
                    or self.error is not None
                    or self._done.is_set()
                ),
                timeout=timeout_s,
            )
            error, arrived = self.error, self.counters.pongs.get(seq)
        if error is not None:
            raise error
        if arrived is None:
            raise ProbeError(
                FailureType.STREAM_DROPPED
                if self._done.is_set()
                else FailureType.TIMEOUT,
                f"no reply to session ping {seq} within {timeout_s:g} s"
                + ("" if ok else " (wait timed out)"),
            )
        return arrived - sent

    def wait_ack(self, op: str, timeout_s: float) -> Message:
        with self._cond:
            self._cond.wait_for(
                lambda: (
                    op in self.acks or self.error is not None or self._done.is_set()
                ),
                timeout=timeout_s,
            )
            error, seen = self.error, op in self.acks
        if error is not None:
            raise error
        if not seen:
            raise ProbeError(
                FailureType.STREAM_DROPPED
                if self._done.is_set()
                else FailureType.TIMEOUT,
                f"no '{op}' acknowledgement within {timeout_s:g} s",
            )
        return self.hello or {}

    def close(self) -> None:
        """Half-close, cancel whatever is left, and join the reader thread."""
        with contextlib.suppress(queue.Full):
            self.outbox.put_nowait(None)
        self._done.wait(timeout=2.0)
        with contextlib.suppress(grpc.RpcError, ValueError):
            self.responses.cancel()
        self.reader.join(timeout=_JOIN_S)


def grpc_session(
    ep: Endpoint,
    *,
    keepalive: str = "on",
    hold_s: float = 5.0,
    ping_interval_s: float = 1.0,
    idle_s: float = 30.0,
) -> Sample:
    """One long-lived bidi session: active phase, idle phase, final round trip.

    Returns ``survived`` 1/0 plus how long the stream stayed usable. A stream
    that dies during the idle phase is *the* result this spike is after, so it
    is returned as a sample (``survived`` 0) with the diagnosis attached, not
    raised - unless the stream never got going at all.
    """
    channel = open_channel(ep, keepalive)
    call: _SessionCall | None = None
    budget = hold_s + idle_s + _SESSION_MARGIN_S
    try:
        ready_s = _wait_ready(channel, ep, "gRPC connect")
        call = _SessionCall(channel, budget)
        call.send(
            {"op": "hello", "protocol": PROTOCOL, "beat_interval_s": ping_interval_s}
        )
        call.wait_ack("hello_ack", ep.io_timeout_s)
        sample: Sample = {
            "channel_ready_ms": _ms(ready_s),
            "keepalive": keepalive,
            "hold_s": hold_s,
            "idle_s": idle_s,
        }
        seq = 0
        active_until = time.perf_counter() + hold_s
        rtts: list[float] = []
        while time.perf_counter() < active_until:
            seq += 1
            call.send({"op": "ping", "seq": seq})
            rtts.append(call.wait_pong(seq, ep.io_timeout_s))
            time.sleep(
                min(ping_interval_s, max(0.0, active_until - time.perf_counter()))
            )
        sample["active_rtt_mean_ms"] = _ms(sum(rtts) / len(rtts)) if rtts else None
        sample["pings_active"] = len(rtts)
        call.send({"op": "go_quiet", "seconds": idle_s})
        call.wait_ack("quiet_ack", ep.io_timeout_s)
        idle_start = time.perf_counter()
        time.sleep(idle_s)
        seq += 1
        try:
            call.send({"op": "ping", "seq": seq})
            final_rtt = call.wait_pong(seq, ep.io_timeout_s)
        except ProbeError as exc:
            sample.update(
                survived=0,
                alive_s=call.alive_s,
                idle_survived_s=time.perf_counter() - idle_start,
                final_rtt_ms=None,
                failure=exc.to_json(),
            )
        else:
            sample.update(
                survived=1,
                alive_s=call.alive_s,
                idle_survived_s=idle_s,
                final_rtt_ms=_ms(final_rtt),
                failure=None,
            )
        sample.update(
            msgs_up=call.counters.msgs_up,
            msgs_down=call.counters.msgs_down,
            beats=call.counters.beats,
        )
        return sample
    finally:
        if call is not None:
            call.close()
        channel.close()
