"""A real loopback HTTP/1.1 blob server for data-plane tests (V1.0 G9-A).

Imported as the top-level module ``mf_blob_range_server`` (``tests/conftest.py``
puts ``tests/_support`` on ``sys.path``). It speaks just enough of the
controller's blob API for an agent staging: ``GET /v1/blobs/<hex>`` with a
bearer token, whole (``200``) or one ``Range: bytes=a-b`` (``206``), keep-alive.

Faults are real socket events, not raised exceptions:

* ``cut_range_request = n`` - the n-th ``Range`` request (1-based) gets its
  head (with the full ``Content-Length``) and half of its body, then the server
  closes the connection: the client sees a body cut off mid-stream
  (httpx ``RemoteProtocolError``), exactly what the lab saw over Wi-Fi.

Every request is recorded (:attr:`BlobRangeServer.requests`) and the accepted
sockets are kept (:attr:`BlobRangeServer.accepted`) so a test can read their
options. ``start()`` / ``close()`` on the test's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass

__all__ = ["BlobRangeServer", "Served"]


@dataclass(frozen=True, slots=True)
class Served:
    hex: str
    range: tuple[int, int] | None
    """(start, end inclusive) of a Range request; None = a whole GET."""
    cut: bool = False


class BlobRangeServer:
    def __init__(
        self,
        blobs: dict[str, bytes],
        *,
        token: str = "tok",  # noqa: S107 - a test fake
        cut_range_request: int | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.blobs = blobs
        self.token = token
        self.cut_range_request = cut_range_request
        self.host = host
        self.port = 0
        self.requests: list[Served] = []
        self.accepted: list[socket.socket] = []
        self._range_requests = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ranges(self) -> list[tuple[int, int]]:
        return [r.range for r in self.requests if r.range is not None]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, 0)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._writers):
            writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 5)

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            self.accepted.append(sock)
        self._writers.add(writer)
        try:
            while await self._one(reader, writer):
                pass
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Serve one request; False = close the connection."""
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        _method, path, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if headers.get("authorization") != f"Bearer {self.token}":
            await self._send(writer, 401, b"")
            return True
        hex_ = path.rsplit("/", 1)[-1]
        data = self.blobs.get(hex_)
        if data is None:
            await self._send(writer, 404, b"")
            return True
        rng = headers.get("range")
        if rng is None:
            self.requests.append(Served(hex_, None))
            await self._send(writer, 200, data)
            return True
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        body = data[start : end + 1]
        self._range_requests += 1
        if self._range_requests == self.cut_range_request:
            self.requests.append(Served(hex_, (start, end), cut=True))
            writer.write(self._head(206, len(body)) + body[: len(body) // 2])
            await writer.drain()
            return False  # closed mid-body
        self.requests.append(Served(hex_, (start, end)))
        await self._send(writer, 206, body)
        return True

    @staticmethod
    def _head(status: int, length: int) -> bytes:
        return (
            f"HTTP/1.1 {status} X\r\ncontent-length: {length}\r\n"
            "content-type: application/octet-stream\r\n\r\n"
        ).encode("ascii")

    async def _send(
        self, writer: asyncio.StreamWriter, status: int, body: bytes
    ) -> None:
        writer.write(self._head(status, len(body)) + body)
        await writer.drain()
