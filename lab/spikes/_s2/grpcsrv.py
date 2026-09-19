"""The gRPC half of the S2 server (plaintext, generic byte handlers).

Three methods, registered without generated stubs (see
:mod:`spikes._s2.protocol`): ``Echo`` (unary), ``Download`` (server-streaming)
and ``Session`` - the ADR-0002 shape, a long-lived bidirectional stream the
*client* opens and both sides write to.

``Session`` runs one reader thread per RPC that drains the request iterator
into a bounded queue, while the response generator drains that queue and
interleaves the server's own periodic ``beat`` messages. A ``go_quiet`` request
stops the beats for N seconds, which is how the idle test measures whether a
firewall or NAT drops an idle HTTP/2 connection. Every session is bounded by
:data:`~spikes._s2.protocol.MAX_HOLD_S` and ends when the peer half-closes,
cancels, or the server stops.
"""

from __future__ import annotations

import contextlib
import itertools
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent import futures
from typing import Any, Final

import grpc

from spikes._s1.protocol import is_allowed_peer
from spikes._s2.env import local_facts
from spikes._s2.protocol import (
    CHUNK_BYTES,
    MAX_HOLD_S,
    METHOD_DOWNLOAD,
    METHOD_ECHO,
    METHOD_SESSION,
    PROTOCOL,
    SERVICE,
    ProtocolError,
    blob_size,
    decode,
    encode,
    server_options,
)

_PEER_RE: Final = re.compile(
    r"^(?:ipv4|ipv6):(?:\[(?P<v6>[^\]]+)\]|(?P<v4>[^:]+)):\d+$"
)
_QUEUE_MAX: Final = 256
_TICK_S: Final = 0.05
_PATTERN: Final = b"\x5a"
MIN_BEAT_S: Final = 0.05
MAX_BEAT_S: Final = 60.0
DEFAULT_BEAT_S: Final = 1.0
MAX_QUIET_S: Final = MAX_HOLD_S


def peer_address(peer: str) -> str | None:
    """``"ipv4:10.0.0.5:51234"`` / ``"ipv6:[::1]:51234"`` -> the bare address."""
    match = _PEER_RE.match(peer.strip())
    if match is None:
        return None
    return match.group("v6") or match.group("v4")


def _clamp(value: object, lo: float, hi: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(lo, min(hi, float(value)))


class S2GrpcServer:
    """Plaintext gRPC server exposing the three S2 probe methods."""

    def __init__(
        self,
        bind: str,
        port: int,
        *,
        name: str,
        log: Callable[[str], None],
        touch: Callable[[], None],
        note_error: Callable[[str], None],
        max_workers: int = 8,
    ) -> None:
        self.bind = bind
        self.name = name
        self.log = log
        self.touch = touch
        self.note_error = note_error
        self.stopping = threading.Event()
        self._started = False
        self._stopped = False
        self.counters: dict[str, int] = {
            "echo": 0,
            "download": 0,
            "session": 0,
            "session_ended": 0,
            "refused_peer": 0,
        }
        self._pool = futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="s2-grpc"
        )
        self._server = grpc.server(self._pool, options=server_options())
        self._server.add_generic_rpc_handlers((self._handlers(),))
        try:
            self.port = self._bind(bind, port)
        except OSError:
            self._pool.shutdown(wait=False, cancel_futures=True)
            raise

    # ----------------------------------------------------------- lifecycle
    def _bind(self, bind: str, port: int) -> int:
        """Bind the gRPC listener, reporting a failure as ``OSError``.

        grpcio raises ``RuntimeError`` (not ``OSError``) when the address is
        taken and older versions returned 0 instead; both are translated, so
        callers - and the CLI's ``serve`` - can handle one exception type and
        print "cannot listen" rather than a traceback.
        """
        host = f"[{bind}]" if ":" in bind else bind
        try:
            bound = self._server.add_insecure_port(f"{host}:{port}")
        except RuntimeError as exc:
            raise OSError(f"cannot listen on {host}:{port} (gRPC): {exc}") from exc
        if bound == 0:
            raise OSError(f"cannot listen on {host}:{port} (gRPC)")
        return int(bound)

    def start(self) -> None:
        if not self._started:
            self._server.start()
            self._started = True

    def stop(self, grace_s: float = 1.0) -> None:
        """Stop accepting, cancel live sessions, wait for the workers.

        gRPC frees the socket bound by ``add_insecure_port`` only when a
        **started** server is stopped: stopping (or dropping) a server that was
        never started leaves the port listening for the life of the process.
        So a never-started server is started here purely to be stopped again,
        which is what ``S2Server.open()``'s failure path needs. Idempotent.
        """
        self.stopping.set()
        if self._stopped:
            return
        self._stopped = True
        self.start()
        self._server.stop(grace_s).wait(timeout=grace_s + 5.0)
        self._pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------ handlers
    def _handlers(self) -> grpc.GenericRpcHandler:
        return grpc.method_handlers_generic_handler(
            SERVICE,
            {
                METHOD_ECHO.rsplit("/", 1)[-1]: grpc.unary_unary_rpc_method_handler(
                    self._echo
                ),
                METHOD_DOWNLOAD.rsplit("/", 1)[
                    -1
                ]: grpc.unary_stream_rpc_method_handler(self._download),
                METHOD_SESSION.rsplit("/", 1)[
                    -1
                ]: grpc.stream_stream_rpc_method_handler(self._session),
            },
        )

    def _check_peer(self, context: grpc.ServicerContext) -> None:
        """Abort the RPC unless the caller is a LAN peer (docs/LAB.md §6)."""
        self.touch()
        address = peer_address(context.peer())
        if address is None or not is_allowed_peer(address):
            self.counters["refused_peer"] += 1
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "LAN peers only (docs/LAB.md §6)"
            )

    def identity(self) -> dict[str, Any]:
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "hostname": socket.gethostname(),
            "name": self.name,
            **local_facts(),
        }

    def _echo(self, request: bytes, context: grpc.ServicerContext) -> bytes:
        self._check_peer(context)
        try:
            msg = decode(request)
        except ProtocolError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        self.counters["echo"] += 1
        return encode(
            {
                **self.identity(),
                "op": "echo_reply",
                "seq": msg.get("seq"),
                "peer": peer_address(context.peer()),
            }
        )

    def _download(
        self, request: bytes, context: grpc.ServicerContext
    ) -> Iterator[bytes]:
        self._check_peer(context)
        try:
            size = blob_size(decode(request).get("bytes"))
        except ProtocolError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        self.counters["download"] += 1
        chunk = _PATTERN * min(size, CHUNK_BYTES)
        left = size
        while left > 0 and not self.stopping.is_set() and context.is_active():
            piece = chunk if left >= len(chunk) else chunk[:left]
            yield piece
            left -= len(piece)
            self.touch()

    # ------------------------------------------------------------- session
    def _session(
        self, request_iterator: Iterator[bytes], context: grpc.ServicerContext
    ) -> Iterator[bytes]:
        self._check_peer(context)
        self.counters["session"] += 1
        state = _SessionState()
        done = threading.Event()
        context.add_callback(done.set)
        reader = threading.Thread(
            target=self._read_requests,
            args=(request_iterator, state, done),
            name="s2-session-reader",
            daemon=True,
        )
        reader.start()
        try:
            yield from self._write_responses(state, done, context)
        finally:
            done.set()
            state.replies.put(None)
            reader.join(timeout=5.0)
            self.counters["session_ended"] += 1
            self.log(f"session from {peer_address(context.peer())} ended")

    def _read_requests(
        self, requests: Iterator[bytes], state: _SessionState, done: threading.Event
    ) -> None:
        try:
            for body in requests:
                if done.is_set():
                    break
                self.touch()
                reply = state.handle(body, self.identity())
                if reply is not None:
                    state.offer(reply)
                if state.closed:
                    break
        except grpc.RpcError as exc:  # peer vanished / cancelled: normal here
            self.log(f"session read ended: {exc}")
        except ProtocolError as exc:
            self.note_error(f"session protocol error: {exc}")
        finally:
            state.replies.put(None)

    def _write_responses(
        self,
        state: _SessionState,
        done: threading.Event,
        context: grpc.ServicerContext,
    ) -> Iterator[bytes]:
        beats = itertools.count()
        started = time.monotonic()
        # The beat interval is negotiated by the client's ``hello``, which the
        # reader thread handles *after* this generator has started. The due
        # time is therefore recomputed from the current interval on every pass
        # (``last_beat + interval``) instead of being latched once here: with a
        # latched deadline the first beat would always be DEFAULT_BEAT_S away,
        # and a session shorter than that would carry no server-initiated
        # message at all - exactly the ADR-0002 property this spike measures.
        last_beat = started
        while not done.is_set() and not self.stopping.is_set() and context.is_active():
            if time.monotonic() - started > MAX_HOLD_S:
                self.log("session hit the server hold limit; closing")
                return
            try:
                item = state.replies.get(timeout=_TICK_S)
            except queue.Empty:
                item = b""
            if item is None:
                return
            if item:
                yield item
                self.touch()
            now = time.monotonic()
            if now - last_beat >= state.beat_interval_s:
                last_beat = now
                if now >= state.quiet_until:
                    yield encode({"op": "beat", "seq": next(beats)})
                    self.touch()


class _SessionState:
    """Per-session state: the reply queue, the beat rate and the quiet window."""

    def __init__(self) -> None:
        self.replies: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self.beat_interval_s = DEFAULT_BEAT_S
        self.quiet_until = 0.0
        self.closed = False

    def offer(self, body: bytes) -> None:
        """Queue a reply; drop it if the peer is not reading (bounded memory)."""
        with contextlib.suppress(queue.Full):
            self.replies.put_nowait(body)

    def handle(self, body: bytes, identity: dict[str, Any]) -> bytes | None:
        msg = decode(body)
        op = msg.get("op")
        if op == "hello":
            self.beat_interval_s = _clamp(
                msg.get("beat_interval_s"), MIN_BEAT_S, MAX_BEAT_S, DEFAULT_BEAT_S
            )
            return encode(
                {**identity, "op": "hello_ack", "beat_interval_s": self.beat_interval_s}
            )
        if op == "ping":
            return encode({"op": "pong", "seq": msg.get("seq")})
        if op == "go_quiet":
            seconds = _clamp(msg.get("seconds"), 0.0, MAX_QUIET_S, 0.0)
            self.quiet_until = time.monotonic() + seconds
            return encode({"op": "quiet_ack", "seconds": seconds})
        if op == "bye":
            self.closed = True
            return encode({"op": "bye_ack"})
        raise ProtocolError(f"unknown op {op!r}")
