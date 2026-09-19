"""HTTP sender: ``GET /blob`` with and without ``Range`` (ADR-0003's data plane).

Standard-library :class:`http.server.ThreadingHTTPServer` with HTTP/1.1
keep-alive, byte-range support (single range; a multi-range request is refused
with 416 rather than answered with a multipart body), the same LAN-only
admission and connection cap as the raw-TCP server, and a socket timeout so a
peer that stops reading cannot pin a thread forever.

It is deliberately *not* a production server - it exists to measure what
stdlib HTTP costs against raw TCP on the same laptops.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from spikes._s3 import payload
from spikes._s3.context import ServerContext
from spikes._s3.protocol import (
    HTTP_BLOB_PATH,
    MAX_CHUNK_BYTES,
    MAX_SIZE_BYTES,
    MIN_CHUNK_BYTES,
    MIN_SIZE_BYTES,
    family_of,
    is_lan_address,
    valid_transfer_id,
)

_POLL_S: Final = 0.2
_JOIN_S: Final = 5.0
SERVER_TOKEN: Final = "meshforge-s3"  # noqa: S105 - a Server: header value, not a secret


class BadRequestError(ValueError):
    """The query string is missing or out of range (answered with 400)."""


def parse_range(header: str, size: int) -> tuple[int, int]:
    """RFC 9110 single byte range -> ``(offset, length)``; ``ValueError`` if invalid.

    Supports ``bytes=a-b``, ``bytes=a-`` and the suffix form ``bytes=-n``.
    """
    unit, sep, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or not sep or "," in spec:
        raise ValueError(f"unsupported range {header!r}")
    first, dash, last = spec.strip().partition("-")
    if not dash:
        raise ValueError(f"unsupported range {header!r}")
    if not first:
        if not last.isdigit() or int(last) == 0:
            raise ValueError(f"unsupported range {header!r}")
        length = min(int(last), size)
        return size - length, length
    if not first.isdigit() or (last and not last.isdigit()):
        raise ValueError(f"unsupported range {header!r}")
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        raise ValueError(f"range not satisfiable: {header!r}")
    return start, end - start + 1


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _int_param(
    query: dict[str, list[str]], key: str, lo: int, hi: int, default: int
) -> int:
    raw = _one(query, key)
    if raw is None:
        return default
    if not raw.isdigit():
        raise BadRequestError(f"bad {key}")
    value = int(raw)
    if not lo <= value <= hi:
        raise BadRequestError(f"{key} out of range")
    return value


class S3HttpHandler(BaseHTTPRequestHandler):
    """``GET /blob?size=&chunk=&transfer=`` and ``GET /stats?transfer=&parts=``."""

    protocol_version = "HTTP/1.1"
    server_version = SERVER_TOKEN
    sys_version = ""
    ctx: ServerContext

    # --------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        self.ctx.say("http: " + (format % args))

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        self.ctx.count("bad_requests")
        self.ctx.say("http: " + (format % args))

    def _fail(
        self, status: HTTPStatus, text: str, extra: dict[str, str] | None = None
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    # ----------------------------------------------------------------- routes
    def do_GET(self) -> None:
        self.ctx.touch()
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == HTTP_BLOB_PATH:
                self._blob(query)
            elif url.path == "/stats":
                self._stats(query)
            elif url.path == "/hello":
                self._json({"ok": True, "name": self.ctx.name, "server": SERVER_TOKEN})
            else:
                self._fail(HTTPStatus.NOT_FOUND, "not found\n")
        except BadRequestError as exc:
            self.ctx.count("bad_requests")
            self._fail(HTTPStatus.BAD_REQUEST, f"{exc}\n")
        except OSError as exc:  # client vanished mid-response
            if not self.ctx.stopping.is_set():
                self.ctx.note_error(f"http: {type(exc).__name__}: {exc}")
            self.close_connection = True

    def _json(self, doc: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(doc, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _blob(self, query: dict[str, list[str]]) -> None:
        size = _int_param(query, "size", MIN_SIZE_BYTES, MAX_SIZE_BYTES, 0)
        if size == 0:
            raise BadRequestError("size is required")
        chunk = _int_param(
            query, "chunk", MIN_CHUNK_BYTES, MAX_CHUNK_BYTES, MIN_CHUNK_BYTES * 256
        )
        transfer_id = _one(query, "transfer") or ""
        if not valid_transfer_id(transfer_id):
            raise BadRequestError("bad transfer id")
        header = self.headers.get("Range")
        offset, length, status = 0, size, HTTPStatus.OK
        if header is not None:
            try:
                offset, length = parse_range(header, size)
            except ValueError as exc:
                self.ctx.count("bad_requests")
                self._fail(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    f"{exc}\n",
                    {"Content-Range": f"bytes */{size}"},
                )
                return
            status = HTTPStatus.PARTIAL_CONTENT
        self.ctx.count("transfers")
        self.ctx.transfers.begin(transfer_id, "http")
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status is HTTPStatus.PARTIAL_CONTENT:
            self.send_header(
                "Content-Range", f"bytes {offset}-{offset + length - 1}/{size}"
            )
        self.end_headers()
        sent = 0
        error: str | None = None
        try:
            for view in payload.iter_chunks(offset, length, chunk):
                if self.ctx.stopping.is_set():
                    error = "server stopping"
                    break
                self.wfile.write(view)
                sent += len(view)
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.close_connection = True
        self.ctx.transfers.end(transfer_id, num_bytes=sent, error=error)
        self.ctx.touch()

    def _stats(self, query: dict[str, list[str]]) -> None:
        transfer_id = _one(query, "transfer") or ""
        if not valid_transfer_id(transfer_id):
            raise BadRequestError("bad transfer id")
        parts = _int_param(query, "parts", 1, 64, 1)
        item = self.ctx.transfers.wait_for(
            transfer_id, parts, self.ctx.limits.result_wait_s
        )
        if item is None:
            self._json({"ok": False, "error": "unknown transfer"}, HTTPStatus.NOT_FOUND)
            return
        self._json({"ok": True, "stats": item.to_json()})


class S3HttpServer(ThreadingHTTPServer):
    """LAN-only, connection-capped, auto-stopping HTTP server."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, bind: str, port: int, ctx: ServerContext) -> None:
        self.ctx = ctx
        self.address_family = family_of(bind)
        #: sockets admitted by :meth:`verify_request` and not yet shut down; the
        #: cap counts these, so a *refused* connection (which socketserver also
        #: passes to ``shutdown_request``) never frees a slot it never held
        self._active: set[int] = set()
        self._active_lock = threading.Lock()
        bound_ctx = ctx

        class _BoundHandler(S3HttpHandler):
            ctx = bound_ctx
            timeout = ctx.limits.io_timeout_s

        super().__init__((bind, port), _BoundHandler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def verify_request(self, request: Any, client_address: Any) -> bool:
        peer = str(client_address[0])
        if not is_lan_address(peer):
            self.ctx.count("refused_peer")
            self.ctx.say(f"http: refused non-LAN peer {peer}")
            return False
        with self._active_lock:
            if len(self._active) >= self.ctx.limits.max_connections:
                self.ctx.count("refused_busy")
                return False
            self._active.add(id(request))
        self.ctx.count("accepted")
        self.ctx.touch()
        return True

    @property
    def active_connections(self) -> int:
        with self._active_lock:
            return len(self._active)

    def shutdown_request(self, request: Any) -> None:
        with self._active_lock:
            self._active.discard(id(request))
        super().shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not self.ctx.stopping.is_set():
            self.ctx.note_error(f"http: handler error from {client_address[0]}")

    def start(self) -> None:
        thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": _POLL_S},
            name="s3-http",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def close(self) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            self.shutdown()
        with contextlib.suppress(OSError):
            self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=_JOIN_S)
