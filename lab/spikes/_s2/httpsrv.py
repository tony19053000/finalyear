"""The HTTP half of the S2 server: stdlib ``http.server``, nothing else.

``GET /ping`` answers a small JSON identity document; ``GET /blob?bytes=N``
streams ``N`` bytes so the client can measure time-to-first-byte and
throughput of a large-ish body. Safety (docs/LAB.md §6): LAN peers only,
``N`` capped at :data:`~spikes._s2.protocol.MAX_BLOB_BYTES`, every socket has a
timeout, and the handler never touches the file system.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from spikes._harness.protocol import family_of, is_allowed_peer
from spikes._s2.env import local_facts
from spikes._s2.protocol import (
    CHUNK_BYTES,
    MAX_BLOB_BYTES,
    PROTOCOL,
    ProtocolError,
    blob_size,
    encode,
)

SERVER_NAME: Final = "meshforge-s2/1"
_JOIN_S: Final = 10.0
_PATTERN: Final = b"\xa5"


class _Handler(BaseHTTPRequestHandler):
    """Request handler; configuration comes from the owning server instance."""

    server_version = SERVER_NAME
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def _srv(self) -> S2HttpServer:
        server: S2HttpServer = self.server  # type: ignore[assignment]
        return server

    # BaseHTTPRequestHandler logs to stderr by default; route it through the
    # spike's own log so --quiet really is quiet.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self._srv.log(f"http {self.client_address[0]}: {format % args}")

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        self._srv.note_error(f"http {self.client_address[0]}: {format % args}")

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = encode(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        srv = self._srv
        srv.touch()
        if not is_allowed_peer(self.client_address[0]):
            srv.bump("refused_peer")
            self.close_connection = True
            self._json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "LAN peers only (docs/LAB.md §6)"},
            )
            return
        parts = urlsplit(self.path)
        if parts.path == "/ping":
            srv.bump("ping")
            self._json(
                HTTPStatus.OK, {**srv.identity(), "peer": self.client_address[0]}
            )
            return
        if parts.path == "/blob":
            self._blob(parse_qs(parts.query))
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "use /ping or /blob"})

    def _blob(self, query: dict[str, list[str]]) -> None:
        srv = self._srv
        raw = (query.get("bytes") or ["0"])[0]
        try:
            size = blob_size(int(raw))
        except (ValueError, ProtocolError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        srv.bump("blob")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        chunk = _PATTERN * min(size, CHUNK_BYTES)
        left = size
        while left > 0 and not srv.stopping:
            piece = chunk if left >= len(chunk) else chunk[:left]
            try:
                self.wfile.write(piece)
            except OSError as exc:  # client vanished mid-body: normal here
                srv.bump("blob_aborted")
                srv.log(f"http blob aborted by {self.client_address[0]}: {exc}")
                self.close_connection = True
                return
            left -= len(piece)
            srv.touch()


class S2HttpServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` with a bounded, LAN-only S2 handler."""

    daemon_threads = True
    allow_reuse_address = False
    block_on_close = True

    def __init__(
        self,
        bind: str,
        port: int,
        *,
        name: str,
        log: Callable[[str], None],
        touch: Callable[[], None],
        note_error: Callable[[str], None],
        timeout_s: float = 30.0,
    ) -> None:
        self.address_family = family_of(bind)
        self.name = name
        self.log = log
        self.touch = touch
        self.note_error = note_error
        self.timeout = timeout_s
        self.stopping = False
        self._thread: threading.Thread | None = None
        self.counters: dict[str, int] = {
            "ping": 0,
            "blob": 0,
            "blob_aborted": 0,
            "refused_peer": 0,
        }
        #: handlers run on ``ThreadingHTTPServer``'s per-connection threads;
        #: ``+= 1`` on a dict entry is not atomic, so bumps go through ``bump``
        self._counter_lock = threading.Lock()
        super().__init__((bind, port), _Handler, bind_and_activate=True)
        self.socket.settimeout(timeout_s)

    def bump(self, name: str) -> None:
        with self._counter_lock:
            self.counters[name] += 1

    def snapshot(self) -> dict[str, int]:
        """A consistent copy of the counters (never a half-updated view)."""
        with self._counter_lock:
            return dict(self.counters)

    def identity(self) -> dict[str, Any]:
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "hostname": socket.gethostname(),
            "name": self.name,
            "max_blob_bytes": MAX_BLOB_BYTES,
            **local_facts(),
        }

    # Per-connection socket timeout: a peer that opens a connection and never
    # speaks must not hold a thread for ever.
    def get_request(self) -> tuple[socket.socket, Any]:
        conn, addr = super().get_request()
        conn.settimeout(self.timeout)
        return conn, addr

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        self.note_error(f"http handler error from {client_address}")

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.2}, name="s2-http"
        )
        thread.daemon = True
        thread.start()
        self._thread = thread
        return thread

    def stop(self) -> None:
        """Stop serving (if started), close the listener, join the thread.

        ``shutdown()`` is only called when :meth:`start` ran: socketserver's
        ``shutdown`` waits for ``serve_forever`` to acknowledge and blocks
        **forever** on a server that was opened but never served - which is
        what ``S2Server.open()`` does when the gRPC listener fails to bind.
        Idempotent: ``stop()`` may be called from several threads.
        """
        self.stopping = True
        thread, self._thread = self._thread, None
        if thread is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self.shutdown()
        with contextlib.suppress(OSError):
            self.server_close()
        if thread is not None:
            thread.join(timeout=_JOIN_S)
