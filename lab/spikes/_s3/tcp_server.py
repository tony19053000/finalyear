"""Raw-TCP sender/receiver: the control baseline every other transport is read
against, and the control channel (``hello`` / ``stats``) for all of them.

One thread per connection with a hard connection cap, a header timeout, a
per-operation socket timeout and LAN-only admission. A connection is closed as
soon as its single operation finishes, so threads and memory stay bounded by
``limits.max_connections`` whatever a peer does.
"""

from __future__ import annotations

import contextlib
import platform
import socket
import sys
import threading
from typing import Final

from spikes._s3 import payload
from spikes._s3.context import ServerContext
from spikes._s3.protocol import (
    PROTOCOL,
    Message,
    ProtocolError,
    family_of,
    is_lan_address,
    recv_msg,
    send_msg,
    transfer_request,
    valid_transfer_id,
)

_ACCEPT_TIMEOUT_S: Final = 0.2
_JOIN_S: Final = 5.0
_BACKLOG: Final = 32


class TcpServer:
    """Raw TCP transport + control channel."""

    def __init__(self, bind: str, port: int, ctx: ServerContext) -> None:
        self.bind = bind
        self.requested_port = port
        self.port = port
        self.ctx = ctx
        #: filled in by :class:`~spikes._s3.server.S3Server` once all ports are known
        self.ports: dict[str, int] = {}
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._conns: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ setup
    def open(self) -> int:
        sock = socket.socket(family_of(self.bind), socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind, self.requested_port))
            sock.listen(_BACKLOG)
        except OSError:
            sock.close()
            raise
        sock.settimeout(_ACCEPT_TIMEOUT_S)
        self._sock = sock
        self.port = int(sock.getsockname()[1])
        return self.port

    def start(self) -> None:
        if self._sock is None:
            self.open()
        thread = threading.Thread(target=self._accept_loop, name="s3-tcp", daemon=True)
        thread.start()
        self._accept_thread = thread

    def close(self) -> None:
        """Close the listener and every open connection; join handler threads."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=_JOIN_S)
        for thread in list(self._threads):
            thread.join(timeout=_JOIN_S)

    # -------------------------------------------------------------- accepting
    def _accept_loop(self) -> None:
        sock = self._sock
        if sock is None:  # pragma: no cover - start() opens it
            return
        while not self.ctx.stopping.is_set():
            try:
                conn, addr = sock.accept()
            except (TimeoutError, BlockingIOError):
                continue
            except OSError:
                return  # listener closed by stop()
            self._admit(conn, str(addr[0]))

    def _admit(self, conn: socket.socket, peer: str) -> None:
        if not is_lan_address(peer):
            self.ctx.count("refused_peer")
            self.ctx.say(f"tcp: refused non-LAN peer {peer}")
            conn.close()
            return
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            if len(self._conns) >= self.ctx.limits.max_connections:
                self.ctx.count("refused_busy")
                conn.close()
                return
            self._conns.add(conn)
        self.ctx.count("accepted")
        self.ctx.touch()
        thread = threading.Thread(
            target=self._serve_conn, args=(conn, peer), name="s3-tcp-conn", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as exc:  # cannot start a new thread
            self.ctx.note_error(f"tcp: {exc}")
            self._drop(conn)
            return
        with self._lock:
            self._threads.append(thread)

    def _drop(self, conn: socket.socket) -> None:
        with self._lock:
            self._conns.discard(conn)
        with contextlib.suppress(OSError):
            conn.close()

    # --------------------------------------------------------------- handling
    def _serve_conn(self, conn: socket.socket, peer: str) -> None:
        try:
            conn.settimeout(self.ctx.limits.header_timeout_s)
            try:
                msg = recv_msg(conn)
            except TimeoutError:
                self.ctx.count("header_timeouts")
                return
            except (OSError, ProtocolError) as exc:
                self.ctx.count("bad_requests")
                self.ctx.note_error(f"tcp {peer}: {type(exc).__name__}: {exc}")
                return
            conn.settimeout(self.ctx.limits.io_timeout_s)
            self._dispatch(conn, peer, msg)
        except (OSError, ProtocolError) as exc:
            if not self.ctx.stopping.is_set():
                self.ctx.note_error(f"tcp {peer}: {type(exc).__name__}: {exc}")
        finally:
            self._drop(conn)
            self.ctx.touch()

    def _dispatch(self, conn: socket.socket, peer: str, msg: Message) -> None:
        if msg.get("protocol") != PROTOCOL:
            self.ctx.count("bad_requests")
            send_msg(conn, {"ok": False, "error": f"protocol must be {PROTOCOL}"})
            return
        op = msg.get("op")
        try:
            if op == "hello":
                send_msg(conn, self._hello(peer))
            elif op == "download":
                self._download(conn, msg)
            elif op == "upload":
                self._upload(conn, msg)
            elif op == "stats":
                self._stats(conn, msg)
            else:
                self.ctx.count("bad_requests")
                send_msg(conn, {"ok": False, "error": f"unknown op {op!r}"})
        except ProtocolError as exc:
            self.ctx.count("bad_requests")
            with contextlib.suppress(OSError, ProtocolError):
                send_msg(conn, {"ok": False, "error": str(exc)})

    def _hello(self, peer: str) -> Message:
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "hostname": socket.gethostname(),
            "name": self.ctx.name,
            "os": platform.system(),
            "peer": peer,
            "ports": dict(self.ports),
        }

    def _download(self, conn: socket.socket, msg: Message) -> None:
        transfer_id, size, chunk = transfer_request(msg)
        self.ctx.count("transfers")
        self.ctx.transfers.begin(transfer_id, "tcp")
        sent = 0
        error: str | None = None
        try:
            for view in payload.iter_chunks(0, size, chunk):
                if self.ctx.stopping.is_set():
                    error = "server stopping"
                    break
                conn.sendall(view)
                sent += len(view)
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.ctx.transfers.end(transfer_id, num_bytes=sent, error=error)
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_WR)

    def _upload(self, conn: socket.socket, msg: Message) -> None:
        """Receiver view: bytes, window and CRC of what actually arrived."""
        transfer_id, size, chunk = transfer_request(msg)
        self.ctx.count("transfers")
        buf = bytearray(chunk)
        view = memoryview(buf)
        verifier = payload.Verifier()
        error: str | None = None
        # The window starts before the first recv (the request header has just
        # arrived): a small transfer that lands in one recv must not look infinitely
        # fast. It therefore includes up to ~RTT/2 of request latency.
        self.ctx.transfers.begin(transfer_id, "tcp")
        try:
            while verifier.received < size and not self.ctx.stopping.is_set():
                n = conn.recv_into(view, min(chunk, size - verifier.received))
                if n == 0:
                    break
                verifier.update(view[:n])
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
        crc_ok = verifier.matches(size)
        self.ctx.transfers.end(
            transfer_id, num_bytes=verifier.received, crc_ok=crc_ok, error=error
        )
        item = self.ctx.transfers.get(transfer_id)
        reply: Message = {
            "ok": error is None and crc_ok,
            "error": error or (None if crc_ok else "checksum mismatch at the receiver"),
            "stats": item.to_json() if item is not None else None,
        }
        with contextlib.suppress(OSError):
            send_msg(conn, reply)

    def _stats(self, conn: socket.socket, msg: Message) -> None:
        if not valid_transfer_id(msg.get("transfer_id")):
            raise ProtocolError("bad transfer_id")
        parts = msg.get("parts", 1)
        if (
            not isinstance(parts, int)
            or isinstance(parts, bool)
            or not 1 <= parts <= 64
        ):
            raise ProtocolError("bad parts")
        item = self.ctx.transfers.wait_for(
            str(msg["transfer_id"]), parts, self.ctx.limits.result_wait_s
        )
        if item is None:
            send_msg(conn, {"ok": False, "error": "unknown transfer"})
            return
        send_msg(conn, {"ok": True, "stats": item.to_json()})
