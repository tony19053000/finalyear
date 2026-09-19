"""S1 measurement server: TCP bulk sink/source + UDP echo on one port number.

:class:`S1Server` is usable in-process (the tests) and from the CLI; the
client's loopback ceiling check runs it in a separate subprocess (see
``_s1/ceiling.py``) so that check measures two processes, not one GIL.
Safety rules (docs/LAB.md §6): LAN peers only,
bounded operation sizes, a connection cap, and an automatic stop after
``idle_s`` without traffic or ``duration_s`` in total. Every socket operation
has a timeout; :meth:`S1Server.stop` is thread-safe and :meth:`S1Server.serve`
always closes every socket and joins its handler threads before returning.

Admission: a new TCP connection is *pending* until its complete request header
has arrived. Pending connections are read non-blocking by the main loop (no
thread, at most one header buffer each) and are dropped after
``header_timeout_s``. When ``max_connections`` is reached, the oldest pending
connection is evicted to make room; only connections with a valid header get a
handler thread (*active*), and active ones are never evicted. So idle or
trickling peers cannot lock a real client out, and threads + memory stay
bounded by ``max_connections``.
"""

from __future__ import annotations

import contextlib
import os
import platform
import selectors
import socket
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from spikes._s1.protocol import (
    LENGTH_BYTES,
    MAGIC,
    MAX_PING_BYTES,
    MAX_SECONDS,
    MAX_STREAMS,
    MIN_PING_BYTES,
    MIN_SECONDS,
    PROTOCOL,
    RECV_BUFFER_BYTES,
    SEND_CHUNK_BYTES,
    Message,
    ProtocolError,
    body_length,
    decode_body,
    family_of,
    is_allowed_peer,
    send_msg,
    valid_test_id,
)

_MAX_RESULTS: Final = 256
_SELECT_S: Final = 0.2
_JOIN_S: Final = 5.0
_UDP_BURST: Final = 256
_ACCEPT_BURST: Final = 256
_BIND_RETRIES: Final = 20
_MAX_ERRORS: Final = 100
#: Windows: "A message sent on a datagram socket was larger than the buffer"
_WSAEMSGSIZE: Final = 10040
UPLOAD_LIMIT_ERROR: Final = "upload exceeded limit"

STOP_REQUESTED: Final = "stop requested"
STOP_IDLE: Final = "idle limit reached"
STOP_DURATION: Final = "duration limit reached"


@dataclass(frozen=True, slots=True)
class ServerLimits:
    duration_s: float = 3600.0
    idle_s: float = 600.0
    #: cap on *all* open TCP connections (pending + active); bounds threads/memory
    max_connections: int = 32
    #: a new connection must deliver its complete request header within this time
    header_timeout_s: float = 2.0
    io_timeout_s: float = 10.0
    result_wait_s: float = 5.0
    #: an ``upload`` is cut off this long after its first byte; ``None`` =
    #: ``MAX_SECONDS + io_timeout_s`` (the longest legitimate test plus slack)
    max_upload_s: float | None = None

    def upload_limit_s(self) -> float:
        if self.max_upload_s is None:
            return MAX_SECONDS + self.io_timeout_s
        return self.max_upload_s


@dataclass(slots=True)
class ServerCounters:
    tcp_accepted: int = 0
    tcp_refused_peer: int = 0
    #: dropped because the server was full: refused at accept (every slot held
    #: by an active measurement), or a pending connection evicted to make room
    tcp_refused_busy: int = 0
    #: the evicted subset of ``tcp_refused_busy``
    tcp_evicted_pending: int = 0
    tcp_header_timeouts: int = 0
    udp_echoed: int = 0
    udp_dropped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Pending:
    """A connection whose request header has not fully arrived yet."""

    peer: str
    deadline: float
    buf: bytearray = field(default_factory=bytearray)
    #: body length once the 4-byte prefix has been read
    body: int | None = None


class S1Server:
    def __init__(
        self,
        bind: str,
        port: int,
        limits: ServerLimits | None = None,
        log: Callable[[str], None] | None = None,
        name: str | None = None,
    ) -> None:
        self.bind = bind
        #: identity reported in ``hello`` (matrix row/column); default hostname
        self.name = name or socket.gethostname()
        self.requested_port = port
        self.port = port
        self.limits = limits or ServerLimits()
        self.counters = ServerCounters()
        self._log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._results_cond = threading.Condition(self._lock)
        self._results: OrderedDict[str, dict[int, Message]] = OrderedDict()
        #: every open TCP connection (pending + active), guarded by _lock
        self._conns: set[socket.socket] = set()
        #: pending connections, oldest first; only the main loop touches this
        self._pending: OrderedDict[socket.socket, _Pending] = OrderedDict()
        self._sel: selectors.BaseSelector | None = None
        self._threads: list[threading.Thread] = []
        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._last_activity = time.monotonic()
        self._payload = os.urandom(SEND_CHUNK_BYTES)
        self.stop_reason: str | None = None

    # ------------------------------------------------------------------ setup
    def open(self) -> int:
        """Bind TCP and UDP to ``bind:port`` (port 0 = pick a free one for both).

        Returns the port. Raises ``OSError`` (e.g. port in use).
        """
        family = family_of(self.bind)
        attempts = _BIND_RETRIES if self.requested_port == 0 else 1
        last: OSError | None = None
        for _ in range(attempts):
            tcp = socket.socket(family, socket.SOCK_STREAM)
            udp = socket.socket(family, socket.SOCK_DGRAM)
            try:
                if sys.platform == "win32":
                    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    udp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    # Lets a restarted server rebind while old sockets sit in TIME_WAIT.
                    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp.bind((self.bind, self.requested_port))
                port = int(tcp.getsockname()[1])
                udp.bind((self.bind, port))
                tcp.listen(64)
            except OSError as exc:
                tcp.close()
                udp.close()
                last = exc
                continue
            tcp.setblocking(False)
            udp.setblocking(False)
            self._tcp, self._udp, self.port = tcp, udp, port
            return port
        raise last if last is not None else OSError("bind failed")

    def stop(self) -> None:
        self._stop.set()

    def _note_error(self, text: str) -> None:
        with self._lock:
            if len(self.counters.errors) < _MAX_ERRORS:
                self.counters.errors.append(text)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def open_connections(self) -> int:
        """Number of open TCP connections (pending + active) right now."""
        with self._lock:
            return len(self._conns)

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    # -------------------------------------------------------------- main loop
    def serve(self) -> str:
        """Serve until stopped or a limit is hit; returns the stop reason."""
        if self._tcp is None or self._udp is None:
            self.open()
        if self._tcp is None or self._udp is None:  # pragma: no cover - open() raises
            raise RuntimeError("server sockets not open")
        started = time.monotonic()
        self._touch()
        sel = selectors.DefaultSelector()
        self._sel = sel
        try:
            sel.register(self._tcp, selectors.EVENT_READ, "tcp")
            sel.register(self._udp, selectors.EVENT_READ, "udp")
            while True:
                reason = self._check_limits(started)
                if reason is not None:
                    self.stop_reason = reason
                    break
                self._expire_pending()
                for key, _ in sel.select(timeout=self._select_timeout()):
                    if key.data == "tcp":
                        self._accept()
                    elif key.data == "udp":
                        self._echo_udp()
                    elif isinstance(key.fileobj, socket.socket):
                        self._read_pending(key.fileobj)
        finally:
            if self.stop_reason is None:
                self.stop_reason = "interrupted"
            self._pending.clear()
            self._sel = None
            sel.close()
            self._shutdown()
        return self.stop_reason

    def _check_limits(self, started: float) -> str | None:
        now = time.monotonic()
        if self._stop.is_set():
            return STOP_REQUESTED
        if now - started >= self.limits.duration_s:
            return STOP_DURATION
        with self._lock:
            active = len(self._conns)
        if active == 0 and now - self._last_activity >= self.limits.idle_s:
            return STOP_IDLE
        return None

    def _select_timeout(self) -> float:
        """Wake up in time for the oldest pending connection's header deadline."""
        if not self._pending:
            return _SELECT_S
        oldest = next(iter(self._pending.values()))
        return max(0.0, min(_SELECT_S, oldest.deadline - time.monotonic()))

    def start_thread(self) -> threading.Thread:
        """Run :meth:`serve` in a background thread (in-process use)."""
        if self._tcp is None:
            self.open()
        thread = threading.Thread(target=self.serve, name="s1-server", daemon=True)
        thread.start()
        return thread

    def _shutdown(self) -> None:
        self._stop.set()
        for sock in (self._tcp, self._udp):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        deadline = time.monotonic() + _JOIN_S
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------- UDP
    def _echo_udp(self) -> None:
        if self._udp is None:
            return
        for _ in range(_UDP_BURST):
            try:
                data, addr = self._udp.recvfrom(MAX_PING_BYTES + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno == _WSAEMSGSIZE:
                    # Windows: a datagram larger than the buffer is truncated
                    # and reported as an error -> it is an oversized ping.
                    self.counters.udp_dropped += 1
                    continue
                # Windows reports an earlier ICMP "port unreachable" here
                # (WSAECONNRESET); it concerns a previous peer, not this socket.
                continue
            peer = str(addr[0])
            if (
                not is_allowed_peer(peer)
                or not MIN_PING_BYTES <= len(data) <= MAX_PING_BYTES
                or not data.startswith(MAGIC)
            ):
                self.counters.udp_dropped += 1
                continue
            try:
                self._udp.sendto(data, addr)
            except OSError:
                self.counters.udp_dropped += 1
                continue
            self.counters.udp_echoed += 1
            self._touch()

    # ------------------------------------------------------- TCP admission
    def _accept(self) -> None:
        """Accept every queued connection (bounded burst) as *pending*."""
        if self._tcp is None or self._sel is None:
            return
        for _ in range(_ACCEPT_BURST):
            try:
                conn, addr = self._tcp.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self._note_error(f"accept: {exc}")
                return
            peer = str(addr[0])
            if not is_allowed_peer(peer):
                self.counters.tcp_refused_peer += 1
                self._log(f"refused non-LAN peer {peer}")
                conn.close()
                continue
            with self._lock:
                full = len(self._conns) >= self.limits.max_connections
            if full and self._pending:
                # Make room by evicting the oldest connection without a header.
                # Active measurements are never evicted.
                oldest = next(iter(self._pending))
                self._drop_pending(oldest)
                self.counters.tcp_evicted_pending += 1
                self.counters.tcp_refused_busy += 1
            elif full:
                self.counters.tcp_refused_busy += 1
                conn.close()
                continue
            conn.setblocking(False)
            with self._lock:
                self._conns.add(conn)
            self._pending[conn] = _Pending(
                peer, time.monotonic() + self.limits.header_timeout_s
            )
            self._sel.register(conn, selectors.EVENT_READ, "pending")
            self.counters.tcp_accepted += 1
            self._touch()

    def _drop_pending(self, conn: socket.socket) -> None:
        self._pending.pop(conn, None)
        if self._sel is not None:
            with contextlib.suppress(KeyError, ValueError):
                self._sel.unregister(conn)
        with self._lock:
            self._conns.discard(conn)
        with contextlib.suppress(OSError):
            conn.close()

    def _expire_pending(self) -> None:
        now = time.monotonic()
        while self._pending:
            conn, pending = next(iter(self._pending.items()))
            if pending.deadline > now:
                return  # FIFO with one timeout: the rest are younger
            self._drop_pending(conn)
            self.counters.tcp_header_timeouts += 1
            self._log(f"{pending.peer}: no complete request header in time; dropped")

    def _read_pending(self, conn: socket.socket) -> None:
        """Read what is available of the header; promote once it is complete."""
        pending = self._pending.get(conn)
        if pending is None:
            return
        need = LENGTH_BYTES if pending.body is None else LENGTH_BYTES + pending.body
        try:
            data = conn.recv(need - len(pending.buf))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop_pending(conn)
            return
        if not data:
            self._drop_pending(conn)
            return
        pending.buf += data
        try:
            if pending.body is None and len(pending.buf) == LENGTH_BYTES:
                pending.body = body_length(bytes(pending.buf))
                return
            if pending.body is None or len(pending.buf) < LENGTH_BYTES + pending.body:
                return
            msg = decode_body(bytes(pending.buf[LENGTH_BYTES:]))
        except ProtocolError as exc:
            self._note_error(f"{pending.peer}: ProtocolError: {exc}")
            self._drop_pending(conn)
            return
        self._promote(conn, pending.peer, msg)

    def _promote(self, conn: socket.socket, peer: str, msg: Message) -> None:
        """Header complete: the connection becomes an active measurement."""
        self._pending.pop(conn, None)
        if self._sel is not None:
            with contextlib.suppress(KeyError, ValueError):
                self._sel.unregister(conn)
        conn.setblocking(True)
        conn.settimeout(self.limits.io_timeout_s)
        thread = threading.Thread(
            target=self._handle, args=(conn, peer, msg), name="s1-conn", daemon=True
        )
        self._threads = [t for t in self._threads if t.is_alive()]
        try:
            thread.start()
        except RuntimeError as exc:  # cannot start a new thread
            self._note_error(f"{peer}: {exc}")
            with self._lock:
                self._conns.discard(conn)
            conn.close()
            return
        self._threads.append(thread)
        self._touch()

    # ----------------------------------------------------------- TCP handlers
    def _handle(self, conn: socket.socket, peer: str, msg: Message) -> None:
        try:
            if msg.get("protocol") != PROTOCOL:
                send_msg(conn, {"ok": False, "error": f"protocol must be {PROTOCOL}"})
                return
            op = msg.get("op")
            if op == "hello":
                send_msg(
                    conn,
                    {
                        "ok": True,
                        "protocol": PROTOCOL,
                        "hostname": socket.gethostname(),
                        "name": self.name,
                        "os": platform.system(),
                        "peer": peer,
                    },
                )
            elif op == "upload":
                self._op_upload(conn, msg)
            elif op == "download":
                self._op_download(conn, msg)
            elif op == "result":
                self._op_result(conn, msg)
            else:
                send_msg(conn, {"ok": False, "error": f"unknown op {op!r}"})
        except (OSError, ProtocolError) as exc:
            if not self._stop.is_set():
                self._note_error(f"{peer}: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._conns.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()
            self._touch()

    @staticmethod
    def _stream_index(msg: Message) -> int:
        if not valid_test_id(msg.get("test_id")):
            raise ProtocolError("bad test_id")
        stream = msg.get("stream")
        if not isinstance(stream, int) or isinstance(stream, bool):
            raise ProtocolError("bad stream")
        if not 0 <= stream < MAX_STREAMS:
            raise ProtocolError("stream out of range")
        return stream

    def _record(self, test_id: str, stream: int, stats: Message) -> None:
        with self._results_cond:
            entry = self._results.setdefault(test_id, {})
            entry[stream] = {"stream": stream, **stats}
            self._results.move_to_end(test_id)
            while len(self._results) > _MAX_RESULTS:
                self._results.popitem(last=False)
            self._results_cond.notify_all()

    def _op_upload(self, conn: socket.socket, msg: Message) -> None:
        """Receiver view: bytes and first/last-byte times on this host's clock.

        A peer that never half-closes cannot hold the handler forever: the
        upload is cut off ``limits.upload_limit_s()`` after its first byte and
        recorded with ``error`` = :data:`UPLOAD_LIMIT_ERROR`.
        """
        stream = self._stream_index(msg)
        buf = bytearray(RECV_BUFFER_BYTES)
        received = 0
        t_first: float | None = None
        t_last: float | None = None
        error: str | None = None
        deadline: float | None = None
        try:
            while not self._stop.is_set():
                n = conn.recv_into(buf)
                if n == 0:
                    break
                now = time.perf_counter()
                if t_first is None:
                    t_first = now
                    deadline = now + self.limits.upload_limit_s()
                t_last = now
                received += n
                if deadline is not None and now >= deadline:
                    error = UPLOAD_LIMIT_ERROR
                    break
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        self._record(
            msg["test_id"],
            stream,
            {"bytes": received, "t_first": t_first, "t_last": t_last, "error": error},
        )

    def _op_download(self, conn: socket.socket, msg: Message) -> None:
        """Sender view: bytes handed to the kernel and the send window."""
        stream = self._stream_index(msg)
        seconds = msg.get("seconds")
        if (
            not isinstance(seconds, int | float)
            or isinstance(seconds, bool)
            or not MIN_SECONDS <= seconds <= MAX_SECONDS
        ):
            raise ProtocolError("bad seconds")
        sent = 0
        error: str | None = None
        t_start = time.perf_counter()
        deadline = t_start + float(seconds)
        try:
            while not self._stop.is_set() and time.perf_counter() < deadline:
                sent += conn.send(self._payload)
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        t_end = time.perf_counter()
        self._record(
            msg["test_id"],
            stream,
            {"bytes": sent, "t_start": t_start, "t_end": t_end, "error": error},
        )
        if error is None:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_WR)
                conn.recv(1)  # wait (bounded by io_timeout) for the client to close

    def _op_result(self, conn: socket.socket, msg: Message) -> None:
        test_id = msg.get("test_id")
        streams = msg.get("streams")
        if not isinstance(test_id, str) or not valid_test_id(test_id):
            raise ProtocolError("bad result request")
        if not isinstance(streams, int) or not 1 <= streams <= MAX_STREAMS:
            raise ProtocolError("bad result request")
        deadline = time.monotonic() + self.limits.result_wait_s
        with self._results_cond:
            while len(self._results.get(test_id, {})) < streams:
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    break
                self._results_cond.wait(timeout=min(left, _SELECT_S))
            entry = dict(self._results.get(test_id, {}))
        stats = [entry[k] for k in sorted(entry)]
        if len(stats) < streams:
            send_msg(
                conn,
                {"ok": False, "error": f"only {len(stats)}/{streams} streams recorded"},
            )
            return
        send_msg(conn, {"ok": True, "streams": stats})
