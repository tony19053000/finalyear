"""The agent's read-only peer blob server (V0.5 D97).

A small HTTP/1.1 server on asyncio streams + ``h11`` (no web framework in the
agent) that lets other agents download blobs from this node's CAS:

* ``GET`` / ``HEAD`` on ``/v1/blobs/<64 hex>`` only; any other method ``405``,
  any other path ``404``; a request body ``400``.
* ``Authorization: Bearer <peer token>`` checked by
  :class:`~meshforge.agent.peer.auth.PeerAuthorizer` (D98): none ``401``, bad
  ``403``, a digest outside the token's tree ``403``, no key yet ``503``.
* One ``Range: bytes=a-b`` (or ``a-`` / ``-n``) -> ``206``; two ranges or a
  malformed one ``400``; outside the blob ``416``. No ``Range`` -> ``200``.
* Files come **only** from the CAS layout: the path is built from the validated
  hex digest, opened with ``O_NOFOLLOW`` and must be a regular file - nothing
  outside the CAS directory is ever reachable. Read-only: nothing is written.
* **Network probes (V0.6 G4, D111):** ``GET``/``HEAD /v1/probe?bytes=N`` answers
  ``N`` zero bytes (``N`` <= ``probe_max_bytes``, 8 MiB; more ``400``) to a
  request carrying a controller-signed **probe** token for this node (kind
  ``probe``, <= 60 s; a blob token is ``403``). No file is read; the body is
  written from one shared zero buffer under the same send deadline as a blob.

Bounds (``[agent.peer]``): request head bytes (``431``), a whole request head
within ``idle_timeout_s`` (slowloris) and idle keep-alive closed after it,
``max_connections`` (one more gets ``503`` and is closed), ``max_requests_per_conn``,
and a send deadline of ``idle_timeout_s + size / min_send_rate_bytes_s``.

**Delayed-ACK (D97).** ``TCP_NODELAY`` is set on every connection. A response
up to :data:`COALESCE_BYTES` is written as **one** buffer (head + body); a larger
body follows its head through ``loop.sendfile`` (zero copy where the OS has it).
With Nagle off the head never waits for the peer's delayed ACK of a previous
segment (the ~40 ms stall seen with split small writes).

The server never logs a token; refusals are logged with their reason code.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import socket
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import BinaryIO, Final

import h11

from meshforge.agent.cas.store import AgentCas
from meshforge.agent.peer.auth import PeerAuthorizer
from meshforge.agent.settings import PeerSettings
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import check_bind_address

__all__ = ["COALESCE_BYTES", "PeerBlobServer", "PeerServerStats", "parse_range"]

log = get_logger(__name__)

COALESCE_BYTES: Final = 256 * 1024
"""Bodies up to this are sent in the same write as the response head."""
_READ: Final = 64 * 1024
_PATH_RE: Final = re.compile(rb"/v1/blobs/([0-9a-f]{64})")
_PROBE_RE: Final = re.compile(rb"/v1/probe\?bytes=(\d{1,9})")
_ZEROS: Final = bytes(_READ)
_RANGE_RE: Final = re.compile(rb"bytes=(\d{0,19})-(\d{0,19})")
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_BINARY: Final = getattr(os, "O_BINARY", 0)
_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_SERVER: Final = b"meshforge-peer"
_REASONS: Final = {
    400: b"Bad Request",
    401: b"Unauthorized",
    403: b"Forbidden",
    404: b"Not Found",
    405: b"Method Not Allowed",
    416: b"Range Not Satisfiable",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    503: b"Service Unavailable",
}


class _Unsatisfiable(Exception):  # noqa: N818 - internal control flow
    pass


class _BadRange(Exception):  # noqa: N818 - internal control flow
    pass


def parse_range(value: bytes, size: int) -> tuple[int, int]:
    """``(start, length)`` of one ``bytes=`` range within ``size``.

    Raises ``_BadRange`` (``400``: syntax, several ranges) or ``_Unsatisfiable``
    (``416``: outside the blob, end before start).
    """
    match = _RANGE_RE.fullmatch(value.strip())
    if match is None:
        raise _BadRange
    first, last = match.group(1), match.group(2)
    if not first and not last:
        raise _BadRange
    if not first:  # suffix: the last n bytes
        n = int(last)
        if n == 0 or size == 0:
            raise _Unsatisfiable
        n = min(n, size)
        return size - n, n
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise _Unsatisfiable
    end = min(end, size - 1)
    return start, end - start + 1


@dataclass(slots=True)
class PeerServerStats:
    connections: int = 0
    connections_rejected: int = 0
    requests: int = 0
    served: int = 0
    bytes_sent: int = 0
    probes_served: int = 0
    probe_bytes_sent: int = 0
    refused: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refused[reason] = self.refused.get(reason, 0) + 1


class _Placeholder:
    """Stands for a sendfile'd body in ``h11`` (it only needs the length)."""

    def __init__(self, length: int) -> None:
        self._length = length

    def __len__(self) -> int:
        return self._length


def _raw_error(status: int) -> bytes:
    reason = _REASONS.get(status, b"Error")
    return (
        b"HTTP/1.1 %d %s\r\nserver: %s\r\ncontent-length: 0\r\n"
        b"connection: close\r\n\r\n" % (status, reason, _SERVER)
    )


def _set_nodelay(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def _local_ip_towards(host: str) -> str | None:
    """The local address the OS would use to reach ``host`` (no packet sent)."""
    try:
        family = (
            socket.AF_INET6
            if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
            else socket.AF_INET
        )
    except ValueError:
        family = socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((host, 9))
            return str(probe.getsockname()[0])
    except OSError:
        return None


class PeerBlobServer:
    """See module docstring. ``start()`` then ``close()``; event-loop owned."""

    def __init__(
        self,
        cas: AgentCas,
        authorizer: PeerAuthorizer,
        settings: PeerSettings,
        *,
        node_id: str,
    ) -> None:
        if settings.bind is None:
            raise ValueError("the peer server needs [agent.peer] bind")
        self._cas = cas
        self._auth = authorizer
        self._cfg = settings
        self._node_id = node_id
        self._host = check_bind_address(
            settings.bind, allow_any=settings.allow_any_bind
        )
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._active = 0
        self.port = 0
        self.stats = PeerServerStats()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client,
            host=self._host,
            port=self._cfg.port,
            reuse_address=True,
            limit=self._cfg.header_bytes + _READ,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        log.info(
            "agent.peer.listening",
            node_id=self._node_id,
            bind=self._host,
            port=self.port,
        )

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 5.0)
        log.info("agent.peer.closed", node_id=self._node_id, **self._summary())

    def _summary(self) -> dict[str, int]:
        return {
            "requests": self.stats.requests,
            "served": self.stats.served,
            "bytes_sent": self.stats.bytes_sent,
        }

    def endpoint(self, controller_host: str | None) -> str:
        """``host:port`` for ``Hello.peer_endpoint`` ("" = not advertisable)."""
        if self._server is None or not self.port:
            return ""
        host = self._cfg.advertise_host
        if host is None:
            if ipaddress.ip_address(self._host).is_unspecified:
                host = _local_ip_towards(controller_host) if controller_host else None
            else:
                host = self._host
        if not host:
            return ""
        shown = f"[{host}]" if ":" in host else host
        return f"{shown}:{self.port}"

    # ----------------------------------------------------------- connection

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            await self._connection(reader, writer)
        except (asyncio.CancelledError, ConnectionError, TimeoutError):
            pass
        except Exception:
            log.exception("agent.peer.connection_failed", node_id=self._node_id)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            if task is not None:
                self._clients.discard(task)

    async def _connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        _set_nodelay(writer)
        self.stats.connections += 1
        if self._active >= self._cfg.max_connections:
            self.stats.connections_rejected += 1
            self.stats.refuse("too_many_connections")
            writer.write(_raw_error(503))
            await asyncio.wait_for(writer.drain(), self._cfg.idle_timeout_s)
            return
        self._active += 1
        try:
            await self._serve(reader, writer)
        finally:
            self._active -= 1

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        loop = asyncio.get_running_loop()
        conn = h11.Connection(
            h11.SERVER, max_incomplete_event_size=self._cfg.header_bytes + 1
        )
        served = 0
        while True:
            deadline = loop.time() + self._cfg.idle_timeout_s
            try:
                event = await self._read_head(conn, reader, deadline)
                if not isinstance(event, h11.Request):
                    return  # ConnectionClosed (or nothing more to do)
                self.stats.requests += 1
                served += 1
                # GET/HEAD carry no body: read to EndOfMessage, refuse any data.
                while True:
                    tail = await self._next_event(conn, reader, deadline)
                    if isinstance(tail, h11.EndOfMessage):
                        break
                    if isinstance(tail, h11.Data):
                        self._refuse_log("body", None, None)
                        writer.write(_raw_error(400))
                        await asyncio.wait_for(writer.drain(), self._cfg.idle_timeout_s)
                    return  # a body, or the peer closed mid-request
            except h11.RemoteProtocolError as exc:
                status = exc.error_status_hint if exc.error_status_hint == 431 else 400
                self._refuse_log(
                    "header_too_big" if status == 431 else "bad_http", None, None
                )
                writer.write(_raw_error(status))
                with contextlib.suppress(TimeoutError, ConnectionError):
                    await asyncio.wait_for(writer.drain(), self._cfg.idle_timeout_s)
                return
            except TimeoutError:
                if conn.their_state is not h11.IDLE or conn.trailing_data[0]:
                    self._refuse_log("idle_timeout", None, None)
                return
            keep = served < self._cfg.max_requests_per_conn
            await self._respond(conn, writer, event, keep_alive=keep)
            if conn.our_state is h11.MUST_CLOSE or not keep:
                return
            try:
                conn.start_next_cycle()
            except h11.LocalProtocolError:
                return

    async def _read_head(
        self, conn: h11.Connection, reader: asyncio.StreamReader, deadline: float
    ) -> object:
        """The next request head, counting its bytes exactly: a head (request
        line + headers + blank line) longer than ``header_bytes`` is ``431``
        even when it arrived in one read (h11 bounds only incomplete heads)."""
        loop = asyncio.get_running_loop()
        limit = self._cfg.header_bytes
        fed = len(conn.trailing_data[0])  # pipelined bytes already buffered
        while True:
            event = conn.next_event()
            if event is not h11.NEED_DATA:
                if isinstance(event, h11.Request):
                    head = fed - len(conn.trailing_data[0])
                    if head > limit:
                        raise h11.RemoteProtocolError(
                            "request head too large", error_status_hint=431
                        )
                return event
            if fed > limit:
                raise h11.RemoteProtocolError(
                    "request head too large", error_status_hint=431
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                data = await reader.read(min(_READ, limit + 1 - fed))
            fed += len(data)
            conn.receive_data(data)

    async def _next_event(
        self, conn: h11.Connection, reader: asyncio.StreamReader, deadline: float
    ) -> object:
        loop = asyncio.get_running_loop()
        while True:
            event = conn.next_event()
            if event is not h11.NEED_DATA:
                return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                data = await reader.read(_READ)
            conn.receive_data(data)

    # -------------------------------------------------------------- request

    def _refuse_log(
        self, reason: str, digest: Digest | None, requester: str | None
    ) -> None:
        self.stats.refuse(reason)
        log.info(
            "agent.peer.refused",
            node_id=self._node_id,
            reason=reason,
            digest=None if digest is None else str(digest),
            requester=requester,
        )

    async def _respond(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        request: h11.Request,
        *,
        keep_alive: bool,
    ) -> None:
        headers: dict[bytes, list[bytes]] = {}
        for name, value in request.headers:
            headers.setdefault(bytes(name), []).append(bytes(value))
        method = bytes(request.method)
        base: list[tuple[bytes, bytes]] = [(b"server", _SERVER)]
        if not keep_alive:
            base.append((b"connection", b"close"))

        async def simple(
            status: int,
            reason: str,
            extra: list[tuple[bytes, bytes]] | None = None,
            digest: Digest | None = None,
            requester: str | None = None,
        ) -> None:
            self._refuse_log(reason, digest, requester)
            out = conn.send(
                h11.Response(
                    status_code=status,
                    headers=[*base, (b"content-length", b"0"), *(extra or [])],
                    reason=_REASONS.get(status, b""),
                )
            )
            out += conn.send(h11.EndOfMessage())
            writer.write(out)
            await asyncio.wait_for(writer.drain(), self._cfg.idle_timeout_s)

        if method not in (b"GET", b"HEAD"):
            await simple(405, "method", [(b"allow", b"GET, HEAD")])
            return
        target = bytes(request.target)
        auth_values = headers.get(b"authorization", [])
        range_values = headers.get(b"range", [])
        probe = _PROBE_RE.fullmatch(target)
        if probe is not None:
            await self._probe(
                conn, writer, method, base, auth_values, int(probe.group(1)), simple
            )
            return
        match = _PATH_RE.fullmatch(target)
        if match is None:
            await simple(404, "path")
            return
        digest = Digest(match.group(1).decode("ascii"))
        if len(auth_values) > 1 or len(range_values) > 1:
            await simple(400, "duplicate_header", digest=digest)
            return
        authorization = auth_values[0].decode("latin-1") if auth_values else None
        result = await self._auth.authorize(authorization, digest)
        requester = None if result.claims is None else str(result.claims.requester)
        if result.status:
            extra = [(b"www-authenticate", b"Bearer")] if result.status == 401 else []
            await simple(result.status, result.reason, extra, digest=digest)
            return
        try:
            # Opened on the loop, not in a thread: an open() + fstat() of a local
            # CAS file is microseconds, and a thread's result would leak its
            # descriptor if this request were cancelled meanwhile.
            fh = self._open(digest)
            owner = _Owner()
        except FileNotFoundError:
            await simple(404, "absent", digest=digest, requester=requester)
            return
        except OSError:
            await simple(404, "unreadable", digest=digest, requester=requester)
            return
        try:
            size = os.fstat(fh.fileno()).st_size
            status = 200
            start, length = 0, size
            extra = [(b"accept-ranges", b"bytes")]
            if range_values:
                try:
                    span = parse_range(range_values[0], size)
                except _BadRange:
                    await simple(400, "bad_range", digest=digest, requester=requester)
                    return
                except _Unsatisfiable:
                    await simple(
                        416,
                        "range_unsatisfiable",
                        [(b"content-range", b"bytes */%d" % size)],
                        digest=digest,
                        requester=requester,
                    )
                    return
                start, length = span
                status = 206
                extra.append(
                    (
                        b"content-range",
                        b"bytes %d-%d/%d" % (start, start + length - 1, size),
                    )
                )
            head = conn.send(
                h11.Response(
                    status_code=status,
                    headers=[
                        *base,
                        (b"content-type", b"application/octet-stream"),
                        (b"content-length", b"%d" % length),
                        *extra,
                    ],
                    reason=b"OK" if status == 200 else b"Partial Content",
                )
            )
            budget = self._cfg.idle_timeout_s + length / self._cfg.min_send_rate_bytes_s
            async with asyncio.timeout(budget):
                if method == b"HEAD" or length == 0:
                    writer.write(head + conn.send(h11.EndOfMessage()))
                    await writer.drain()
                elif length <= COALESCE_BYTES:
                    body = await _read_owned(fh, start, length, owner)
                    # One buffer: head + body (no small-write pair, D97).
                    writer.write(
                        head
                        + conn.send(h11.Data(data=body))
                        + conn.send(h11.EndOfMessage())
                    )
                    await writer.drain()
                    self.stats.bytes_sent += length
                else:
                    writer.write(head)
                    await writer.drain()
                    loop = asyncio.get_running_loop()
                    sent = await loop.sendfile(
                        writer.transport, fh, start, length, fallback=True
                    )
                    if sent != length:
                        raise ConnectionError("sendfile ended early")
                    conn.send_with_data_passthrough(h11.Data(data=_Placeholder(length)))  # type: ignore[arg-type]
                    conn.send(h11.EndOfMessage())
                    self.stats.bytes_sent += length
            self.stats.served += 1
            log.debug(
                "agent.peer.served",
                node_id=self._node_id,
                digest=str(digest),
                requester=requester,
                status=status,
                bytes=0 if method == b"HEAD" else length,
            )
        finally:
            if owner.owns:
                fh.close()

    async def _probe(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        method: bytes,
        base: list[tuple[bytes, bytes]],
        auth_values: list[bytes],
        size: int,
        simple: Callable[..., Awaitable[None]],
    ) -> None:
        """``/v1/probe?bytes=N`` (module docstring, D111)."""
        if len(auth_values) > 1:
            await simple(400, "duplicate_header")
            return
        authorization = auth_values[0].decode("latin-1") if auth_values else None
        result = self._auth.authorize_probe(authorization)
        if result.status:
            extra = [(b"www-authenticate", b"Bearer")] if result.status == 401 else []
            await simple(result.status, f"probe_{result.reason}", extra)
            return
        requester = None if result.probe is None else str(result.probe.requester)
        if size > self._cfg.probe_max_bytes:
            await simple(400, "probe_too_big", requester=requester)
            return
        head = conn.send(
            h11.Response(
                status_code=200,
                headers=[
                    *base,
                    (b"content-type", b"application/octet-stream"),
                    (b"content-length", b"%d" % size),
                    (b"cache-control", b"no-store"),
                ],
                reason=b"OK",
            )
        )
        budget = self._cfg.idle_timeout_s + size / self._cfg.min_send_rate_bytes_s
        sent = 0
        async with asyncio.timeout(budget):
            if method == b"HEAD" or size == 0:
                writer.write(head + conn.send(h11.EndOfMessage()))
                await writer.drain()
            else:
                writer.write(head)
                while sent < size:
                    piece = min(len(_ZEROS), size - sent)
                    chunk = _ZEROS if piece == len(_ZEROS) else _ZEROS[:piece]
                    writer.write(conn.send(h11.Data(data=chunk)))
                    await writer.drain()
                    sent += piece
                writer.write(conn.send(h11.EndOfMessage()))
                await writer.drain()
        self.stats.probes_served += 1
        self.stats.probe_bytes_sent += sent
        log.debug(
            "agent.peer.probe_served",
            node_id=self._node_id,
            requester=requester,
            bytes=sent,
        )

    def _open(self, digest: Digest) -> BinaryIO:
        """Open the CAS file of ``digest`` read-only; never a link, never a dir."""
        path = self._cas.store.path_for(digest)
        # O_NONBLOCK: a FIFO (or device) planted at a blob path must not block
        # open() on the event loop; it opens at once and fstat refuses it.
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY | _NONBLOCK)
        regular = False
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
            if regular and _NONBLOCK:
                os.set_blocking(fd, True)
        finally:
            if not regular:
                os.close(fd)
        if not regular:
            raise FileNotFoundError(str(digest))
        return os.fdopen(fd, "rb", buffering=0)


class _Owner:
    """Who closes the file: the request, or a read thread still using it."""

    owns = True


async def _read_owned(fh: BinaryIO, start: int, length: int, owner: _Owner) -> bytes:
    """Read in a thread; if the request is cancelled meanwhile, the thread's
    completion closes the file (never closed under a running read)."""
    read = asyncio.ensure_future(asyncio.to_thread(_pread, fh, start, length))
    try:
        return await asyncio.shield(read)
    except asyncio.CancelledError:
        owner.owns = False

        def close(done: asyncio.Future[bytes]) -> None:
            if not done.cancelled():
                done.exception()  # retrieved: the request is gone anyway
            fh.close()

        read.add_done_callback(close)
        raise


def _pread(fh: BinaryIO, start: int, length: int) -> bytes:
    fh.seek(start)
    data = bytearray()
    while len(data) < length:
        piece = fh.read(length - len(data))
        if not piece:
            raise ConnectionError("the blob changed size while it was read")
        data += piece
    return bytes(data)
